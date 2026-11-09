# Quamm TRES 实现进度

> 任务：v0.24.0 设备截图 Web 预览增强 + 图片生命周期管理（task_id: t805）

## 状态：已完成（代码 + 迁移 + REST/MCP + 释放清理 + worker + 前端 build + 单测 + 实机验证 + 证据；未 commit / 未 push，停住等审）

## 本轮进展

| 项 | 内容 | 状态 |
|---|---|---|
| 截图元数据入库 | 新增 `ScreenshotRecord`（表 `screenshot_record`，10 字段含 shot_id/device_id 索引/file_name 唯一/captured_at 索引）；`create_all` 自动建 + `migrations._ensure_screenshot_table` inspect 判表幂等兜底；`capture_screenshot` 每次成功后写记录（显式 commit）+ 自动 per-device prune；`list_screenshots`/`delete_screenshot` 领域函数（删行+删文件，缺失不报错） | ✅ |
| 保留策略 / 磁盘上限 | `config.py` 默认 + `runtime_config` 白名单 4 键（max_per_device=10 / retention_days=14 / total_max=500 / cleanup_on_release=true）；新模块 `screenshot_lifecycle.py` 实现 prune_device / prune_global（超期 + 总数上限最旧先删），所有删除写 audit(action=screenshot_prune, detail 含数量/原因) | ✅ |
| worker 周期任务 | `app/workers/main.py` 增 `_screenshot_prune_job`（每小时，独立任务 best-effort 不阻塞） | ✅ |
| 换用户/释放清理 | `leasing._reclaim()` 物理闸门附近 best-effort 删该 device_id 全部截图（记录+文件+audit），try/except 包裹、commit=False 挂外层事务，失败只 log 警告不影响释放；配置开关可关 | ✅ |
| REST/MCP | admin `GET /api/screenshot-records`（历史）、`/stats`（总数/占用）、`DELETE /{shot_id}`（单删）、`POST /prune`（立即全局清理）；POST 截图与 `res_screenshot` 传 created_by | ✅ |
| 前端预览增强 | 对话框展示 captured_at(本地)/quality/rotate/float_abi/bin/bytes；下载 `<a download>`；重新截图(loading)；旋转 0/90/180/270；`apiUrl()` 用 `new URL(..., window.location.href)` 生成绝对地址兼容隧道/子路径 | ✅ |
| 前端历史 + 存储管理 | 设备表「截图记录」入口（缩略图+时间+点开预览+单删）；设置「截图存储」块（总数/占用空间、4 项策略表单、立即清理） | ✅ |
| DB 升级能力确认 | 只读核对 `db_source.py`：`XOSADB2UPDATE_PATH=/home/scm/bin/xosadb2update` 存在且可执行，`upgrade()` 默认 `-f -a -e data` 未变，probe/versions 只读函数可调用；未改默认 | ✅ |
| 修复 | `runtime_config.set_runtime_value()` bool 原来一律存 `"true"`（开关关不掉），改为按真实值存 true/false | ✅ |
| 版本 | VERSION 0.23.0→0.24.0；CHANGELOG 加 `[0.24.0] - 2026-11-09` | ✅ |
| 验证 | `scripts/verify_screenshot_lifecycle.py` 实机 172.16.10.246 截图 3 张 PASS；新增 13 项单测全通；前端 `npm run build` 通过 | ✅ |

## 结果

- 实机：连拍 3 张，记录数 1→2→2（N=2 触发 prune 删最旧），磁盘仅留最新 2 张；列举/统计/单删/全局 prune 通过（临时 SQLite 库，不污染线上）
- 全量 pytest：`17 failed, 70 passed`；干净 HEAD 基线 `17 failed, 57 passed`，失败名单完全一致（既存，v0.17 移除种子机型后旧断言未更新），无新增回归；新增 13 项单测全通过
- 截图相关单测：`40 passed`（原 27 + 新 13）；py_compile 14 文件 + 全模块导入 + `npm run build` 均 exit 0
- 迁移幂等：删表后 migrate 补建、二次不报错；REST 防穿越 `../evil.jpg` 仍 HTTP 400

## 未做 / 说明

- 未本地 git commit；未 push origin（由用户提交推送）
- 未重启 uvicorn/worker：新 API/MCP 端点需进程重启后生效；验证用「直调处理函数 + 全新进程导入 + 临时库」完成
- 不提交两个 `*.bak-caddyadd`；未触碰 `app/discovery.py`（用户既存未提交改动）及 `.agent-bridge/evidence/t804.md`
- `web/dist/`、`app/var/screenshots/*.jpg` 为运行产物（gitignore），不提交
- 仓库既存 17 项单测失败与本任务无关，未擅自修改测试或种子
- 设备端 vendor 二进制 `xosscreen -r 90/180/270` 实测 Segmentation fault（二进制自身 bug）；实机验证统一 rotate=0，前端仍按所选 rotate 调用 API，透传由 mocked 单测覆盖
