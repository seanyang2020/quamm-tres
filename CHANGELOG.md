# Changelog

所有值得注意的改动都会记录在此文件。
版本号唯一真源是根目录 `VERSION` 文件；本文件与 git tag 仅作镜像。

## [0.13.0] - 2026-11-08

### 新增
- 发接入凭据改为两步式：先「生成凭据（预览）」调 `/api/credentials/batch-generate`，弹窗展示每人 token / 邮件正文 / MCP 配置，再「确认发送邮件」调 `/api/credentials/batch-send`
- 设备类型分「启用中 / 已停用」两区：主区只列 enabled 类型，停用类型收进折叠面板；停用走旧 disable 接口，启用走 `PUT /api/product-types/{type_code}` body `{enabled: true}`
- `/api/product-types` 列表返回 `enabled` 字段

### 变更
- `/api/version` 的 `name` 由 `Quamm RES` 改为 `Quamm TRES`，`scripts/verify_v1.py` 断言同步更新
- 版本号 0.13.0

## [0.12.0] - 2026-11-08

### 新增
- product_type 扩展 6 个工程/编译元数据字段（defconfig / sdk_repo_cmd / build_cmd / image_path / jenkins_url / notes）；幂等迁移只 ALTER ADD COLUMN
- 完整度判定：defconfig + SDK 获取命令 + 编译命令 + 镜像路径四列都非空 = 【完整】，缺任一 = 【信息不全】；允许信息不全机型存在
- Web 设置页新增「设备类型」Tab：列表打完整度标签，admin 可弹窗编辑 6 字段（PUT /api/product-types/{type_code}）
- res_apply 支持两种二选一：`product_type=`（按机型池申请 / 机型等待队列）或 `device_id=`（指定设备，兼容旧用法 target=）
- res_list_products 每个机型返回 总数 / available / leased(活跃租约) / queued(机型排队人数)
- Web 排队列表行可展开：显示 agent_label + 申请人 email/name、等待机型、已等多久

### 说明
- `/api/version` 的 `name` 当时仍为旧展示名，0.13.0 起统一为 `Quamm TRES`（verify_v1.py 断言同步更新）；版本号由 VERSION 单一真源读取
- 排队测试用例（verify_v1.py）同步改为按机型（product_type）申请，32 项验收保持全 PASS

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
- `/api/version` 的 `name` 自 0.13.0 起统一为 `Quamm TRES`；此前为兼容历史验收脚本保留旧值。

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
