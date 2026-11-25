---
task_id: t1060
---

# t1060：COMRes 心跳间隔以节点上报为准（0.54.2）

## a. 编译/构建

- 命令：`timeout 30s /home/scm/github_gitee/quamm-res/.venv/bin/python -m py_compile app/config.py app/domain/comres.py app/domain/comres_prune.py app/domain/wirelog.py app/workers/main.py tests/test_comres_heartbeat.py`
- 退出码：`0`
- 关键结果：语法编译通过（无独立前端构建步骤）

- 命令：`timeout 180s /home/scm/github_gitee/quamm-res/.venv/bin/python -m pytest tests/test_comres*.py tests/test_wirelog*.py -q --tb=line`
- 退出码：`0`
- 关键结果：`65 passed in 14.34s`（含新增 `tests/test_comres_heartbeat.py` 8 例）

## b. 改动文件清单

- `VERSION` — 0.54.1 → 0.54.2
- `CHANGELOG.md` — 0.54.2 中文说明
- `app/config.py` — `comres_prune_interval_s=60`；`comres_stale_floor_s=180`
- `app/domain/comres.py` — 解析/持久化节点心跳间隔；expire/probe 按节点超时
- `app/domain/comres_prune.py` — `node_heartbeat_interval_s` / `node_stale_timeout_s` / `node_effectively_online`；audit 逐节点
- `app/domain/wirelog.py` — `_node_brief` 只读 stale + age/interval/timeout 字段
- `app/workers/main.py` — prune scheduler 默认 60、最小 30
- `web/src/App.vue` — 心跳年龄与超时展示
- `tests/test_comres_heartbeat.py` — 新增覆盖
- `tests/smoke_comres.py` — 按上报值断言 interval；stale 用 helper 阈值
- `AGENT_BRIDGE_PROGRESS.md` / `AGENT_BRIDGE_EVIDENCE.md` — 本任务证据
- （域内未改）`tests/test_comres_prune.py` — 既有用例仍通过

## c. 版本/提交证据

改动文件内容 hash + ISO 时间戳（`sha256sum` + `stat` mtime UTC）：

| path | sha256 | mtime (UTC) |
|------|--------|-------------|
| VERSION | 9344871e852c4b6d582f8213b8f4d1b4beaad33182507c831972528b48577861 | 2026-11-25T15:11:44Z |
| CHANGELOG.md | db8b2ce903113d089168c2ed89fa96dc0c376f80c2ff238d9296f73af618554e | 2026-11-25T15:11:48Z |
| app/config.py | e7faca1431a7dba46fc43104fff0e4ea93b3296eb0a2d3bb8a9e484028875579 | 2026-11-25T15:09:59Z |
| app/domain/comres.py | 076d53520014f5b51f04b62eb3be79977e280b02be2530cfe4669c9e21aab60c | 2026-11-25T15:11:06Z |
| app/domain/comres_prune.py | 5a29114c8a7422e007f3489ceb22f42c9cbfce78b1207021b9f99c2b4944d4a2 | 2026-11-25T15:10:12Z |
| app/domain/wirelog.py | 764ac060d716c4403f9cd8c84701d697e66ec58e3954bc6e6281e82e608738bb | 2026-11-25T15:11:19Z |
| app/workers/main.py | 61eecd3869b24b3f25220c7949b2dcb97db0fae8268c61596c5022a907d62a75 | 2026-11-25T15:10:14Z |
| web/src/App.vue | 0fcda8aa64672c8bf84bd53a844db7513e50bb739def4503164978b0c0d3db9e | 2026-11-25T15:11:33Z |
| tests/test_comres_heartbeat.py | 32fdc9b9ed94e55bd16d313bab1f4b9aab4bf877c7f2e5a717590651268500a7 | 2026-11-25T15:12:26Z |
| tests/smoke_comres.py | 817ad7923692a093431baecc5be8ae254d58c771481735a43938a05d63f58a33 | 2026-11-25T15:12:01Z |

- 提交：`5677478707b5d0555bbcff8d8f09942749c0815d` — `fix(comres): honor node heartbeat interval (0.54.2)`
- 分支：`fix/comres-heartbeat`（基线 `4fb12e2` / 0.54.1）
- 未 push / 未部署
- 禁止项：未做基线快照存储、自动回滚、git init

## d. 验证/自测

- `timeout 180s … pytest tests/test_comres*.py tests/test_wirelog*.py -q --tb=line` → 退出码 `0`，`65 passed`
- 关键行为：
  - 上报 `heartbeat_interval_s=60` → 响应 60 且写入 `config_json`
  - 无上报 → 兜底 3600；非法 0/99999 → `COMRES_BAD_HEARTBEAT_INTERVAL`
  - 60s 节点心跳回拨 4min → prune offline+端口 stale；3600s 节点同龄不 offline
  - `expire_allocations`：60s 节点 >180s 释放 auto；3600s 不误放
  - `list_nodes` 超时只读返回 `stale` + `heartbeat_age_s`
- `tests/smoke_comres.py`：本改相关断言（按上报 interval）已更新；脚本另有**既有**失败（端点独占双节点 count=1；F6 auto `allocated=false`；miss 用例 NoResultFound），与本任务无关
