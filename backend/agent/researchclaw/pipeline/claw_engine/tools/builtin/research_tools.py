"""Research-specific tools: LaTeX compile, BibTeX search, data analysis, web search.

These are domain tools that differentiate ScholarLab from generic coding agents.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from researchclaw.pipeline.claw_engine.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
)

logger = logging.getLogger(__name__)


class LatexCompileTool(Tool):
    name = "latex_compile"
    description = (
        "Compile a LaTeX document to PDF using pdflatex or xelatex. "
        "Runs bibtex if .bib files are present. Returns compilation log. "
        "Use for verifying paper drafts, checking for errors."
    )
    is_read_only = False
    is_concurrency_safe = False

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the main .tex file to compile.",
                },
                "engine": {
                    "type": "string",
                    "description": "LaTeX engine to use (pdflatex, xelatex, lualatex). Default: pdflatex.",
                },
                "bibtex": {
                    "type": "boolean", "default": True,
                    "description": "Run bibtex/biber for bibliography (default: true).",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_path = args["path"]
        if os.path.isabs(raw_path):
            tex_path = Path(raw_path).resolve()
        else:
            tex_path = (context.workspace / raw_path).resolve()

        if not tex_path.exists():
            return ToolResult(data=f"File not found: {raw_path}", is_error=True)

        engine = args.get("engine", "pdflatex")
        run_bib = args.get("bibtex", True)
        work_dir = tex_path.parent
        base_name = tex_path.stem

        outputs: list[str] = []

        def _run(cmd: list[str], label: str) -> int:
            try:
                r = subprocess.run(
                    cmd, cwd=str(work_dir),
                    capture_output=True, timeout=120,
                )
                stdout = r.stdout.decode("utf-8", errors="replace")
                stderr = r.stderr.decode("utf-8", errors="replace")
                outputs.append(f"=== {label} (exit {r.returncode}) ===")
                if stdout:
                    lines = stdout.strip().splitlines()
                    errors_warnings = [
                        l for l in lines
                        if any(kw in l.lower() for kw in ("error", "warning", "!"))
                    ]
                    if errors_warnings:
                        outputs.append("\n".join(errors_warnings[-30:]))
                    else:
                        outputs.append(f"({len(lines)} lines, no errors)")
                if stderr and r.returncode != 0:
                    outputs.append(f"[stderr] {stderr[:500]}")
                return r.returncode
            except subprocess.TimeoutExpired:
                outputs.append(f"=== {label} TIMEOUT ===")
                return -1
            except FileNotFoundError:
                outputs.append(f"=== {label}: command not found ===")
                return -1

        _run([engine, "-interaction=nonstopmode", "-halt-on-error", str(tex_path)], f"{engine} pass 1")

        if run_bib:
            bib_files = list(work_dir.glob("*.bib"))
            if bib_files:
                _run(["bibtex", base_name], "bibtex")
                _run([engine, "-interaction=nonstopmode", str(tex_path)], f"{engine} pass 2")
                _run([engine, "-interaction=nonstopmode", str(tex_path)], f"{engine} pass 3")

        pdf_path = work_dir / f"{base_name}.pdf"
        if pdf_path.exists():
            size_kb = pdf_path.stat().st_size / 1024
            outputs.append(f"\nPDF generated: {base_name}.pdf ({size_kb:.1f} KB)")
        else:
            outputs.append("\nNo PDF generated — check errors above.")

        data = "\n".join(outputs)
        if len(data) > 16000:
            data = data[:16000] + "\n... [truncated]"
        return ToolResult(data=data, files_modified=[f"{base_name}.pdf"])

    def summarize_input(self, args: dict[str, Any]) -> str:
        return args.get("path", "?")


class BibSearchTool(Tool):
    name = "bib_search"
    description = (
        "Search BibTeX/bibliography files for entries matching a query. "
        "Searches across title, author, year, and keywords fields. "
        "Use for finding citations, verifying references."
    )
    is_read_only = True
    is_concurrency_safe = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (matches title, author, keywords).",
                },
                "path": {
                    "type": "string",
                    "description": "Path to .bib file or directory containing .bib files.",
                },
                "limit": {
                    "type": "integer", "minimum": 1, "default": 20,
                    "description": "Max results to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        query = args["query"].lower()
        raw_path = args.get("path")
        limit = args.get("limit", 20)

        if raw_path:
            p = Path(raw_path).resolve() if os.path.isabs(raw_path) else (context.workspace / raw_path).resolve()
            if p.is_file():
                bib_files = [p]
            elif p.is_dir():
                bib_files = list(p.rglob("*.bib"))
            else:
                return ToolResult(data=f"Not found: {raw_path}", is_error=True)
        else:
            bib_files = list(context.workspace.rglob("*.bib"))

        if not bib_files:
            return ToolResult(data="No .bib files found.")

        results: list[str] = []
        entry_pattern = re.compile(r'@(\w+)\{([^,]+),', re.MULTILINE)

        for bib_path in bib_files[:10]:
            try:
                text = bib_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            entries = entry_pattern.finditer(text)
            for m in entries:
                start = m.start()
                depth = 0
                end = start
                for i in range(start, len(text)):
                    if text[i] == '{':
                        depth += 1
                    elif text[i] == '}':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                entry_text = text[start:end]

                if query in entry_text.lower():
                    results.append(entry_text.strip())
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break

        if not results:
            return ToolResult(data=f"No bib entries matching '{args['query']}'")

        data = f"Found {len(results)} matching entries:\n\n" + "\n\n".join(results)
        if len(data) > 16000:
            data = data[:16000] + "\n... [truncated]"
        return ToolResult(data=data)

    def summarize_input(self, args: dict[str, Any]) -> str:
        return args.get("query", "?")


class DataAnalysisTool(Tool):
    name = "data_analysis"
    description = (
        "Run a quick data analysis on a CSV/TSV file. Returns summary statistics, "
        "column info, and first/last rows. Use for understanding experiment data."
    )
    is_read_only = True
    is_concurrency_safe = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the CSV/TSV file to analyze.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional analysis query (e.g. 'correlation between col_a and col_b').",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_path = args["path"]
        if os.path.isabs(raw_path):
            file_path = Path(raw_path).resolve()
        else:
            file_path = (context.workspace / raw_path).resolve()

        if not file_path.exists():
            return ToolResult(data=f"File not found: {raw_path}", is_error=True)

        import csv
        sep = "\t" if file_path.suffix.lower() in (".tsv", ".tab") else ","

        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return ToolResult(data=f"Cannot read: {e}", is_error=True)

        lines = text.strip().splitlines()
        if not lines:
            return ToolResult(data="Empty file")

        reader = csv.reader(lines, delimiter=sep)
        rows = list(reader)
        if len(rows) < 2:
            return ToolResult(data=f"Only {len(rows)} row(s), too few to analyze")

        header = rows[0]
        data_rows = rows[1:]

        parts = [
            f"File: {file_path.name}",
            f"Rows: {len(data_rows)}, Columns: {len(header)}",
            f"Columns: {', '.join(header)}",
            "",
            "First 5 rows:",
        ]
        for r in data_rows[:5]:
            parts.append("  " + sep.join(r))

        if len(data_rows) > 5:
            parts.append(f"  ... ({len(data_rows) - 5} more rows)")
            parts.append("Last 3 rows:")
            for r in data_rows[-3:]:
                parts.append("  " + sep.join(r))

        for col_idx, col_name in enumerate(header):
            values = [r[col_idx] for r in data_rows if col_idx < len(r)]
            numeric = []
            for v in values:
                try:
                    numeric.append(float(v))
                except (ValueError, TypeError):
                    pass
            if numeric and len(numeric) > len(values) * 0.5:
                avg = sum(numeric) / len(numeric)
                mn = min(numeric)
                mx = max(numeric)
                parts.append(
                    f"\n{col_name}: min={mn:.4g}, max={mx:.4g}, "
                    f"mean={avg:.4g}, count={len(numeric)}"
                )

        return ToolResult(data="\n".join(parts))

    def summarize_input(self, args: dict[str, Any]) -> str:
        return args.get("path", "?")


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web for academic papers, documentation, or technical information. "
        "Use for literature review, finding related work, checking latest results."
    )
    is_read_only = True
    is_concurrency_safe = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "num_results": {
                    "type": "integer", "minimum": 1, "maximum": 20, "default": 5,
                    "description": "Number of results to return (default: 5).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def call(self, args: dict[str, Any], context: ToolContext) -> ToolResult:
        query = args["query"]
        return ToolResult(
            data=(
                f"Web search for: {query}\n"
                "(Web search integration pending — use bash with curl for now)"
            ),
        )

    def summarize_input(self, args: dict[str, Any]) -> str:
        return args.get("query", "?")[:60]
