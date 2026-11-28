# inner-4405 — 进度

> 任务：Quamm TRES 租约配额门禁修复：admin 账号可正常申请/租约设备
> task_id: inner-4405
> 工作区：`/home/scm/github_gitee/quamm-res`
> 分支：当前工作区（HEAD `e927470`，VERSION 0.54.12）

## 状态：已完成（停住等审）

## 根因

`app/domain/leasing.py` 的 `apply()` 在 ACL 裁决前先查配额，未豁免 admin；`app/seed.py` 中 admin `quota=0`，导致 `0 >= 0` 恒触发 `QUOTA_EXCEEDED`。ACL 层 `can_use_device` 已对 `is_admin` 放行，无问题。

## 本轮工作

| 项 | 内容 | 状态 |
|---|---|---|
| 配额门禁 | `apply()` 仅在 `not auth.is_admin` 时检查 `count_active_leases >= principal.quota`；admin 直接进入 ACL/分配 | ✅ |
| 第二处门禁排查 | 全仓 grep `count_active_leases` / `quota`：仅 `leasing.apply()` 为门禁；`mcp/server.py` 的 `res_whoami` 只读展示；无第二处需豁免 | ✅ |
| seed 语义 | admin `quota=0` 保留，仅修正注释（admin 不占配额，租约入口豁免） | ✅ |
| 单测 | `tests/test_quota.py` 新增 admin quota=0 连续租 2 台；原有普通用户超额用例保留 | ✅ |
| 本机服务 | 重启 `quamm-tres`/`quamm-worker`，`/api/health` = ok / 0.54.12 | ✅ |
| 实测 | admin Bearer token：`dev_080989bb98` → GRANTED `L-f39793a0a2` → RELEASED；复核 active_leases=0、设备 available | ✅ |
| 201 部署 | 本机无 SSH 通道，未执行；交付写明离线 bundle + `scripts/deploy-git.sh` | N/A |

## 测试结果

- `tests/test_quota.py tests/test_device_acl.py tests/test_credentials_role.py`：`16 passed`（退出码 0）
- `tests/test_apply_flow.py`：`2 passed, 2 failed`；失败为 v0.17 起 `PRODUCT_TYPES=[]` 导致的既有用例过期（断言 5 个预置机型），与本改动无关
- 四文件合并：`18 passed, 2 failed`（同上）

## 备注 / 风险

- 未提交、未 push、未部署 201、未改前端/数据库既有记录。
- `test_apply_flow.py` 的两个产品字典用例在未改动的 HEAD 上同样失败（`seed_product_types` 无预置数据），建议另立任务清理，本次未扩大范围。
