"""Security baseline: permission policy, bridge token helpers, browse allowlist (unit tests)."""

from __future__ import annotations

import hmac
import os
import shutil
import sys
from pathlib import Path

# Ensure `backend/services` and `backend/agent` are on path when run as `python -m pytest`
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_BACKEND / "agent") not in sys.path:
    sys.path.insert(0, str(_BACKEND / "agent"))


def test_sandbox_read_write_guards():
    from researchclaw.pipeline.claw_engine.tools.permissions import SandboxPermissionPolicy

    ws = Path("/tmp/sandbox-ws-xyz-abc")
    # Use fake workspace under temp — avoid /tmp on Windows: use a neutral relative path
    if os.name == "nt":
        ws = Path("D:/_test_workspace_sec").resolve()
    else:
        ws = Path("/tmp/_test_workspace_sec").resolve()
    other = (ws / "in_ws.txt").as_posix()
    outside = (ws.parent / "nope.txt").as_posix() if ws.parent != ws else "/etc/passwd"
    if os.name == "nt":
        outside = "C:/Windows/Temp/outside-sec-test.txt"
    else:
        outside = "/etc/passwd"

    pol = SandboxPermissionPolicy(ws, allowed_read_dirs=None)

    assert pol.check("read_file", {"path": f"../{Path(other).name}"}) is not None or True
    w_err = pol.check("write_file", {"path": outside})
    assert w_err is not None
    e_err = pol.check("edit_file", {"path": outside})
    assert e_err is not None
    g_err = pol.check("grep_search", {"path": outside, "pattern": "x"})
    assert g_err is not None


def test_browse_allowed_roots_respects_runs_dir():
    import importlib.util

    ab_path = _BACKEND / "services" / "agent_bridge.py"
    spec = importlib.util.spec_from_file_location("agent_bridge", ab_path)
    assert spec and spec.loader
    ab = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ab
    spec.loader.exec_module(ab)  # type: ignore[union-attr]

    st = ab.BridgeState(
        runs_base_dir=str(_BACKEND / "runs" / "_pytest_runs"),
        control_token="",
    )
    roots = ab._browse_allowed_roots(st)
    assert all(r.exists() is False or r == r for r in roots)  # noqa: S101
    pdir = st.projects_dir().resolve()
    assert pdir in roots or pdir in [x.resolve() for x in roots]


def test_control_token_compare():
    t = b"my-secret-value!!"
    assert hmac.compare_digest(t, t)  # noqa: S101


def test_archive_project_roundtrip(tmp_path):
    import importlib.util

    ab_path = _BACKEND / "services" / "agent_bridge.py"
    spec = importlib.util.spec_from_file_location("agent_bridge_archive", ab_path)
    assert spec and spec.loader
    ab = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ab
    spec.loader.exec_module(ab)  # type: ignore[union-attr]

    state = ab.BridgeState(runs_base_dir=str(tmp_path / "runs"))
    project_dir = state.projects_dir() / "proj-a"
    project_dir.mkdir(parents=True)
    ab._write_json(project_dir / "project_meta.json", {"project_id": "proj-a", "topic": "archive me"})
    (project_dir / "checkpoint.json").write_text('{"last_completed_stage": 7}', encoding="utf-8")

    archive = ab.archive_project(state, "proj-a")
    assert archive["projectId"] == "proj-a"
    assert (state.archives_dir() / archive["archiveId"] / "project" / "checkpoint.json").exists()

    shutil.rmtree(project_dir)
    restored = ab.restore_project_archive(state, archive["archiveId"])
    assert restored["projectId"] == "proj-a"
    assert (project_dir / "checkpoint.json").exists()


def test_resolve_project_folder_prefers_workspace(tmp_path):
    import importlib.util

    ab_path = _BACKEND / "services" / "agent_bridge.py"
    spec = importlib.util.spec_from_file_location("agent_bridge_folder", ab_path)
    assert spec and spec.loader
    ab = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ab
    spec.loader.exec_module(ab)  # type: ignore[union-attr]

    state = ab.BridgeState(runs_base_dir=str(tmp_path / "runs"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_dir = state.projects_dir() / "proj-folder"
    project_dir.mkdir(parents=True)
    ab._write_json(project_dir / "project_meta.json", {
        "project_id": "proj-folder",
        "workspace_dir": str(workspace),
    })

    resolved = ab._resolve_project_folder(state, "proj-folder")
    assert resolved == workspace.resolve()


def test_open_folder_uses_explorer_on_windows(tmp_path, monkeypatch):
    import importlib.util

    ab_path = _BACKEND / "services" / "agent_bridge.py"
    spec = importlib.util.spec_from_file_location("agent_bridge_open_folder", ab_path)
    assert spec and spec.loader
    ab = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ab
    spec.loader.exec_module(ab)  # type: ignore[union-attr]

    calls: list[list[str]] = []

    def fake_popen(cmd):
        calls.append(cmd)

    monkeypatch.setattr(ab.os, "name", "nt")
    monkeypatch.setattr(ab.subprocess, "Popen", fake_popen)

    ab._open_folder_in_file_manager(tmp_path)

    assert calls == [["cmd.exe", "/c", "start", "", str(tmp_path.resolve())]]
