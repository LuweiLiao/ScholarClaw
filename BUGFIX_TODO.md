# ScholarClaw E2E 测试 Bug 修复表

## 🔴 Critical（完全阻塞用户）

### Bug #1: 前端 Start Task 按钮始终 disabled
**根因**: `globalLLM`（全局模型）和 `layerModels`（各层模型）是两个独立状态。用户填写全局模型后，`layerModels` 仍为空，`Start Task` 的 disabled 条件 `!Object.values(layerModels).some(hasLM)` 始终为 true。
**影响**: 新用户完全无法从前端创建任务。
**修复**: 添加"Apply Global"按钮，或全局模型变化时自动填充到未配置的层。
**文件**: `frontend/src/components/CreateTaskWizard.tsx`

### Bug #2: 前端模型验证 Test 返回 Fail
**根因**: 
- 后端 `_test_model_config` 调用第三方 API，当使用 `https://open.bigmodel.cn/api/paas/v4` 时返回 429 Too Many Requests
- 即使使用 coding endpoint (`/api/coding/paas/v4`)，API 也可能因频繁测试而限流
**影响**: 用户无法确认模型配置是否正确，体验极差。
**修复**: 
1. 统一所有配置文件使用 coding endpoint
2. 添加更友好的错误提示（区分 429/401/网络错误）
3. 添加默认模型模板，减少用户填写负担
**文件**: `backend/services/agent_bridge.py`, `backend/runs/project_configs/*.yaml`

## 🟡 High（频繁导致 pipeline 失败）

### Bug #3: Stage 14 EXPERIMENT_RUN 在 simulated 模式下频繁失败
**根因**: LLM 生成的实验代码有时不输出 `results.json`，导致 `FileNotFoundError`，stage 标记为 FAILED。
**影响**: Pipeline 在 Stage 14 终止，无法继续后续阶段。
**修复**: 当 `results.json` 不存在时，生成一个 synthetic fallback results.json，让 pipeline 能继续。
**文件**: `backend/agent/researchclaw/pipeline/experiment_run/runtime.py`

### Bug #4: Stage 22 LaTeX Unicode 字符编译错误
**根因**: LLM 生成的论文内容包含 Unicode subscript（如 `₀` U+2080），xelatex 编译时报致命错误。
**影响**: 论文导出失败，pipeline 在 Stage 25/26 附近失败。
**修复**: 在 LaTeX 导出前添加 Unicode→LaTeX 转义（如 `₀` → `\textsubscript{0}`）。
**文件**: `backend/agent/researchclaw/pipeline/latex_export/runtime.py` 或相关文件

## 🟠 Medium（影响体验）

### Bug #5: 配置文件 base URL 不一致
**根因**: 部分配置文件使用 `/api/paas/v4`（限流），部分使用 `/api/coding/paas/v4`（正常）。
**影响**: 使用旧配置的用户会遇到 429 错误。
**修复**: 批量替换所有配置文件中的 base URL。
**文件**: `backend/runs/project_configs/*.yaml`

### Bug #6: Event Log 筛选按钮点击超时
**根因**: 点击 Event Log 筛选按钮时，前端可能触发耗时操作或死循环。
**影响**: 前端无响应，需要刷新页面。
**修复**: 待调查具体原因。
**文件**: 待确定

### Bug #7: Open Project 文件夹选择器 disabled
**根因**: `runs/projects` 目录为空，没有可选择的文件夹。
**影响**: 用户无法通过 UI 打开已有项目。
**修复**: 让文件选择器能浏览到 `artifacts/` 目录，或显示已有运行列表。
**文件**: `frontend/src/components/FolderPicker.tsx`
