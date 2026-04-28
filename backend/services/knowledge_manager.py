"""
Knowledge Manager for ScholarLab v2.0.

Maintains a local knowledge base of all files read, modified, and produced
by the agent pipeline. Entries are stored as JSONL in {workspace}/.scholar/knowledge_base/.
"""

from __future__ import annotations

import json
import time
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path

CATEGORIES = ["literature", "experiment", "code", "paper", "config", "data", "analysis", "other"]

KB_DIR_NAME = "knowledge_base"


@dataclass
class KBEntry:
    id: str
    path: str
    category: str = "other"
    title: str = ""
    summary: str = ""
    source: str = ""
    tags: list[str] = field(default_factory=list)
    timestamp: float = 0.0
    size_bytes: int = 0
    content_hash: str = ""
    agent_id: str = ""
    stage: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class KnowledgeManager:
    """Manages the local knowledge base for a project."""

    def __init__(self) -> None:
        self.entries: dict[str, list[KBEntry]] = {}

    def _kb_dir(self, workspace_dir: str) -> Path:
        d = Path(workspace_dir) / ".scholar" / KB_DIR_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_path(self, workspace_dir: str) -> Path:
        return self._kb_dir(workspace_dir) / "index.jsonl"

    def _load_index(self, workspace_dir: str) -> list[KBEntry]:
        idx_path = self._index_path(workspace_dir)
        if workspace_dir in self.entries:
            return self.entries[workspace_dir]
        entries: list[KBEntry] = []
        if idx_path.exists():
            for line in idx_path.read_text(encoding="utf-8").strip().splitlines():
                try:
                    d = json.loads(line)
                    entries.append(KBEntry(**{k: v for k, v in d.items() if k in KBEntry.__dataclass_fields__}))
                except Exception:
                    pass
        self.entries[workspace_dir] = entries
        return entries

    def _save_index(self, workspace_dir: str) -> None:
        entries = self.entries.get(workspace_dir, [])
        idx_path = self._index_path(workspace_dir)
        lines = [json.dumps(e.to_dict(), ensure_ascii=False) for e in entries]
        idx_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def ingest_file(
        self,
        workspace_dir: str,
        file_path: str,
        source: str = "agent",
        category: str = "",
        agent_id: str = "",
        stage: int = 0,
        summary: str = "",
    ) -> KBEntry:
        """Add or update a file in the knowledge base."""
        entries = self._load_index(workspace_dir)

        fp = Path(file_path)
        rel = str(fp)
        try:
            rel = str(fp.relative_to(workspace_dir))
        except ValueError:
            pass

        h = hashlib.md5(rel.encode()).hexdigest()[:12]
        entry_id = f"kb-{h}"

        if not category:
            category = _auto_categorize(file_path)

        size = 0
        content_hash = ""
        if fp.exists() and fp.is_file():
            size = fp.stat().st_size
            try:
                content_hash = hashlib.md5(fp.read_bytes()).hexdigest()[:16]
            except Exception:
                pass

        existing = next((e for e in entries if e.id == entry_id), None)
        if existing:
            existing.timestamp = time.time()
            existing.size_bytes = size
            existing.content_hash = content_hash
            existing.source = source
            if category:
                existing.category = category
            if agent_id:
                existing.agent_id = agent_id
            if stage:
                existing.stage = stage
            if summary:
                existing.summary = summary
            entry = existing
        else:
            entry = KBEntry(
                id=entry_id,
                path=rel,
                category=category,
                title=fp.stem,
                summary=summary,
                source=source,
                timestamp=time.time(),
                size_bytes=size,
                content_hash=content_hash,
                agent_id=agent_id,
                stage=stage,
            )
            entries.append(entry)

        self._save_index(workspace_dir)
        return entry

    def ingest_diff(
        self,
        workspace_dir: str,
        diff_record: dict,
    ) -> KBEntry:
        """Record a file modification as a KB entry."""
        return self.ingest_file(
            workspace_dir,
            diff_record.get("file", ""),
            source="diff",
            category="paper" if diff_record.get("file", "").endswith(".tex") else "code",
            agent_id=diff_record.get("agentId", ""),
            stage=diff_record.get("stage", 0),
            summary=f"Modified: +{len((diff_record.get('modified', '')).splitlines())} lines",
        )

    def list_entries(self, workspace_dir: str, category: str = "") -> list[dict]:
        entries = self._load_index(workspace_dir)
        if category:
            entries = [e for e in entries if e.category == category]
        return [e.to_dict() for e in sorted(entries, key=lambda e: e.timestamp, reverse=True)]

    def search(self, workspace_dir: str, query: str) -> list[dict]:
        entries = self._load_index(workspace_dir)
        q = query.lower()
        results = []
        for e in entries:
            score = 0
            if q in e.path.lower():
                score += 3
            if q in e.title.lower():
                score += 2
            if q in e.summary.lower():
                score += 1
            if q in " ".join(e.tags).lower():
                score += 1
            if score > 0:
                results.append((score, e))
        results.sort(key=lambda x: (-x[0], -x[1].timestamp))
        return [e.to_dict() for _, e in results[:50]]

    def get_stats(self, workspace_dir: str) -> dict:
        entries = self._load_index(workspace_dir)
        by_cat: dict[str, int] = {}
        for e in entries:
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
        return {
            "total": len(entries),
            "by_category": by_cat,
        }


def _auto_categorize(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in (".tex", ".bib", ".sty", ".cls"):
        return "paper"
    if ext in (".py", ".m", ".ipynb", ".r", ".jl", ".sh", ".cpp", ".c", ".h"):
        return "code"
    if ext in (".pdf",):
        return "literature"
    if ext in (".yaml", ".yml", ".toml", ".ini", ".cfg", ".json"):
        return "config"
    if ext in (".mat", ".csv", ".tsv", ".npz", ".npy", ".hdf5", ".h5", ".xlsx"):
        return "data"
    if ext in (".md", ".txt"):
        return "analysis"
    return "other"
