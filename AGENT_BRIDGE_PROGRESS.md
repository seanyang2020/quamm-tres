# t1043 P0 probe source guard — 进度

> 任务：禁止 mock 静默落库 / source 可审计  
> task_id: t1043  
> 工作区：`/home/scm/worktree/tres-probe-source-guard`  
> 分支：`feat/probe-source-guard`（基于 41ae518）

## 状态：已完成（停住等审）

## 本轮工作

| 项 | 内容 | 状态 |
|---|---|---|
| ProbeSource 常量 | `ALL_SOURCES` / `PRODUCTION_SOURCES` | ✅ |
| 配置默认 | `probe_backend=real`，`allow_mock_probe=False` | ✅ |
| 运行时键 | `allow_mock_probe` bool | ✅ |
| build_prober | 未知值 ValueError，不再回退 mock | ✅ |
| catalog 闸门 | `_resolve_probe_source`；probe/register 封口 | ✅ |
| 模型/迁移 | 默认 real；`simulated` 派生；空值→unknown | ✅ |
| 公开视图/MCP/UI | source+simulated；docstring；去掉 mock 选项 | ✅ |
| 单测 | `test_probe_source_guard.py` + conftest/patrol 适配 | ✅ |
| 提交/部署 | 未 commit / 未 push / 未部署 | ✅ |

## 关键设计

- **校验放在 catalog 入口**（`device_probe` / `device_register`），不封死 `get_prober_for("mock")`，故既有 mock 设备巡检仍可用。
- **mock 仅当 `allow_mock_probe=True`**；生产默认拒绝，错误码 `MOCK_PROBE_DISABLED`。

## 风险 / 未完成

- 已有库 `device.source` 列若早已以 `DEFAULT 'mock'` 建好，SQLite 无法在线改列默认；仅影响「将来 ADD COLUMN」路径与 ORM 默认。
- 未批量改写既有 `source=mock` 行（按 P0 要求）。
- 全量 17 个既有失败（flash/registration/products/queue/mcp_http）未在本任务修复。
