"""LaTeX compilation and error repair utilities (IMP-18).

Provides ``compile_latex()`` which attempts ``xelatex`` compilation,
parses the log for common errors, applies automated fixes, and retries
up to 3 times.  Designed to run inside ``_package_deliverables()`` so
that the final paper.tex in ``deliverables/`` is compile-tested.

Uses xelatex (not pdflatex) for native Unicode support, eliminating
manual Unicode escape workarounds.

If xelatex is not installed the module gracefully returns a failure
report without raising.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CompileResult:
    """Outcome of a LaTeX compilation attempt."""

    success: bool
    log_excerpt: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fixes_applied: list[str] = field(default_factory=list)
    attempts: int = 0


def compile_latex(
    tex_path: Path,
    *,
    max_attempts: int = 3,
    timeout: int = 120,
) -> CompileResult:
    """Compile *tex_path* with pdflatex, auto-fixing common errors.

    Parameters
    ----------
    tex_path:
        Path to the ``.tex`` file.  Must be inside a directory that also
        contains ``references.bib`` and any required ``.sty`` files.
    max_attempts:
        Maximum compile→fix cycles.
    timeout:
        Seconds before killing a stuck pdflatex process.

    Returns
    -------
    CompileResult
        Contains success flag, log excerpt, errors found, and fixes applied.
    """
    if not shutil.which("xelatex"):
        return CompileResult(
            success=False,
            log_excerpt="xelatex not found on PATH",
            errors=["xelatex not installed"],
        )

    result = CompileResult(success=False)
    work_dir = tex_path.parent
    tex_name = tex_path.name

    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt
        try:
            _raw = subprocess.run(
                [
                    "xelatex",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    tex_name,
                ],
                cwd=work_dir,
                capture_output=True,
                timeout=timeout,
            )
            proc = subprocess.CompletedProcess(
                _raw.args, _raw.returncode,
                stdout=_raw.stdout.decode("utf-8", errors="replace") if _raw.stdout else "",
                stderr=_raw.stderr.decode("utf-8", errors="replace") if _raw.stderr else "",
            )
        except subprocess.TimeoutExpired:
            result.errors.append(f"xelatex timed out after {timeout}s")
            break
        except FileNotFoundError:
            result.errors.append("xelatex not found")
            break

        log_text = proc.stdout + "\n" + proc.stderr
        errors, warnings = _parse_log(log_text)
        result.errors = errors
        result.warnings = warnings
        result.log_excerpt = log_text[-2000:] if len(log_text) > 2000 else log_text

        if proc.returncode == 0:
            result.success = True
            # Run bibtex + two more xelatex passes for bibliography & cross-refs
            bib_stem = tex_name.rsplit(".", 1)[0]
            _run_bibtex(work_dir, bib_stem, timeout=60)
            for _pass in range(2):
                subprocess.run(
                    ["xelatex", "-interaction=nonstopmode", tex_name],
                    cwd=work_dir,
                    capture_output=True,
                    timeout=timeout,
                )
            logger.info("IMP-18: LaTeX compiled successfully on attempt %d", attempt)
            break

        # Try to auto-fix errors
        tex_text = tex_path.read_text(encoding="utf-8")
        fixed_text, fixes = fix_common_latex_errors(tex_text, errors)
        if fixes:
            result.fixes_applied.extend(fixes)
            tex_path.write_text(fixed_text, encoding="utf-8")
            logger.info(
                "IMP-18: Applied %d fixes on attempt %d: %s",
                len(fixes),
                attempt,
                fixes,
            )
        else:
            # No fixes available — stop retrying
            logger.warning(
                "IMP-18: Compilation failed on attempt %d with %d unfixable errors",
                attempt,
                len(errors),
            )
            break

    return result


def _escape_unicode_for_latex(text: str) -> tuple[str, list[str]]:
    """Replace Unicode characters that pdflatex cannot handle with LaTeX equivalents.

    Returns ``(escaped_text, list_of_fix_descriptions)``.
    """
    fixes: list[str] = []
    # Unicode subscripts (U+2080–U+2089 + Latin subscripts U+1D62–U+1D6A)
    sub_map = {
        "\u2080": "\\textsubscript{0}", "\u2081": "\\textsubscript{1}",
        "\u2082": "\\textsubscript{2}", "\u2083": "\\textsubscript{3}",
        "\u2084": "\\textsubscript{4}", "\u2085": "\\textsubscript{5}",
        "\u2086": "\\textsubscript{6}", "\u2087": "\\textsubscript{7}",
        "\u2088": "\\textsubscript{8}", "\u2089": "\\textsubscript{9}",
        "\u1D62": "\\textsubscript{i}", "\u1D63": "\\textsubscript{r}",
        "\u1D64": "\\textsubscript{u}", "\u1D65": "\\textsubscript{v}",
    }
    # Unicode superscripts (U+2070–U+2079, U+00B2, U+00B3, U+00B9)
    sup_map = {
        "\u2070": "\\textsuperscript{0}", "\u00B9": "\\textsuperscript{1}",
        "\u00B2": "\\textsuperscript{2}", "\u00B3": "\\textsuperscript{3}",
        "\u2074": "\\textsuperscript{4}", "\u2075": "\\textsuperscript{5}",
        "\u2076": "\\textsuperscript{6}", "\u2077": "\\textsuperscript{7}",
        "\u2078": "\\textsuperscript{8}", "\u2079": "\\textsuperscript{9}",
    }
    for ch, repl in {**sub_map, **sup_map}.items():
        if ch in text:
            text = text.replace(ch, repl)
            fixes.append(f"Escaped Unicode {repr(ch)} -> {repl}")
    # Greek letters commonly used in math
    greek_map = {
        "\u03A9": "\\Omega", "\u03C9": "\\omega",
        "\u03B1": "\\alpha", "\u03B2": "\\beta", "\u03B3": "\\gamma",
        "\u03B4": "\\delta", "\u03B5": "\\epsilon", "\u03B8": "\\theta",
        "\u03BB": "\\lambda", "\u03BC": "\\mu", "\u03C0": "\\pi",
        "\u03C3": "\\sigma", "\u03C4": "\\tau", "\u03C6": "\\phi",
        "\u0393": "\\Gamma", "\u0394": "\\Delta", "\u0398": "\\Theta",
        "\u039B": "\\Lambda", "\u03A0": "\\Pi", "\u03A3": "\\Sigma",
        "\u03A6": "\\Phi", "\u03A8": "\\Psi",
    }
    for ch, repl in greek_map.items():
        if ch in text:
            text = text.replace(ch, f"${repl}$")
            fixes.append(f"Escaped Unicode {repr(ch)} -> ${repl}$")

    # En-dash / Em-dash
    if "\u2013" in text:
        text = text.replace("\u2013", "--")
        fixes.append("Escaped Unicode en-dash -> --")
    if "\u2014" in text:
        text = text.replace("\u2014", "---")
        fixes.append("Escaped Unicode em-dash -> ---")
    # Unicode minus sign (U+2212) — different from ASCII hyphen
    if "\u2212" in text:
        text = text.replace("\u2212", "-")
        fixes.append("Escaped Unicode minus sign -> -")
    # Unicode multiplication sign (U+00D7)
    if "\u00D7" in text:
        text = text.replace("\u00D7", "\\times")
        fixes.append("Escaped Unicode times -> \\times")
    # Unicode division sign (U+00F7)
    if "\u00F7" in text:
        text = text.replace("\u00F7", "\\div")
        fixes.append("Escaped Unicode division -> \\div")
    # Common math symbols
    math_map = {
        "\u2200": "\\forall",   # ∀
        "\u2203": "\\exists",   # ∃
        "\u2208": "\\in",       # ∈
        "\u2209": "\\notin",    # ∉
        "\u220B": "\\ni",       # ∋
        "\u2227": "\\wedge",    # ∧
        "\u2228": "\\vee",      # ∨
        "\u2229": "\\cap",      # ∩
        "\u222A": "\\cup",      # ∪
        "\u2282": "\\subset",   # ⊂
        "\u2286": "\\subseteq", # ⊆
        "\u2283": "\\supset",   # ⊃
        "\u2287": "\\supseteq", # ⊇
        "\u2260": "\\neq",      # ≠
        "\u2264": "\\leq",      # ≤
        "\u2265": "\\geq",      # ≥
        "\u221A": "\\sqrt{}",   # √ (special — needs arg)
        "\u221E": "\\infty",    # ∞
        "\u2192": "\\rightarrow", # →
        "\u2190": "\\leftarrow",  # ←
        "\u2194": "\\leftrightarrow", # ↔
        "\u21D2": "\\Rightarrow",   # ⇒
        "\u2026": "\\ldots",    # …
        "\u00B1": "\\pm",       # ±
        "\u00B0": "^{\\circ}",  # °
        "\u2211": "\\sum",      # ∑
        "\u222B": "\\int",      # ∫
        "\u2202": "\\partial",  # ∂
        "\u2206": "\\Delta",    # ∆ (increment, use math Delta)
        "\u2207": "\\nabla",    # ∇
    }
    for ch, repl in math_map.items():
        if ch in text:
            if ch == "\u221A":  # √ needs special handling
                text = text.replace(ch, repl)
            else:
                text = text.replace(ch, f"${repl}$")
            fixes.append(f"Escaped Unicode {repr(ch)} -> {repl}")
    return text, fixes


def fix_common_latex_errors(
    tex_text: str, errors: list[str]
) -> tuple[str, list[str]]:
    """Apply automated fixes for common LaTeX errors.

    Returns ``(fixed_text, list_of_fix_descriptions)``.
    """
    fixes: list[str] = []
    fixed = tex_text

    # Pre-process: escape Unicode characters before any error-specific fixes
    fixed, unicode_fixes = _escape_unicode_for_latex(fixed)
    fixes.extend(unicode_fixes)

    for err in errors:
        err_lower = err.lower()

        # Undefined control sequence: remove the offending command
        if "undefined control sequence" in err_lower:
            # Extract the command name from error like "! Undefined control sequence. \foo"
            m = re.search(r"\\([a-zA-Z]+)", err)
            if m:
                cmd = m.group(1)
                # Don't remove standard commands
                _safe_to_remove = {
                    "textsc", "textsl", "mathbb", "mathcal",
                    "bm", "boldsymbol",
                }
                if cmd in _safe_to_remove:
                    # Replace \cmd{text} → text
                    fixed = re.sub(
                        rf"\\{cmd}\{{([^}}]*)\}}", r"\1", fixed
                    )
                    fixes.append(f"Removed undefined \\{cmd}")

        # Missing $ inserted — likely unescaped underscore or caret
        if "missing $ inserted" in err_lower:
            # Find bare underscores outside of math mode and escape them
            # This is a conservative fix — only fixes _text_ patterns
            pass  # Already handled by converter's _convert_inline

        # File not found
        if "file" in err_lower and "not found" in err_lower:
            m = re.search(r"File `([^']+)' not found", err)
            if m:
                missing_file = m.group(1)
                if missing_file.endswith(".sty"):
                    # Comment out the usepackage line
                    pkg = missing_file.replace(".sty", "")
                    fixed = re.sub(
                        rf"\\usepackage(\[[^\]]*\])?\{{{pkg}\}}",
                        f"% IMP-18: Removed missing package {pkg}",
                        fixed,
                    )
                    fixes.append(f"Removed missing package {pkg}")

        # Too many unprocessed floats
        if "too many unprocessed floats" in err_lower:
            # Add \clearpage before problematic float
            fixed = fixed.replace(
                "\\begin{table}",
                "\\clearpage\n\\begin{table}",
                1,
            )
            fixes.append("Added \\clearpage for float overflow")

        # Misplaced alignment tab &
        if "misplaced alignment tab" in err_lower:
            # Usually from & outside tabular — escape stray &
            pass  # Hard to auto-fix without context

    return fixed, fixes


def _parse_log(log_text: str) -> tuple[list[str], list[str]]:
    """Parse pdflatex log output for errors and warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    for line in log_text.split("\n"):
        line_stripped = line.strip()
        if line_stripped.startswith("!"):
            errors.append(line_stripped)
        elif "LaTeX Warning:" in line_stripped:
            warnings.append(line_stripped)
        elif "Undefined control sequence" in line_stripped:
            errors.append(line_stripped)
        elif "Missing" in line_stripped and "inserted" in line_stripped:
            errors.append(line_stripped)
        elif "File" in line_stripped and "not found" in line_stripped:
            errors.append(line_stripped)

    return errors, warnings


