"""
ProjectScanner — Deep analysis of project folders for ScholarLab v2.0.

Scans a workspace directory and produces a structured ProjectScanResult
with paper status, experiment status, data status, and literature status.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ── File extension sets ──────────────────────────────────────────────────────

_LATEX_EXT = {".tex", ".bib", ".sty", ".cls", ".bst"}
_CODE_EXT = {".py", ".m", ".ipynb", ".r", ".jl", ".sh", ".cpp", ".c", ".h"}
_DATA_EXT = {".mat", ".csv", ".tsv", ".json", ".npz", ".npy", ".hdf5", ".h5", ".xlsx"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".eps"}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "scholar_output", "latex_input", ".idea", ".vscode"}


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class TexSection:
    level: str          # "section" | "subsection" | "subsubsection"
    title: str
    line_number: int
    content_lines: int  # non-empty lines in this section
    has_todo: bool
    is_empty: bool      # fewer than 3 non-empty content lines


@dataclass
class PaperStatus:
    main_tex: str                     # filename of the main .tex
    tex_files: list[str]              # all .tex/.bib files found
    sections: list[TexSection]
    total_lines: int
    total_content_lines: int          # non-empty, non-comment lines
    citation_count: int               # \cite{} occurrences
    bib_entry_count: int              # @article/@inproceedings/etc entries in .bib
    completeness_pct: int             # 0-100, estimated
    empty_sections: list[str]         # section titles that appear empty
    todo_sections: list[str]          # sections containing TODO markers


@dataclass
class CodeFileInfo:
    path: str
    lines: int
    imports: list[str]                # detected library imports
    has_main: bool                    # contains if __name__ == "__main__" or def main
    has_train: bool                   # contains train/fit/forward patterns
    has_test: bool                    # contains test/eval/predict patterns


@dataclass
class ExperimentStatus:
    code_files: list[CodeFileInfo]
    total_code_lines: int
    frameworks: list[str]             # e.g. ["pytorch", "sklearn", "tensorflow"]
    has_training_code: bool
    has_test_code: bool
    has_main_entry: bool
    result_files: list[str]           # files that look like experiment results


@dataclass
class DataFileInfo:
    path: str
    size_mb: float
    extension: str
    columns: list[str] | None        # for CSV files, first row headers


@dataclass
class DataStatus:
    files: list[DataFileInfo]
    total_size_mb: float
    image_count: int
    image_files: list[str]


@dataclass
class LiteratureStatus:
    bib_files: list[str]
    bib_entry_count: int
    pdf_files: list[str]
    pdf_count: int


@dataclass
class ProjectScanResult:
    workspace_dir: str
    paper: PaperStatus | None
    experiment: ExperimentStatus
    data: DataStatus
    literature: LiteratureStatus
    summary_text: str                 # human-readable summary for LLM & UI

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict."""
        d: dict = {
            "workspace_dir": self.workspace_dir,
            "paper": _paper_to_dict(self.paper) if self.paper else None,
            "experiment": _experiment_to_dict(self.experiment),
            "data": _data_to_dict(self.data),
            "literature": _literature_to_dict(self.literature),
            "summary_text": self.summary_text,
        }
        return d


# ── Serialization helpers ────────────────────────────────────────────────────

def _paper_to_dict(p: PaperStatus) -> dict:
    return {
        "main_tex": p.main_tex,
        "tex_files": p.tex_files,
        "sections": [
            {
                "level": s.level,
                "title": s.title,
                "line_number": s.line_number,
                "content_lines": s.content_lines,
                "has_todo": s.has_todo,
                "is_empty": s.is_empty,
            }
            for s in p.sections
        ],
        "total_lines": p.total_lines,
        "total_content_lines": p.total_content_lines,
        "citation_count": p.citation_count,
        "bib_entry_count": p.bib_entry_count,
        "completeness_pct": p.completeness_pct,
        "empty_sections": p.empty_sections,
        "todo_sections": p.todo_sections,
    }


