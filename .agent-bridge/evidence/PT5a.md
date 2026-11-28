---
task_id: PT5a
title: PowerRes 绑定权限收紧为 admin + 绑定信息补全（location/driver）
date: 2026-11-26
branch: feat/capabilities
baseline: 62cce60 (v0.53.0)，上游 PT3a/b/c/d + PT4a/b 未提交改动之上
---

## a. 编译/构建

- `python -m compileall -q app` → 退出码 0（COMPILE_OK）
- `python -m pytest tests/test_powerres.py tests/test_device_capabilities_api.py tests/test_serial_openable.py tests/test_serial_power_guard.py -q` → 退出码 0，尾行 `44 passed, 15 warnings in 9.87s`（新增 PT5a 用例 4 个：sync 带/不带 location+driver、超长拒绝、bind/bindings 携带 meta、API 403/200 权限矩阵）
- `cd web && npm run build` → 退出码 0，尾行 `✓ built in 4.40s`（chunk >500kB 仅告警，既有）

## b. 改动文件清单

| 路径 | 说明 |
|------|------|
| `app/models.py` | PowerResInventory 增加 `location String(64)` / `driver String(32)` nullable |
| `app/migrations.py` | `_POWERRES_INV_COLUMNS` + `migrate()` 内 `_ensure_columns("power_res_inventory", ...)` 幂等加列 |
| `app/domain/powerres.py` | `_clean_location/_clean_driver`（超限 `POWERRES_BAD_PAYLOAD` 不截断）；sync/upsert 落库；inventory/bindings/bind 行带 location/driver（`_inventory_meta` 从 inventory 取） |
| `app/api/powerres_router.py` | bind/unbind/inventory upsert/delete 换 `require_admin`（读接口保持 require_login）；inventory upsert 透传 location/driver |
| `web/src/App.vue` | A：PowerRes 卡片新增 位置/来源 列、非 admin 隐藏手工登记/绑定/解绑 + info alert、电源动作按 `prActionLeaseBlocked` 禁用（tooltip「需持有目标设备租约」）；B：绑定对话框补 位置（未登记提示）+ COM + driver + 底部控制权说明，宽度 600px；prAddDlg 增 位置/驱动 字段；C：设备详情电源卡候选/已绑表补 位置/来源 列、候选绑定按钮仅 admin、已绑行动作 `devicePowerLeaseBlocked()` 禁用 |
| `web/src/powerresBindTargets.js` | 目标设备 label 补 `位置 position_code` |
| `tests/test_powerres.py` | 新增 4 个 PT5a 用例（含 TestClient 权限矩阵） |

**未改**：`serial.check_access`、bind 唯一键语义、无新接口、VERSION、COMRes/CtrlRes 其它逻辑。

## c. 版本/提交证据

- `git log -1 --oneline`：`62cce60 chore(release): v0.53.0 设备统一能力描述符 Phase 1`
- `git status --short`：tracked 修改 11 文件 + untracked（powerres_router/powerres.py/test_powerres.py 等上游 PT3/PT4 产物），全部未 commit/push（按要求）
- `git diff --stat`（工作区 tracked 整体，含上游）：`11 files changed, 2712 insertions(+), 150 deletions(-)`
- 本任务核心文件 sha256 前16：
  - `d99b5967a88e1938` app/domain/powerres.py
  - `f785727e767d75ff` app/api/powerres_router.py
  - `d8bd1df331efc3a3` app/models.py
  - `26dfcde5f28eb559` app/migrations.py
  - `ced15e20d905f050` web/src/App.vue
  - `938369de4ade7e0f` web/src/powerresBindTargets.js
  - `cf091a426661f032` tests/test_powerres.py

## d. 验证/自测

- compileall：0（见 a）
- pytest 4 个相关文件：`44 passed`（含新增：非 admin bind/unbind/inventory POST/DELETE → 403；GET overview/inventory 非 admin → 200；admin 带 location/driver bind → 200 且 binding 返回携带）
- 前端 build：0（见 a）
- 人工路径自查（代码路径）：admin-only 按钮均有 `me.role === 'admin'` guard；非 admin 卡片顶部 info alert；PowerRes 卡已绑行与设备页已绑行电源按钮在无租约时 disabled + tooltip；sync 缺省 location/driver 落 None（旧 hub 兼容）
