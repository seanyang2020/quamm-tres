---
task_id: t817
---

# Quamm TRES 设备位置可重复 + Agent 自动联动验收证据（task_id: t817）

> 需求一：同一位置可放多台设备，取消 position_code 全库唯一。
> 需求二：指定名单「允许 Agent」按所选用户的有效凭据给出选项/自动填充。

## a. 编译/构建

```bash
cd /home/scm/quamm-res
python -m py_compile app/models.py app/migrations.py app/domain/catalog.py \
  app/api/router.py app/discovery.py tests/test_registration.py
# 退出码：0
# 输出：PYCOMPILE_OK

cd /home/scm/quamm-res/web && npm run build
# 退出码：0
```

构建尾部：

```
vite v5.4.21 building for production...
✓ 1591 modules transformed.
dist/index.html                     0.40 kB
dist/assets/index-6Q5-UEmr.css    366.25 kB │ gzip:  49.27 kB
dist/assets/index-Bwc0PQbF.js   1,153.44 kB │ gzip: 369.71 kB
✓ built in 4.78s
```

产物存在：`web/dist/index.html`、`web/dist/assets/index-6Q5-UEmr.css`、`web/dist/assets/index-Bwc0PQbF.js`。

## b. 改动文件清单

- `app/models.py`：`uq_device_position_code` 唯一索引 → `ix_device_position_code` 普通索引；模块注释同步。
- `app/migrations.py`：新增幂等 `_ensure_device_position_index()` 并在 `migrate()` 调用。
- `app/domain/catalog.py`：删除 `POSITION_DUP` 位置占用校验；同步释放位置处的过时注释。
- `app/discovery.py`：同步注册校验注释（去掉“位置唯一性”措辞）。
- `app/api/router.py`：新增 `POST /api/principals/agent-options`；更新设置页位置规则描述。
- `web/src/App.vue`：删除 `suggestFreePosition` 自动避重；位置文案更新；restricted 分支 Agent 拉取/自动填充/选项/移除用户重算。
- `tests/test_registration.py`：原 `test_position_duplicate_rejected` 改为 `test_same_position_allows_multiple_devices`。
- `AGENT_BRIDGE_PROGRESS.md`、`AGENT_BRIDGE_EVIDENCE.md`：进度与证据。
- `.agent-bridge/evidence/t817.md`：证据镜像。

未触碰 WireLog：`app/domain/wirelog.py`、`app/api/wirelog_router.py`、`app/domain/prober_uart.py`。

## c. 版本/提交证据

```bash
git log -1 --oneline
# 7a8f4a1 v0.29.1 serial: shared-port queue + explicit release + port health degrade

git status --short
#  M AGENT_BRIDGE_EVIDENCE.md
#  M AGENT_BRIDGE_PROGRESS.md
#  M app/api/router.py
#  M app/constants.py            （本任务未改）
#  M app/discovery.py
#  M app/domain/catalog.py
#  M app/domain/patrol.py        （本任务未改）
#  M app/domain/probe.py         （本任务未改）
#  M app/main.py                 （本任务未改）
#  M app/mcp/server.py           （本任务未改）
#  M app/migrations.py
#  M app/models.py               （含本任务前已有的未提交改动）
#  M tests/test_registration.py
#  M web/src/App.vue             （含 t806 已有的未提交 UI 改动）
#  ?? .agent-bridge/evidence/t817.md
#  ??（其余 wirelog/备份/脚本未跟踪文件为既存状态）

git diff --stat
#  7 files changed, 743 insertions(+), 59 deletions(-)  [本任务涉及文件子集]
```

Gerrit change：无（未 commit / 未 push）。

## d. 验证/自测

### 1. 同位置注册（真实 probe → register）

```bash
.venv/bin/python -m pytest \
  tests/test_registration.py::test_same_position_allows_multiple_devices \
  tests/test_registration.py::test_reregister_same_device_is_idempotent -q
# 退出码：0
# 2 passed
```

用例同时校验 `res_list_devices` 返回的两台设备 `position_code` 均为 `302-R4-01`。

### 2. 旧库迁移 + 同位置共存（临时 SQLite）

脚本构造旧库：`CREATE UNIQUE INDEX uq_device_position_code`，插入 1 台设备后执行 `migrate()`：

```
before indexes: [('uq_device_asset_tag',1), ('uq_device_mac',1), ('uq_device_position_code',1), ('uq_device_sn',1)]
after indexes:  [('ix_device_position_code',0), ('uq_device_asset_tag',1), ('uq_device_mac',1), ('uq_device_sn',1)]
same position devices: ['dev-new', 'dev-old'] count: 2
second migrate idempotent: [('ix_device_position_code',0), ...]
```

结论：旧唯一索引已删除、新普通索引创建、原设备仍在、可插入第二台同位置设备、重复迁移无副作用。

### 3. Agent 选项接口逻辑

同一脚本直接调用 `principal_agent_options`：

```
{'items': [
  {'email': 'a@corp.com', 'agents': ['ci','laptop'], 'default_agent': 'ci', 'found': True},
  {'email': 'b@corp.com', 'agents': ['only'], 'default_agent': 'only', 'found': True},
  {'email': 'none@corp.com', 'agents': [], 'default_agent': None, 'found': False}
]}
AGENT_OPTIONS_OK: True
```

覆盖：多 agent 去重排序、last_used_at 最新优先、单 agent、无凭据邮箱、吊销/过期凭据过滤。

### 4. 已知基线失败（与本次改动无关）

```bash
.venv/bin/python -m pytest tests/test_registration.py -q
# 2 failed, 5 passed
```

失败项 `test_register_20_devices`、`test_probe_unreachable_registers_pending` 依赖预置机型；
当前 `app/seed.py` 的 `PRODUCT_TYPES` 自 v0.17 起为空列表（本次未改），导致机型列表为 0、
`res_apply` 报 `UNKNOWN_TARGET`。与位置索引/注册逻辑改动无关。