def _experiment_to_dict(e: ExperimentStatus) -> dict:
    return {
        "code_files": [
            {
                "path": c.path,
                "lines": c.lines,
                "imports": c.imports,
                "has_main": c.has_main,
                "has_train": c.has_train,
                "has_test": c.has_test,
            }
            for c in e.code_files
        ],
        "total_code_lines": e.total_code_lines,
        "frameworks": e.frameworks,
        "has_training_code": e.has_training_code,
        "has_test_code": e.has_test_code,
        "has_main_entry": e.has_main_entry,
        "result_files": e.result_files,
    }


def _data_to_dict(d: DataStatus) -> dict:
    return {
        "files": [
            {
                "path": f.path,
                "size_mb": round(f.size_mb, 2),
                "extension": f.extension,
                "columns": f.columns,
            }
            for f in d.files
        ],
        "total_size_mb": round(d.total_size_mb, 2),
        "image_count": d.image_count,
        "image_files": d.image_files[:20],
    }


def _literature_to_dict(lit: LiteratureStatus) -> dict:
    return {
        "bib_files": lit.bib_files,
        "bib_entry_count": lit.bib_entry_count,
        "pdf_files": lit.pdf_files[:20],
        "pdf_count": lit.pdf_count,
    }


# ── Core scanning logic ─────────────────────────────────────────────────────

def scan_project(workspace_dir: str, main_tex_hint: str = "") -> ProjectScanResult:
    """Perform a deep scan of a project workspace directory."""
    root = Path(workspace_dir)
    if not root.exists() or not root.is_dir():
        return ProjectScanResult(
            workspace_dir=workspace_dir,
            paper=None,
            experiment=ExperimentStatus([], 0, [], False, False, False, []),
            data=DataStatus([], 0.0, 0, []),
            literature=LiteratureStatus([], 0, [], 0),
            summary_text="目录不存在或无法访问。",
        )

    tex_files: list[Path] = []
    bib_files: list[Path] = []
    code_files: list[Path] = []
    data_files: list[Path] = []
    pdf_files: list[Path] = []
    image_files: list[Path] = []
    result_files: list[Path] = []

    for item in root.rglob("*"):
        if not item.is_file():
            continue
        rel_parts = item.relative_to(root).parts[:-1]
        if set(rel_parts) & _SKIP_DIRS:
            continue
        if any(p.startswith(".") for p in rel_parts):
            continue

        ext = item.suffix.lower()
        if ext == ".tex":
            tex_files.append(item)
        elif ext == ".bib":
            bib_files.append(item)
        elif ext in _LATEX_EXT:
            pass  # .sty/.cls/.bst — tracked but not primary
        elif ext in _CODE_EXT:
            code_files.append(item)
        elif ext in _DATA_EXT:
            data_files.append(item)
        elif ext == ".pdf":
            pdf_files.append(item)
        elif ext in _IMAGE_EXT:
            image_files.append(item)

        name_lower = item.name.lower()
        if any(kw in name_lower for kw in ("result", "output", "log", "metric", "score")):
            if ext in {".json", ".csv", ".txt", ".log"}:
                result_files.append(item)

    paper = _analyze_paper(tex_files, bib_files, root, main_tex_hint)
    experiment = _analyze_code(code_files, result_files, root)
    data = _analyze_data(data_files, image_files, root)
    literature = _analyze_literature(bib_files, pdf_files, root)

    summary = _build_summary(paper, experiment, data, literature, root)

    return ProjectScanResult(
        workspace_dir=workspace_dir,
        paper=paper,
        experiment=experiment,
        data=data,
        literature=literature,
        summary_text=summary,
    )


# ── Paper analysis ───────────────────────────────────────────────────────────

