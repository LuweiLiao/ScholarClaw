# Kimi 增强融入 — 验收清单（人工 + 自动化）

本文档与 `backend/tests/test_kimi_*`、`backend/agent/tests/test_kimi_*` 配套，用于多子任务合并后的统一验收。

## 自动化（后端）

在仓库 `backend` 目录执行：

```bash
cd backend
python -m pytest tests/test_kimi_task_graph_node_lifecycle.py tests/test_kimi_contract_helpers_unit.py -q
python -m pytest tests/test_kimi_integration_red.py -q
python -m pytest agent/tests/test_kimi_metaprompt_overlay_order.py -q
```

或使用仓库根目录：`examples/kimi_run_backend_contracts.ps1`（Windows）。

**当前预期（示例一次完整跑完）：约 11 通过、3 失败** — 失败项即为 RED 契约，合并实现后应变 GREEN。

- **GREEN（应通过）**：`test_kimi_task_graph_node_lifecycle.py`、`test_kimi_contract_helpers_unit.py`；`test_kimi_metaprompt_overlay_order.py` 中 `TestMetapromptOverlayOrderGreen`（进化 overlay 在人体反馈之前）。
- **RED（合并相关 PR 前应失败）**：`test_kimi_integration_red.py` — `Task` 尚未含 `stage_from`/`stage_to`；尚无 `services/node_detail.py`；`TestMetapromptOverlayContractRed` — `PromptManager.for_stage` 尚无 `meta_prompt_overlay`。

合并相关 PR 后，RED 应逐项变为 GREEN；若暂时保留 RED，在 PR 描述中注明「契约测试待实现」。

## 前端协议类型（_ws / UI）

最小约定（供前后端对齐；具体 TypeScript 定义由前端子任务落地）：

| 方向 | 类型 / 主题 | 验证点 |
|------|----------------|--------|
| 服务端 → 客户端 | `task_graph_update` | `payload` 含 `projectId`、`nodes`；节点含 `status`、`stage_from`、`stage_to` |
| 服务端 → 客户端 | `agent_update` / `log` | run/pause/stop 后状态与日志一致 |
| 客户端 → 服务端 | 控制指令（以实际 bridge 为准） | run / pause / retry / skip / rollback / open detail 能触发对应 graph/agent 行为 |

人工验收时打开开发者工具 WebSocket 帧，确认节点控制与 graph 广播一致。

## 人工 UI 验收（ScholarLab / Lobster 前端）

下列步骤在集成环境逐项勾选：

1. **任务图 / 节点**
   - 启动 **run**：节点进入 running，日志出现阶段范围提示（与节点 `stage_from`–`stage_to` 一致）。
   - **pause**：进程或调度暂停，UI 状态与日志一致。
   - **retry**：失败或中止后可从节点重试，状态回到 pending/ready 再运行。
   - **skip**：依赖允许时节点标记 skipped，下游节点可变为 ready。
   - **rollback**（若已实现）：回退 checkpoint / 节点产物符合产品定义。
   - **detail**：节点详情展示 **阶段范围汇总**（起止 stage、覆盖数量或标签列表）。

2. **Prompt 标签页**
   - 展示当前阶段或聚合 prompt；MetaPrompt 注入后，文案顺序符合：**基础用户 prompt → 进化 lessons（若有）→ MetaPrompt（若有）→ 人体反馈块（若有）**。

3. **日志与产物**
   - 日志面板可滚动查看 agent 日志路径对应输出。
   - 产物预览：至少能打开当前 stage 目录下代表性文件或摘要。

## 与其他子 Task 的合并顺序建议

1. **TaskGraph / planner**：合并后 RED 中的 `Task.stage_from`/`stage_to` 应由 agent_bridge 填充；运行 `test_kimi_integration_red.py` 第一条。
2. **agent_bridge / executor**：确认 `researchclaw run --from-stage/--to-stage` 来自 TaskNode，而非仅用 layer 默认范围；可辅以 `kimi_contract_helpers.argv_has_stage_range` 做单元断言。
3. **node detail**：新增 `node_detail`（或约定模块名）后，第二条 RED 变 GREEN。
4. **MetaPrompt**：实现 `meta_prompt_overlay` 后，MetaPrompt RED 变 GREEN；executor 中调用顺序与 `PromptManager` 保持一致。
5. **前端**：对齐 WebSocket payload 与按钮；人工按上表走一遍。

## Merge-back / 清理

本任务在独立 git worktree 中开发时，合并回主工作树使用 Cursor 的 `/apply-worktree`；清理使用 `/delete-worktree`（参见项目命令说明）。
