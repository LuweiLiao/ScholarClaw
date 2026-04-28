# Kimi 后端契约测试 — 从仓库根目录或 backend 目录均可调用。
# 用法 (PowerShell):  .\examples\kimi_run_backend_contracts.ps1
$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Backend = Join-Path $RepoRoot "backend" | Resolve-Path
Push-Location $Backend
try {
    python -m pytest `
        tests/test_kimi_task_graph_node_lifecycle.py `
        tests/test_kimi_integration_red.py `
        agent/tests/test_kimi_metaprompt_overlay_order.py `
        -q --tb=short
} finally {
    Pop-Location
}