_SECTION_RE = re.compile(
    r"\\(section|subsection|subsubsection)\*?\{([^}]*)\}",
    re.IGNORECASE,
)
_CITE_RE = re.compile(r"\\cite[tp]?\*?\{[^}]+\}")
_TODO_RE = re.compile(r"\bTODO\b|\bFIXME\b|\bXXX\b|\bHACK\b|\\todo\b", re.IGNORECASE)
_BIB_ENTRY_RE = re.compile(r"^@\w+\{", re.MULTILINE)


def _analyze_paper(
    tex_files: list[Path],
    bib_files: list[Path],
    root: Path,
    main_tex_hint: str,
) -> PaperStatus | None:
    if not tex_files:
        return None

    main_tex = _find_main_tex(tex_files, main_tex_hint)
    if main_tex is None and tex_files:
        main_tex = max(tex_files, key=lambda f: f.stat().st_size)

    all_tex_names = [str(f.relative_to(root)) for f in tex_files]
    all_tex_names += [str(f.relative_to(root)) for f in bib_files]

    try:
        content = main_tex.read_text(encoding="utf-8", errors="replace")
    except Exception:
        content = ""

    lines = content.split("\n")
    total_lines = len(lines)
    content_lines = [
        ln for ln in lines
        if ln.strip() and not ln.strip().startswith("%")
    ]
    total_content_lines = len(content_lines)

    sections = _parse_sections(content)
    citation_count = len(_CITE_RE.findall(content))

    bib_entry_count = 0
    for bf in bib_files:
        try:
            bib_text = bf.read_text(encoding="utf-8", errors="replace")
            bib_entry_count += len(_BIB_ENTRY_RE.findall(bib_text))
        except Exception:
            pass

    empty_sections = [s.title for s in sections if s.is_empty]
    todo_sections = [s.title for s in sections if s.has_todo]

    completeness = _estimate_completeness(sections, total_content_lines, citation_count)

    return PaperStatus(
        main_tex=main_tex.name if main_tex else "",
        tex_files=all_tex_names,
        sections=sections,
        total_lines=total_lines,
        total_content_lines=total_content_lines,
        citation_count=citation_count,
        bib_entry_count=bib_entry_count,
        completeness_pct=completeness,
        empty_sections=empty_sections,
        todo_sections=todo_sections,
    )


def _find_main_tex(tex_files: list[Path], hint: str) -> Path | None:
    if hint:
        for tf in tex_files:
            if tf.name == hint or str(tf).endswith(hint):
                return tf
    for tf in tex_files:
        if tf.stem.lower() in ("main", "paper", "manuscript", "thesis"):
            return tf
    candidates = [tf for tf in tex_files if tf.suffix == ".tex"]
    for tf in candidates:
        try:
            head = tf.read_text(encoding="utf-8", errors="replace")[:2000]
            if "\\documentclass" in head:
                return tf
        except Exception:
            pass
    return None


def _parse_sections(content: str) -> list[TexSection]:
    lines = content.split("\n")
    raw_matches: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        m = _SECTION_RE.search(line)
        if m:
            raw_matches.append((i, m.group(1).lower(), m.group(2).strip()))

    sections: list[TexSection] = []
    for idx, (line_no, level, title) in enumerate(raw_matches):
        next_line = raw_matches[idx + 1][0] if idx + 1 < len(raw_matches) else len(lines)
        block = lines[line_no + 1 : next_line]
        content_block = [
            ln for ln in block
            if ln.strip() and not ln.strip().startswith("%")
        ]
        has_todo = any(_TODO_RE.search(ln) for ln in block)
        is_empty = len(content_block) < 3

        sections.append(TexSection(
            level=level,
            title=title,
            line_number=line_no + 1,
            content_lines=len(content_block),
            has_todo=has_todo,
            is_empty=is_empty,
        ))

    return sections


