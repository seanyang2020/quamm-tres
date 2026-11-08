# Quamm RES 实现进度

> 任务：补齐「用 token 把模拟设备注册进来」+ 可复现端到端演示（task_id: t783）

## 状态：已完成（等审）

## 本轮进展

| 项 | 内容 | 状态 |
|---|---|---|
| 设备注册脚本 | `scripts/demo_seed_devices.py`：20 个 mock 端点(7001-7020)，逐个走真实 MCP HTTP 注册 | ✅ |
| 租约演示脚本 | `scripts/demo_lease_flow.py`：whoami → products → devices → apply → my_leases → renew → release → 回池 | ✅ |
| MCP 客户端 | `scripts/mcp_client.py`：JSON-RPC/SSE、token 掩码、token 缓存（0600, gitignore） | ✅ |
| 缺陷修复 | 进程启动未初始化探测后端（恒为 AdbProber 桩 → 全部 pending） | ✅ |
| 缺陷修复 | 重复注册同一设备的自身位置被误判 POSITION_DUP（幂等） | ✅ |
| 配置 | `prober_backend` 一期默认 `http`；`python -m app.seed --rotate` 可签发明文 token | ✅ |
| 文档 | README 新增「模拟演示」一节 | ✅ |

## 验证

- `pytest tests/ -q` → 23 passed（退出码 0）
- 注册脚本两次运行：20 台全部 available；第二次 matched_as=existing，无新增、无报错
- 租约演示两次运行均通过；释放后设备回到可用池
- `/api/dashboard` total_devices=20，by_status={'available': 20}；`/api/devices` 20 条，位置无重复
- 证据详见 `AGENT_BRIDGE_EVIDENCE.md`（task_id: t783）

## 未做

- 未接真实 ADB（二期替换 `app/domain/probe.py` 的 prober 即可）
- 未 push（按任务要求由主控统一推送）
