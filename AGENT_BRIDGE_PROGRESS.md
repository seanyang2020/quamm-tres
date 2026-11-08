# Quamm TRES 实现进度

> 任务：Quamm TRES v0.11 登录认证 + 凭据管理 + 通讯录 + SMTP 发 token + 全局改名（task_id: t787）

## 状态：已完成（功能 + 证据已提交，push 待审）

## 本轮进展

| 项 | 内容 | 状态 |
|---|---|---|
| 全局改名 | 网页 title / 侧栏 / FastAPI title / MCP server name `quamm-tres` / MCP 配置 key / README / CHANGELOG / 日志前缀 `quamm-tres.worker` / demo 描述 统一 Quamm TRES；VERSION 0.10.0→0.11.0 | ✅ |
| 登录认证 | `app/web_auth.py`：HMAC 签名 HttpOnly cookie（8h），cookie 只存 credential_id 每次复核；`/api/auth/login\|me\|logout\|setup-status\|setup`；rr_ token 与 MCP 同一套 | ✅ |
| 首次引导 | 库中无 role=admin token 时 `needs_setup=true`，`/api/auth/setup` 凭邮箱生成第一个 admin token；已有 admin@corp.com 保留，重复调用 409 | ✅ |
| 权限两档 | admin 看全部；user 只看自己的租约/申请/凭据，dashboard 隐藏占用详情；写/管理接口 require_admin（401/403）；MCP 仍 Authorization header 不受影响 | ✅ |
| 凭据 + 通讯录 | `contacts` 表（17 人 @quaming.com 种子）、`settings` 表；设置页「人员通讯录」Tab（增删改查）、「发接入凭据」Tab（通讯录多选/手填 + 权限 + 备注 + SMTP 发送）；凭据台账增 name/role/note/active/created_at，续期/吊销 | ✅ |
| SMTP 发 token | 配置存 settings 表（任务给定默认值），管理员可改 + 发测试邮件；纯文本正文 = MCP 配置 JSON + 使用说明；smtplib 全程 15s 超时 | ✅ |
| Web lease 申请人 | `/api/leases` 增 `name`（token→联系人），占用列表显示 email + 姓名 | ✅ |
| 前端 | `web/src/App.vue`：登录页 / 首次引导页 / 登出 / 设置 4 Tab，沿用 Element Plus，无新框架；`npm run build` 重建 dist | ✅ |
| 兼容性 | `scripts/verify_v1.py` 未改动；`/api/version` 的 `name` 保留 `Quamm RES` 契约值；只读接口匿名 200；迁移只 ALTER ADD COLUMN 幂等；prober_real.py 未动；端口 9100 不变 | ✅ |

## 结果

- `verify_v1.py` 32/32 PASS（0.11.0 就绪）；`pytest tests/ -q` 30 passed；`py_compile` 与 `npm run build` 退出码均 0。
- 重启 uvicorn + worker 后 `/api/health` 200、`/api/version` 0.11.0，启动迁移无报错。
- 真机实测：admin token 登录拿到 HttpOnly cookie；通讯录 17 人 CRUD 正常；SMTP 测试邮件与发凭据邮件均 `sent=true`（真实发出）；user token 登录后 admin 接口 403、只看自己租约；匿名 admin 写接口 401。
- 活跃租约显示申请人 email + 姓名；user02 看不到 user01 的租约。

## 验证

- `.venv/bin/python -m pytest tests/ -q` → 30 passed（退出码 0）
- `timeout 180 .venv/bin/python scripts/verify_v1.py` → 32/32 PASS（退出码 0）
- `cd web && timeout 60 npm run build` → 退出码 0，dist 重建
- Live HTTP 冒烟：登录/通讯录/SMTP 测试邮件/发凭据/user 过滤/引导/租约申请人（脚本退出码 0）
- 证据详见 `AGENT_BRIDGE_EVIDENCE.md`（task_id: t787）及 `.agent-bridge/evidence/t787.md`

## 未做 / 说明

- 未 push（共享远端，停住等审）；t786 遗留的 4 个未跟踪备份未触碰、未 git add。
- `web/dist/` 为 .gitignore 构建产物，未入库；部署时需 `npm run build` 或沿用已重建的磁盘 dist。
- 冒烟测试生成的测试凭据与孤立 principal 已从实时库清理。
- `/api/version` 的 `name` 保留 Quamm RES：verify_v1.py 对该字段有硬断言，属 API 契约值，产品展示名已全部改为 Quamm TRES（README/CHANGELOG 已注明）。
