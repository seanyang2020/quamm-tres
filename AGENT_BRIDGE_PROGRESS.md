# t8436 新建设备默认名用设备类型展示名 — 进度

> 任务：Redmine #8436，esl2_skw 新建设备名称不应是 esl2_skw-技术部
> task_id: t8436
> 工作区：`/home/scm/worktree/tres-8436-name`
> 分支：`fix/8436-device-default-name`（基线 main / release-0.54.7 58c0f25）

## 状态：已完成（停住等审）

## 侧车追加（2026-11-27）

| 项 | 内容 | 状态 |
|---|---|---|
| 脚本修复 | fix_device_display_names.py 自动扫描优先使用 --map 新名称；apply 先改类型再改设备 | ✅ |
| 测试 | tests/test_fix_device_display_names.py 3 例 passed | ✅ |
| 提交 | 独立新 commit（未 amend） | ✅ |

## 本轮工作

| 项 | 内容 | 状态 |
|---|---|---|
| device_naming.py | resolve_device_display_name：显式 > note > 机型展示名（≠type_code）> 兜底 type-position；normalize/128 截断 | ✅ |
| catalog.device_register | display_name 参数；新建统一解析；已有设备仅显式名覆盖；返回 display_name/source | ✅ |
| router.py | /devices/register 透传 display_name；product-types POST/PUT 加 name_warning | ✅ |
| mcp/server.py | res_device_register 可选 display_name + docstring | ✅ |
| wirelog.py | 串口建机同规则：显式 > 机型展示名 > COM@node 兜底 | ✅ |
| App.vue | 「备注」→「名称（可选）」+ 默认名提示；类型对话框防呆提示；name_warning 弹窗 | ✅ |
| fix 脚本 | scripts/fix_device_display_names.py（dry-run 默认、--apply 导出 JSON 备份） | ✅ |
| 测试 | tests/test_device_naming.py 15 例全过；全量 359 passed，7 失败为基线既有 | ✅ |
| VERSION | 0.54.8 + CHANGELOG | ✅ |
| 构建 | npm run build 成功，dist 未提交（gitignore） | ✅ |
| 提交 | `bf5be79` fix(tres): 0.54.8 ... (#8436)；`18a59f9` docs 证据 | ✅ |
| push/部署 | 未 push / 未合并 / 未部署 | ✅ |

## 待办（数据修正，另行执行）

- 201 生产库：`python scripts/fix_device_display_names.py --db <201.db> --map esl2_skw=彩屏价签_V2`（先 dry-run 核对，再 --apply）
- 之后 esl2_skw 新建设备不填名称即得 彩屏价签_V2
