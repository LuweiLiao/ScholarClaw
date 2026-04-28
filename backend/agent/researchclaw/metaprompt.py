"""Kimi-style four-level MetaPrompt overlay (system → domain → project → node).

Files live under ``<base>/.researchclaw/metaprompts/`` or ``<base>/metaprompts/``.
Per-layer files: ``system.yaml``, ``domain.yaml``, ``project.yaml``, ``node.yaml``
or ``nodes/<node_id>.yaml`` for the node tier.

Each file is YAML or JSON mapping optional ``system`` / ``user`` strings that are
appended (in layer order) to :class:`researchclaw.prompts.RenderedPrompt`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from researchclaw.prompts import RenderedPrompt

logger = logging.getLogger(__name__)

META_LAYER_ORDER: Final[tuple[str, ...]] = ("system", "domain", "project", "node")
_METAPROMPT_SUBDIRS: Final[tuple[str, ...]] = (
    ".researchclaw/metaprompts",
    "metaprompts",
)
_SECTION_SEP = "\n\n---\n\n"


def _metaprompt_roots(base: Path) -> tuple[Path, Path]:
    return (base / ".researchclaw" / "metaprompts", base / "metaprompts")


def _parse_payload(raw: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    return data


def _read_dict_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".json",):
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    try:
        return _parse_payload(text)
    except yaml.YAMLError:
        return {}


def _extract_system_user(data: dict[str, Any]) -> tuple[str, str]:
    inner = data.get("meta") if isinstance(data.get("meta"), dict) else data
    if not isinstance(inner, dict):
        return "", ""
    s = inner.get("system")
    u = inner.get("user")
    sys_t = s.strip() if isinstance(s, str) else ""
    usr_t = u.strip() if isinstance(u, str) else ""
    return sys_t, usr_t


def _first_existing_layer_file(root: Path, layer: str, node_id: str | None) -> Path | None:
    if layer == "node" and node_id:
        for base in _metaprompt_roots(root):
            for name in (f"nodes/{node_id}.yaml", f"nodes/{node_id}.yml", f"nodes/{node_id}.json"):
                p = base / name
                if p.is_file():
                    return p
    for base in _metaprompt_roots(root):
        for ext in (".yaml", ".yml", ".json"):
            p = base / f"{layer}{ext}"
            if p.is_file():
                return p
    return None


def _load_layer_slice(root: Path, layer: str, node_id: str | None) -> tuple[str, str, str | None]:
    """Return ``(system_text, user_text, source_path_or_none)`` for one base directory."""
    path = _first_existing_layer_file(root, layer, node_id)
    if path is None:
        return "", "", None
    try:
        data = _read_dict_file(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("MetaPrompt: skip unreadable %s: %s", path, exc)
        return "", "", None
    s, u = _extract_system_user(data)
    return s, u, str(path)


def _merge_base_then_run(
    proj: tuple[str, str],
    run: tuple[str, str],
) -> tuple[str, str]:
    ps, pu = proj
    rs, ru = run

    def _join(a: str, b: str) -> str:
        a, b = a.strip(), b.strip()
        if a and b:
            return f"{a}{_SECTION_SEP}{b}"
        return a or b

    return _join(ps, rs), _join(pu, ru)


@dataclass(frozen=True)
class ResolvedMetaPrompt:
    """Composed overlay for all four layers (project scope merged before run scope)."""

    append_system: str
    append_user: str
    version_hash: str
    """SHA-256 hex of canonical layer payloads (stable for identical files)."""
    sources: tuple[str, ...]
    """Resolved file paths contributing non-empty text, in merge order."""


def _canonical_fingerprint(
    chunks: tuple[tuple[str, str, str], ...],
) -> str:
    """Stable hash from ordered (layer, merged_system, merged_user) entries."""
    payload = [{"layer": a, "system": b, "user": c} for a, b, c in chunks]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_metaprompt_overlay(
    *,
    project_dir: Path | None,
    run_dir: Path | None,
    node_id: str | None = None,
) -> ResolvedMetaPrompt | None:
    """Load MetaPrompt layers from *project_dir* then overlay *run_dir* (run wins per layer).

    Returns ``None`` when no files contribute non-empty ``system``/``user`` text.
    """
    fp_chunks: list[tuple[str, str, str]] = []
    sources: list[str] = []
    layer_system: list[str] = []
    layer_user: list[str] = []

    for layer in META_LAYER_ORDER:
        ps, pu, pp = "", "", None
        rs, ru, rp = "", "", None
        if project_dir is not None:
            ps, pu, pp = _load_layer_slice(Path(project_dir), layer, node_id if layer == "node" else None)
        if run_dir is not None:
            rs, ru, rp = _load_layer_slice(Path(run_dir), layer, node_id if layer == "node" else None)
        merged_s, merged_u = _merge_base_then_run((ps, pu), (rs, ru))
        if pp:
            sources.append(pp)
        if rp and rp != pp:
            sources.append(rp)
        if merged_s or merged_u:
            layer_system.append(merged_s)
            layer_user.append(merged_u)
            fp_chunks.append((layer, merged_s, merged_u))

    if not layer_system and not layer_user:
        return None

    append_system = _SECTION_SEP.join(s for s in layer_system if s.strip())
    append_user = _SECTION_SEP.join(u for u in layer_user if u.strip())
    version_hash = _canonical_fingerprint(tuple(fp_chunks))
    return ResolvedMetaPrompt(
        append_system=append_system,
        append_user=append_user,
        version_hash=version_hash,
        sources=tuple(sources),
    )


def apply_metaprompt_to_rendered(
    base: RenderedPrompt,
    meta: ResolvedMetaPrompt | None,
) -> RenderedPrompt:
    """Append composed MetaPrompt fragments to *base* (no-op when *meta* is None)."""
    if meta is None:
        return base
    sys_t = base.system
    usr_t = base.user
    if meta.append_system.strip():
        sys_t = f"{sys_t}{_SECTION_SEP}{meta.append_system}" if sys_t.strip() else meta.append_system
    if meta.append_user.strip():
        usr_t = f"{usr_t}{_SECTION_SEP}{meta.append_user}" if usr_t.strip() else meta.append_user
    return RenderedPrompt(
        system=sys_t,
        user=usr_t,
        json_mode=base.json_mode,
        max_tokens=base.max_tokens,
        metaprompt_version_hash=meta.version_hash,
    )


def append_metaprompt_version_record(
    run_dir: Path,
    *,
    version_hash: str,
    layers_snapshot: dict[str, Any],
) -> None:
    """Append one JSON line to ``run_dir/.researchclaw/metaprompts/versions.jsonl``."""
    root = Path(run_dir) / ".researchclaw" / "metaprompts"
    root.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"version_hash": version_hash, "snapshot": layers_snapshot},
        ensure_ascii=False,
    )
    with (root / "versions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_metaprompt_versions(run_dir: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    path = Path(run_dir) / ".researchclaw" / "metaprompts" / "versions.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except json.JSONDecodeError:
            continue
    return out
