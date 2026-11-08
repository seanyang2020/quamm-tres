---
task_id: t795
---

# Quamm TRES v0.16 验收证据（task_id: t795）

> 系统设置在线修改 + 设备类型删除引用闸 + 无邮箱凭据生成。未 commit / 未 push，停住等审。

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译 | `timeout 60 .venv/bin/python -m py_compile app/runtime_config.py app/api/router.py app/domain/leasing.py app/domain/catalog.py app/domain/patrol.py` | 0 | 5 个文件编译通过（含新建 `runtime_config.py`） |
| 单测 | `timeout 120 .venv/bin/python -m pytest tests/ -q` | 0 | `47 passed in 39.92s`（原 43 + 新增 4） |
| 前端构建 | `cd web && timeout 120 npm run build` | 0 | dist 重建成功：`web/dist/index.html` + `index-DXVwrwMx.js` / `index-CcyAbUyg.css`；vite `base` 仍为相对 `./` |
| 版本 | `get_version()` | 0 | `0.16.0` |

## b. 改动文件清单

- `app/runtime_config.py`（新增：运行时 KV 助手 + 白名单校验）
- `app/api/router.py`（PUT `/api/settings`；DELETE `/api/product-types/{type_code}`；`manual_holders` 凭据生成 / batch-send 跳过）
- `app/domain/leasing.py`（TTL / offer / grace / max / allow_suspect 走 runtime）
- `app/domain/catalog.py`（allow_suspect 走 runtime）
- `app/domain/patrol.py`（patrol_fail_threshold 走 runtime）
- `web/src/App.vue`（可编辑设置表单；机型删除二次确认；仅姓名凭据条目）
- `tests/test_v016_settings_types_credentials.py`（新增 4 项）
- `VERSION` 0.14.0 → 0.16.0；`CHANGELOG.md` 增 [0.16.0]
- `AGENT_BRIDGE_PROGRESS.md` / `AGENT_BRIDGE_EVIDENCE.md`（本任务进度与证据）
- 未触碰 `web/*.bak-caddyadd`；未改 vite base

## c. 版本/提交证据

```
$ git log -1 --oneline
1d50075 fix(vite): use relative base for universal root/subpath deployment; remove invalid # comment

$ git status --short
 M AGENT_BRIDGE_PROGRESS.md
 M CHANGELOG.md
 M VERSION
 M app/api/router.py
 M app/domain/catalog.py
 M app/domain/leasing.py
 M app/domain/patrol.py
 M web/src/App.vue
?? app/runtime_config.py
?? tests/test_v016_settings_types_credentials.py
?? web/src/App.vue.bak-caddyadd
?? web/vite.config.js.bak-caddyadd
# （写证据后 AGENT_BRIDGE_EVIDENCE.md 也会变更）

$ git diff --stat
 AGENT_BRIDGE_PROGRESS.md |  45 +++------
 CHANGELOG.md             |  12 +++
 VERSION                  |   2 +-
 app/api/router.py        | 252 +++++++++++++++++++++++++++++++++++++++++------
 app/domain/catalog.py    |  17 ++--
 app/domain/leasing.py    |  28 +++---
 app/domain/patrol.py     |   3 +-
 web/src/App.vue          | 167 +++++++++++++++++++++++++++----
 8 files changed, 424 insertions(+), 102 deletions(-)
```

- Gerrit change：无/不适用
- 本任务本地改动尚未 git commit；未 push

## d. 验证/自测

| 功能 | 命令/方式 | 结果 |
|---|---|---|
| PUT settings 白名单/越界 | `tests/test_v016_settings_types_credentials.py::test_put_settings_whitelist_and_bounds` | 越界/非白名单 400；合法写入后 GET 与 `get_runtime_value` 立即为新值 |
| DELETE 引用闸 | `::test_delete_product_type_ref_gate_and_success` / `::test_delete_product_type_blocks_active_lease` | 无引用 200 删除；有设备/排队/租约 409 且 detail 含计数 |
| 无邮箱凭据 | `::test_batch_generate_manual_holders_no_email` | 仅 `manual_holders` 生成成功；返回无占位邮箱；batch-send `skipped=True` |
| 全量回归 | `timeout 120 .venv/bin/python -m pytest tests/ -q` | exit 0，`47 passed` |
| 前端 build | `cd web && timeout 120 npm run build` | exit 0，dist 存在 |

### 各功能自测说明（手工）

1. **系统设置**：设置 → 系统设置，改「租约 TTL」等后点保存 → ElMessage 成功；再 GET `/api/settings` 可见新值；新申请租约应使用新 TTL（无需重启 API 进程）。
2. **机型删除**：设备类型 Tab 点删除 → MessageBox 二次确认显示 code；无引用成功；有设备时弹窗展示后端 409 detail。
3. **无邮箱凭据**：发接入凭据 → 添加「仅姓名」→ 生成预览标注「仅生成，不发邮件」→ 可拷贝 token；确认发送时该条目 skipped。