@dataclass
class QualityCheckResult:
    """Results of post-compilation quality checks."""

    unresolved_refs: list[str] = field(default_factory=list)
    unresolved_cites: list[str] = field(default_factory=list)
    overfull_hboxes: list[str] = field(default_factory=list)
    underfull_hboxes: list[str] = field(default_factory=list)
    page_count: int = 0
    orphan_figures: list[str] = field(default_factory=list)
    orphan_labels: list[str] = field(default_factory=list)
    warnings_summary: list[str] = field(default_factory=list)

    @property
    def has_critical_issues(self) -> bool:
        return bool(self.unresolved_refs or self.unresolved_cites)


def check_compiled_quality(
    tex_path: Path,
    *,
    page_limit: int = 10,
) -> QualityCheckResult:
    """Run post-compilation quality checks on a LaTeX document.

    Parses the .log file and .tex source for:
    - Unresolved references (??)
    - Unresolved citations
    - Overfull/underfull hboxes
    - Page count vs limit
    - Orphan figures (defined but never referenced, or vice versa)
    """
    result = QualityCheckResult()
    work_dir = tex_path.parent
    stem = tex_path.stem

    # --- Parse .log file ---
    log_path = work_dir / f"{stem}.log"
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        for line in log_text.split("\n"):
            line_s = line.strip()
            # Unresolved references
            if "LaTeX Warning: Reference" in line_s and "undefined" in line_s:
                result.unresolved_refs.append(line_s)
            # Unresolved citations
            if "LaTeX Warning: Citation" in line_s and "undefined" in line_s:
                result.unresolved_cites.append(line_s)
            # Overfull hboxes (only flag significant ones > 1pt)
            if "Overfull \\hbox" in line_s:
                m = re.search(r"(\d+\.?\d*)pt", line_s)
                if m and float(m.group(1)) > 1.0:
                    result.overfull_hboxes.append(line_s)
            # Underfull hboxes (badness >= 5000)
            if "Underfull \\hbox" in line_s and "badness" in line_s:
                m = re.search(r"badness (\d+)", line_s)
                if m and int(m.group(1)) >= 5000:
                    result.underfull_hboxes.append(line_s)

    # --- Count pages from .aux or .log ---
    aux_path = work_dir / f"{stem}.aux"
    if aux_path.exists():
        aux_text = aux_path.read_text(encoding="utf-8", errors="replace")
        # Look for \newlabel{LastPage}{{N}{...}}
        m = re.search(r"\\newlabel\{LastPage\}\{\{(\d+)\}", aux_text)
        if m:
            result.page_count = int(m.group(1))
    if result.page_count == 0 and log_path.exists():
        # Fallback: count "Output written on ... (N pages)"
        m = re.search(r"Output written on .* \((\d+) page", log_text)
        if m:
            result.page_count = int(m.group(1))

    # --- Cross-reference validation ---
    tex_text = tex_path.read_text(encoding="utf-8", errors="replace")
    # Find all \label{fig:X}
    fig_labels = set(re.findall(r"\\label\{(fig:[^}]+)\}", tex_text))
    # Find all \ref{fig:X}
    fig_refs = set(re.findall(r"\\ref\{(fig:[^}]+)\}", tex_text))
    # Orphan labels (defined but never referenced)
    result.orphan_labels = sorted(fig_labels - fig_refs)
    # Orphan references (referenced but never defined)
    result.orphan_figures = sorted(fig_refs - fig_labels)

    # --- Build warnings summary ---
    if result.unresolved_refs:
        result.warnings_summary.append(
            f"{len(result.unresolved_refs)} unresolved reference(s)"
        )
    if result.unresolved_cites:
        result.warnings_summary.append(
            f"{len(result.unresolved_cites)} unresolved citation(s)"
        )
    if result.overfull_hboxes:
        result.warnings_summary.append(
            f"{len(result.overfull_hboxes)} overfull hbox(es) > 1pt"
        )
    if result.page_count > page_limit:
        result.warnings_summary.append(
            f"Page count {result.page_count} exceeds limit {page_limit}"
        )
    if result.orphan_figures:
        result.warnings_summary.append(
            f"{len(result.orphan_figures)} referenced but undefined figure(s): "
            + ", ".join(result.orphan_figures[:3])
        )
    if result.orphan_labels:
        result.warnings_summary.append(
            f"{len(result.orphan_labels)} defined but unreferenced figure(s): "
            + ", ".join(result.orphan_labels[:3])
        )

    return result


def _run_bibtex(work_dir: Path, stem: str, timeout: int = 60) -> bool:
    """Run bibtex if the binary exists. Returns True on success."""
    if not shutil.which("bibtex"):
        return False
    try:
        proc = subprocess.run(
            ["bibtex", stem],
            cwd=work_dir,
            capture_output=True,
            timeout=timeout,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
