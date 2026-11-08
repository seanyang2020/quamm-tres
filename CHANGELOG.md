# Changelog

所有值得注意的改动都会记录在此文件。
版本号唯一真源是根目录 `VERSION` 文件；本文件与 git tag 仅作镜像。

## [0.11.0] - 2026-11-08

### 新增
- 全局改名：产品展示名 Quamm RES -> Quamm TRES（网页 title / MCP server name / README / 日志前缀 / MCP 配置 key）
- Web 登录：rr_ token 换 8 小时 HttpOnly 会话 cookie；`/api/auth/login|me|logout`；admin/user 两档权限
- 首次引导：库中无 role=admin token 时引导生成第一个管理员（已有 admin@corp.com 保留）
- 人员通讯录（contacts 表，17 人种子）+ 设置页通讯录 Tab（增删改查）
- 发接入凭据：通讯录多选 / 手填邮箱 + 权限 + 备注，后端签 rr_ token 并 SMTP 发 MCP 配置邮件
- SMTP 在线配置（settings 表）+ 发测试邮件
- 凭据台账扩展：name / role / note / 是否激活 / 创建时间；续期 / 吊销
- 租约列表显示申请人 email + name（token 关联联系人）
- user 登录后只看自己的租约 / 申请 / 凭据，看不到他人与占用详情

### 说明
- `/api/version` 的 `name` 仍是 `Quamm RES`：这是 `scripts/verify_v1.py` 断言的 API 契约值，保持不变以不破坏 32 项验收。

## [0.10.0] - 2026-11-08

### 新增
- 三域骨架（设备申请 / 设备注册 / 设备管理）
- 5 类机型字典 + 20 条一人一 token 凭据
- 20 台模拟设备经真实 MCP 链路注册入库
- 巡检闭环（L3/suspect/offline/恢复）与可用池口径对齐
- 17 个 MCP 工具
- Web 八个一级菜单

### 修复
- res_list_products / res_my_leases 空集合崩溃
- res_whoami is_admin 误判

### 已知限制
- 真实 ADB 探测未接入（仅 mock 后端）
- 串口资源未纳入
- 通知仅落库，未接企微/邮件
