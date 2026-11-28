---
task_id: t8436
---

# t8436：新建设备默认名使用设备类型展示名（0.54.8，Redmine #8436）

## 侧车追加（2026-11-27）：fix 脚本 --map 与自动扫描联动

- 问题：--map TYPE=NEW 时自动扫描仍用库内旧 display_name，dry-run 不列出应随类型改名的设备。
- 修复：scripts/fix_device_display_names.py 扫描优先取 --map 新名称（未给 map 的类型仍用库内值）；apply 顺序天然为「先类型后设备」（changes 列表按类型→精确改名→自动扫描构建）。
- 测试：新增 tests/test_fix_device_display_names.py 3 例（dry-run 列出新名候选、apply 类型+设备一起改且自定义名不动、无 map 时维持跳过），`3 passed in 0.46s`。
- 提交：见 `git log -1 --oneline`（docs 之后的 fix 脚本回归 commit）。

## a. 编译/构建

- 命令：`python -m pytest tests/test_device_naming.py -q`
- 退出码：`0`
- 关键结果：`15 passed in 1.40s`（新增用例全部通过）

- 命令：`python -m pytest tests/ -q --ignore=tests/smoke_comres.py --ignore=tests/smoke_wirelog.py`
- 退出码：`0`
- 关键结果：`7 failed, 359 passed in 99.74s`；7 个失败（test_apply_flow×2、test_mcp_http×2、test_queue×1、test_registration×2）在基线 58c0f25（`git stash` 后复跑）同样失败，属既有失败（seed 不再预置机型），与本改无关。

- 命令：`cd web && npm install && npm run build`
- 退出码：`0`
- 关键结果：`✓ built in 6.62s`；产物 `web/dist/index.html`、`web/dist/assets/index-CNOHFqDM.js`、`web/dist/assets/index-BzwBFL8y.css`；`web/dist` 已被 gitignore，未提交。

## b. 改动文件清单

- `app/domain/device_naming.py` — 新增：`resolve_device_display_name` / `normalize` / `name_warning_for_type`
- `app/domain/catalog.py` — `device_register` 增加 `display_name` 参数；新建用统一解析；已有设备仅显式名覆盖；返回 `display_name`/`display_name_source`
- `app/api/router.py` — `/devices/register` 透传 `display_name`；POST/PUT `/product-types` 响应加 `name_warning`
- `app/mcp/server.py` — `res_device_register` 增加可选 `display_name` 并透传（docstring 说明）
- `app/domain/wirelog.py` — `create_device_from_binding` 用同一解析：显式名 > 机型展示名 > COM@node 兜底
- `web/src/App.vue` — deviceAddConfirm 加 `display_name`；表单「备注」→「名称（可选）」+ 默认名提示；submitDeviceRegister/submitSerialDevice 提交 display_name；机型选择默认名提示；类型新增/编辑名称防呆提示；保存后 name_warning 弹 warning
- `scripts/fix_device_display_names.py` — 新增：--dry-run 默认 / --apply / --map / --rename / 自动扫描 / JSON 备份
- `tests/test_device_naming.py` — 新增 15 例（纯函数 9 + 防呆 1 + 集成 5）
- `VERSION` — 0.54.7 → 0.54.8
- `CHANGELOG.md` — 0.54.8 条目

## c. 版本/提交证据

- `git log -1 --oneline`：`bf5be79 fix(tres): 0.54.8 default device name uses product display name (#8436)`
- `git status --short`：（空，工作区干净）
- `git diff --stat`（HEAD~1..HEAD）：

```
 CHANGELOG.md                     |  22 +++++++
 VERSION                          |   2 +-
 app/api/router.py                |  19 ++++++-
 app/domain/catalog.py            |  36 ++++++++++--
 app/domain/device_naming.py      |  82 ++++++++++++++++++++++++
 app/domain/wirelog.py            |  20 ++++++-
 app/mcp/server.py                |   6 ++-
 scripts/fix_device_display_names.py | 160 +++++++++++++++++++++++++++++++
 tests/test_device_naming.py      | 213 +++++++++++++++++++++++++++++++++++++++
 web/src/App.vue                  |  46 ++++++++++---
 10 files changed, 584 insertions(+), 15 deletions(-)
```

- 分支：`fix/8436-device-default-name`（基线 main / release-0.54.7 `58c0f25`）
- 未 push / 未合并 main / 未部署

## d. 验证/自测

- `pytest tests/test_device_naming.py -q` → 15 passed
- 全量 `pytest tests/ -q`（除 smoke）→ 359 passed；7 个既有失败与基线一致（`git stash` 复跑核实）
- `npm run build` → 成功（见 a）
- 关键行为验证（集成用例）：
  - 显式 display_name → 完全采用，source=explicit
  - 未填 → 机型展示名（如 彩屏价签_V2），source=product_display
  - 旧客户端 note → 仍生效，source=legacy_note
  - 类型展示名 == type_code → 回退 `esl2_skw-302-R2-05`，source=fallback
  - 已有设备重复注册：note/空名不覆盖自定义名（source=kept）；显式名才更新
  - name_warning：display_name 空或等于 type_code 时返回提示，否则 None
