# Quamm TRES 实现进度

> 任务：MCP 固件下载/刷机 + 版本策略 + 保留 data 分区（task_id: t792）

## 状态：已完成（功能 + 单测 + 真实 moto_ld 刷机 + 证据；本地 commit，未 push，停住等审）

## 本轮进展

| 项 | 内容 | 状态 |
|---|---|---|
| 机型映射 | `product_type.db_project_name`：esl2→esl2、framex→framex（`app_framex` 是 APK 工程，构建号 101≠设备 922）、moto_ld→moto_ld_102d、qm10xh_full→10xh_sdk_demo_lh、qm10xv_full→XOS_SDK_Daily/10xv_lv（设备 fw build 125 与该目录一致） | ✅ |
| 版本策略 | `required_major/required_minor/required_patch`（0=不强制）；major 不足 INCOMPATIBLE、minor 不足 UPGRADE_REQUIRED、patch 落后仅提示、空版本 UNKNOWN_VERSION | ✅ |
| 设备版本 | `device.firmware_version`；`prober_real` 实测读 `/etc/fw_version`（回退 `/etc/version`）；巡检回填 | ✅ |
| res_firmware_versions | HTTP 解析 Daily/XOS 目录，按 build_time 倒序，limit；无工程 PRODUCT_NO_DB_PROJECT、不可达 SOURCE_UNAVAILABLE | ✅ |
| res_flash | GRANTED + 本人校验；preserve_data=False 需 confirm_erase_data；同租约任务互斥；flash_job 落库；threading 后台跑 dbxostest | ✅ |
| res_flash_status | QUEUED/RUNNING/SUCCEEDED/FAILED + 日志最后 50 行 | ✅ |
| res_firmware_status | semver 判定 + upgrade_needed + reason/recommendation | ✅ |
| res_apply 门禁 | 跳过 incompatible/upgrade_required 设备，继续找同型下一台；全不符合 NO_COMPATIBLE_DEVICE；未知版本警告放行；patch 落后带 firmware_hint | ✅ |
| 异步硬要求 | `threading.Thread` 后台执行，job 全程落 DB，日志逐 job 落 `var/flash/logs/` | ✅ |
| 真实刷机 | moto_ld `dev_dfbff92075`（172.16.10.192）DB154，preserve_data=True（日志确认 `xosadb2update -f -a -e data`），job SUCCEEDED | ✅* |

\* 刷写与校验通过、设备重启后约 15 分钟仍 `No route to host`（未回到 172.16.10.192）；已按「中途失败也接受」处理，目标巡检确认 L0 并移出可申请池。

## 结果

- `pytest tests/ -q` → 43 passed（原 33 + 新增 10）
- 4 个 MCP 工具经 HTTP JSON-RPC 实测；`tools/list` 共 21 个工具
- 5 个机型版本目录均可达，moto_ld 最新 154（07-Nov-2026 20:36）
- 服务已重启：uvicorn 991224 / worker 991225，`/api/health` → 0.14.0

## 关键说明

- dbxostest 的 `-u` 参数值以 `-` 开头，argparse 会误判为选项（`-u "-f -a -e data"` 报
  `expected one argument`）。实现改用等价形式 `--update-args=-f -a -e data`（单 argv），
  首次真实任务 `fj_16a108151b48` 因此失败并留下证据，修正后 `fj_6241faa6a063` SUCCEEDED。
- 仅动 `mcp/server.py`、新建 `domain/flash.py`、`models.py`、`migrations.py`、`leasing.py` 门禁，
  以及任务点名的 `prober_real.py`；`patrol.py` 仅加 3 行回填 firmware_version。前端未动。

## 未做 / 说明

- 未 push origin；本地 commit `e127831`。
- t789 遗留的 `web/*.bak-caddyadd` 未跟踪文件未触碰、未 git add。