def _estimate_completeness(
    sections: list[TexSection],
    total_content_lines: int,
    citation_count: int,
) -> int:
    if not sections:
        if total_content_lines > 50:
            return 30
        return 10

    filled = sum(1 for s in sections if not s.is_empty)
    total = len(sections)
    section_ratio = filled / total if total > 0 else 0

    expected_sections = {"introduction", "method", "experiment", "result",
                         "conclusion", "related work", "abstract", "discussion"}
    found_standard = sum(
        1 for s in sections
        if any(kw in s.title.lower() for kw in expected_sections)
    )
    structure_score = min(found_standard / 5, 1.0)

    content_score = min(total_content_lines / 300, 1.0)
    cite_score = min(citation_count / 15, 1.0)

    pct = int(
        section_ratio * 35
        + structure_score * 25
        + content_score * 25
        + cite_score * 15
    )
    return max(0, min(100, pct))


# ── Code analysis ────────────────────────────────────────────────────────────

_FRAMEWORK_PATTERNS: dict[str, list[str]] = {
    "pytorch": ["import torch", "from torch"],
    "tensorflow": ["import tensorflow", "from tensorflow"],
    "sklearn": ["import sklearn", "from sklearn"],
    "jax": ["import jax", "from jax"],
    "keras": ["import keras", "from keras"],
    "numpy": ["import numpy", "from numpy"],
    "scipy": ["import scipy", "from scipy"],
    "pandas": ["import pandas", "from pandas"],
    "matplotlib": ["import matplotlib", "from matplotlib"],
    "opencv": ["import cv2", "from cv2"],
    "transformers": ["import transformers", "from transformers"],
    "matlab": ["matlab.engine"],
}

_TRAIN_PATTERNS = re.compile(
    r"\bdef\s+train\b|\bmodel\.train\b|\.fit\(|\.forward\(|optimizer\.step\(|loss\.backward\(",
    re.IGNORECASE,
)
_TEST_PATTERNS = re.compile(
    r"\bdef\s+test\b|\bdef\s+eval\b|\bmodel\.eval\b|\.predict\(|\.evaluate\(",
    re.IGNORECASE,
)
_MAIN_PATTERNS = re.compile(
    r'if\s+__name__\s*==\s*["\']__main__["\']|^def\s+main\s*\(',
    re.MULTILINE,
)


def _analyze_code(
    code_files: list[Path],
    result_files: list[Path],
    root: Path,
) -> ExperimentStatus:
    infos: list[CodeFileInfo] = []
    all_frameworks: set[str] = set()
    has_training = False
    has_test = False
    has_main = False
    total_lines = 0

    for cf in code_files:
        try:
            text = cf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        lines = text.split("\n")
        line_count = len(lines)
        total_lines += line_count

        imports: list[str] = []
        for fw_name, patterns in _FRAMEWORK_PATTERNS.items():
            if any(p in text for p in patterns):
                imports.append(fw_name)
                all_frameworks.add(fw_name)

        file_has_train = bool(_TRAIN_PATTERNS.search(text))
        file_has_test = bool(_TEST_PATTERNS.search(text))
        file_has_main = bool(_MAIN_PATTERNS.search(text))

        if file_has_train:
            has_training = True
        if file_has_test:
            has_test = True
        if file_has_main:
            has_main = True

        infos.append(CodeFileInfo(
            path=str(cf.relative_to(root)),
            lines=line_count,
            imports=imports,
            has_main=file_has_main,
            has_train=file_has_train,
            has_test=file_has_test,
        ))

    return ExperimentStatus(
        code_files=infos,
        total_code_lines=total_lines,
        frameworks=sorted(all_frameworks),
        has_training_code=has_training,
        has_test_code=has_test,
        has_main_entry=has_main,
        result_files=[str(r.relative_to(root)) for r in result_files],
    )


# ── Data analysis ────────────────────────────────────────────────────────────

