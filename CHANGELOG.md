# Changelog

所有值得注意的改动都会记录在此文件。
版本号唯一真源是根目录 `VERSION` 文件；本文件与 git tag 仅作镜像。

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
