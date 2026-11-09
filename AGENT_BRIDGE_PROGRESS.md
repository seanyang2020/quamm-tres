# Quamm TRES 实现进度

> 任务：v0.23.0 设备截图 + 芯片/浮点能力 + MCP 释放（task_id: t804）

## 状态：已完成（代码 + 迁移 + MCP/REST + 前端 build + 验证脚本 + 证据；未 commit / 未 push，停住等审）

## 本轮进展

| 项 | 内容 | 状态 |
|---|---|---|
| Vendor 二进制 | `app/vendor/xosdbg`(软浮点) / `app/vendor/vxosdbg`(硬浮点)，从 prebuild 复制并保持可执行，sha256 一致 | ✅ |
| 截图核心 | `app/screenshot.py`：GNU timeout + subprocess 双保险；浮点 ABI 三级判定（机型配置→cpuinfo Features→运行时 `--version` 兜底）；connect→push(/data 不可写退 /tmp)→chmod→`xosscreen -s`→pull→JPEG 校验→清理；截图时 ABI 不匹配自动换另一个二进制 | ✅ |
| 探测增强 | `prober_real.py` 读 `/proc/cpuinfo`，extra 增 features / cpu_part / cpu_implementer / has_fpu，现有逻辑不回归 | ✅ |
| 芯片/浮点字段 | `models.py` + `migrations.py` 幂等 ALTER 增 chip_model / float_abi(auto) / has_fpu(0)；品类增/改可填、注册时探测自动回填（不覆盖人工、不阻塞） | ✅ |
| MCP | 新增 `res_screenshot`（30→31），`_guard` + require_auth，DomainError→`{"error":...}` | ✅ |
| REST | admin `POST /api/devices/{device_id}/screenshot`（cookie）、`GET /api/screenshots/{file_name}`（FileResponse + 白名单/basename/目录包含三层防穿越） | ✅ |
| 前端 | 设备表操作列 admin「截图」按钮 + el-image 预览弹窗；品类编辑/新增表单 + 表格加芯片·浮点字段；`npm run build` 通过 | ✅ |
| DB 升级 | `db_source.py` 增 `XOSADB2UPDATE_PATH` 可配置常量（env 可覆盖）；确认 `/home/scm/bin/xosadb2update` 存在且为默认；默认 `-f -a -e data`（保留 data）未变 | ✅ |
| 版本 | VERSION 0.22.0→0.23.0；CHANGELOG 加 [0.23.0] - 2026-11-09 | ✅ |
| 验证 | `scripts/verify_screenshot.py` 对 172.16.10.246 截图 PASS（hard/vxosdbg，JPEG 240x320 可打开）；新增 27 项单测全通 | ✅ |

## 结果

- 真实设备截图：`float_abi=hard` → `vxosdbg`，pull 回主机 jpg 经 `file`/SOI/EOI/SOF 校验可打开
- MCP 工具数：30 → 31（`res_screenshot` schema `device_id/ip/port/quality/rotate`）
- 全量 pytest：57 passed / 17 failed（17 项为仓库既存失败，干净 HEAD 同名单，无回归）；新增 27 项 `tests/test_screenshot.py` 全通过
- Python 全模块导入 + py_compile + `npm run build` 均 exit 0
- 迁移幂等：老 schema 两次 migrate 均 OK；本地库已幂等补 3 列

## 未做 / 说明

- 未本地 git commit；未 push origin（由用户提交推送）
- 未重启 uvicorn/worker：新 API/MCP 端点需进程重启后生效；验证用「直调处理函数 + 全新进程导入」完成
- 未触碰 `app/discovery.py`（用户既存未提交改动）；两个 `*.bak-caddyadd` 未跟踪文件不纳入提交
- `app/var/screenshots/*.jpg` 为运行产物（gitignore `var/`），不提交
- 仓库既存 17 项单测失败（v0.17 移除种子机型后旧断言未更新），与本任务无关，未擅自修改测试或种子