def _analyze_data(
    data_files: list[Path],
    image_files: list[Path],
    root: Path,
) -> DataStatus:
    infos: list[DataFileInfo] = []
    total_size = 0.0

    for df in data_files:
        try:
            size = df.stat().st_size
        except Exception:
            size = 0
        size_mb = size / (1024 * 1024)
        total_size += size_mb

        columns: list[str] | None = None
        if df.suffix.lower() == ".csv":
            columns = _read_csv_headers(df)

        infos.append(DataFileInfo(
            path=str(df.relative_to(root)),
            size_mb=size_mb,
            extension=df.suffix.lower(),
            columns=columns,
        ))

    return DataStatus(
        files=infos,
        total_size_mb=total_size,
        image_count=len(image_files),
        image_files=[str(f.relative_to(root)) for f in image_files],
    )


def _read_csv_headers(path: Path) -> list[str] | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first_line = f.readline().strip()
        if first_line:
            return [h.strip().strip('"') for h in first_line.split(",")][:20]
    except Exception:
        pass
    return None


# ── Literature analysis ──────────────────────────────────────────────────────

def _analyze_literature(
    bib_files: list[Path],
    pdf_files: list[Path],
    root: Path,
) -> LiteratureStatus:
    entry_count = 0
    for bf in bib_files:
        try:
            text = bf.read_text(encoding="utf-8", errors="replace")
            entry_count += len(_BIB_ENTRY_RE.findall(text))
        except Exception:
            pass

    return LiteratureStatus(
        bib_files=[str(f.relative_to(root)) for f in bib_files],
        bib_entry_count=entry_count,
        pdf_files=[str(f.relative_to(root)) for f in pdf_files],
        pdf_count=len(pdf_files),
    )


# ── Summary builder ──────────────────────────────────────────────────────────

def _build_summary(
    paper: PaperStatus | None,
    experiment: ExperimentStatus,
    data: DataStatus,
    literature: LiteratureStatus,
    root: Path,
) -> str:
    parts: list[str] = [f"项目目录: {root}\n"]

    if paper:
        parts.append(f"【论文状态】 完成度约 {paper.completeness_pct}%")
        parts.append(f"  主文件: {paper.main_tex} ({paper.total_lines} 行, "
                     f"{paper.total_content_lines} 行有效内容)")
        parts.append(f"  章节数: {len(paper.sections)}, "
                     f"引用数: {paper.citation_count}, "
                     f"参考文献条目: {paper.bib_entry_count}")
        if paper.empty_sections:
            parts.append(f"  空章节: {', '.join(paper.empty_sections)}")
        if paper.todo_sections:
            parts.append(f"  含TODO的章节: {', '.join(paper.todo_sections)}")
    else:
        parts.append("【论文状态】 未发现 .tex 文件")

    parts.append("")

    if experiment.code_files:
        parts.append(f"【实验代码】 {len(experiment.code_files)} 个文件, "
                     f"{experiment.total_code_lines} 行代码")
        if experiment.frameworks:
            parts.append(f"  框架: {', '.join(experiment.frameworks)}")
        flags = []
        if experiment.has_training_code:
            flags.append("训练代码")
        if experiment.has_test_code:
            flags.append("测试代码")
        if experiment.has_main_entry:
            flags.append("主入口")
        if flags:
            parts.append(f"  检测到: {', '.join(flags)}")
        if experiment.result_files:
            parts.append(f"  结果文件: {len(experiment.result_files)} 个")
    else:
        parts.append("【实验代码】 未发现代码文件")

    parts.append("")

    if data.files:
        parts.append(f"【数据文件】 {len(data.files)} 个文件, "
                     f"总计 {data.total_size_mb:.1f} MB")
        if data.image_count > 0:
            parts.append(f"  图片文件: {data.image_count} 个")
    else:
        parts.append("【数据文件】 未发现数据文件")

    parts.append("")

    if literature.bib_entry_count > 0 or literature.pdf_count > 0:
        parts.append(f"【文献资料】 .bib 条目: {literature.bib_entry_count}, "
                     f"PDF 文件: {literature.pdf_count}")
    else:
        parts.append("【文献资料】 未发现文献资料")

    return "\n".join(parts)
