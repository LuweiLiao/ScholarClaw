"""MetaPrompt overlay: four-layer merge, PromptManager integration, version hash."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from researchclaw.metaprompt import (
    META_LAYER_ORDER,
    ResolvedMetaPrompt,
    apply_metaprompt_to_rendered,
    resolve_metaprompt_overlay,
)
from researchclaw.prompts import PromptManager, RenderedPrompt


def _write_layer(base: Path, layer: str, *, system: str = "", user: str = "") -> Path:
    root = base / ".researchclaw" / "metaprompts"
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{layer}.yaml"
    p.write_text(yaml.dump({"system": system, "user": user}), encoding="utf-8")
    return p


def test_no_overlay_prompt_manager_unchanged() -> None:
    pm = PromptManager()
    sp = pm.for_stage(
        "topic_init",
        topic="T",
        domains="ml",
        project_name="p",
        quality_threshold="0.5",
    )
    sp2 = PromptManager().for_stage(
        "topic_init",
        topic="T",
        domains="ml",
        project_name="p",
        quality_threshold="0.5",
    )
    assert sp == sp2
    assert sp.metaprompt_version_hash is None


def test_four_layer_order_system_and_user(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    for i, layer in enumerate(META_LAYER_ORDER):
        _write_layer(proj, layer, system=f"S-{layer}", user=f"U-{layer}")
    r = resolve_metaprompt_overlay(project_dir=proj, run_dir=None, node_id=None)
    assert r is not None
    ix = r.append_system.index
    assert ix("S-system") < ix("S-domain") < ix("S-project") < ix("S-node")
    iu = r.append_user.index
    assert iu("U-system") < iu("U-domain") < iu("U-project") < iu("U-node")


def test_run_dir_overlays_project_same_layer(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    run = tmp_path / "run"
    proj.mkdir()
    run.mkdir()
    _write_layer(proj, "domain", system="P", user="")
    _write_layer(run, "domain", system="R", user="")
    r = resolve_metaprompt_overlay(project_dir=proj, run_dir=run, node_id=None)
    assert r is not None
    assert "P" in r.append_system and "R" in r.append_system
    assert r.append_system.index("P") < r.append_system.index("R")


def test_node_layer_last_priority(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_layer(proj, "system", system="A", user="")
    _write_layer(proj, "node", system="Z", user="")
    r = resolve_metaprompt_overlay(project_dir=proj, run_dir=None, node_id=None)
    assert r is not None
    assert r.append_system.endswith("Z") or r.append_system.rstrip().endswith("Z")
    assert r.append_system.index("A") < r.append_system.index("Z")


def test_node_specific_file_with_node_id(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = proj / ".researchclaw" / "metaprompts" / "nodes"
    root.mkdir(parents=True)
    (root / "n1.yaml").write_text(yaml.dump({"system": "NODE1", "user": ""}), encoding="utf-8")
    r_none = resolve_metaprompt_overlay(project_dir=proj, run_dir=None, node_id=None)
    assert r_none is None
    r1 = resolve_metaprompt_overlay(project_dir=proj, run_dir=None, node_id="n1")
    assert r1 is not None
    assert "NODE1" in r1.append_system


def test_version_hash_stable(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_layer(proj, "system", system="x", user="y")
    a = resolve_metaprompt_overlay(project_dir=proj, run_dir=None)
    b = resolve_metaprompt_overlay(project_dir=proj, run_dir=None)
    assert a is not None and b is not None
    assert a.version_hash == b.version_hash
    assert len(a.version_hash) == 64


def test_apply_metaprompt_sets_hash_on_rendered() -> None:
    base = RenderedPrompt(system="base-s", user="base-u")
    meta = ResolvedMetaPrompt(
        append_system="M-S",
        append_user="M-U",
        version_hash="a" * 64,
        sources=(),
    )
    out = apply_metaprompt_to_rendered(base, meta)
    assert out.metaprompt_version_hash == meta.version_hash
    assert "M-S" in out.system and "M-U" in out.user


def test_prompt_manager_metaprompt_constructor(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_layer(proj, "project", system="", user="EXTRA")
    meta = resolve_metaprompt_overlay(project_dir=proj, run_dir=None)
    assert meta is not None
    pm = PromptManager(metaprompt=meta)
    sp = pm.for_stage(
        "topic_init",
        topic="T",
        domains="ml",
        project_name="p",
        quality_threshold="0.5",
    )
    assert "EXTRA" in sp.user
    assert sp.metaprompt_version_hash == meta.version_hash


def test_json_layer_file(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    mp = proj / "metaprompts"
    mp.mkdir(parents=True)
    (mp / "system.json").write_text(
        json.dumps({"system": "J", "user": ""}),
        encoding="utf-8",
    )
    r = resolve_metaprompt_overlay(project_dir=proj, run_dir=None)
    assert r is not None
    assert "J" in r.append_system
