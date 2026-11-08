# Quamm RES 实现进度

> 任务：quamm-tres 接入真实 XOS 设备（real ADB 探测 + 工程配置名）（task_id: t786）

## 状态：已完成（commit + push 待审）

## 本轮进展

| 项 | 内容 | 状态 |
|---|---|---|
| RealXosProber | `app/domain/prober_real.py`：`adb connect`(3s) → `adb -s shell` 读 `/etc/product_name`、eth0/wlan0 MAC、`/proc/cmdline`、`/proc/device-tree/compatible`、`test -d wlan0` → `adb disconnect`；固定 mac 排除 fe: 随机地址；L0/L3 判定；**不调用 getprop**（XOS 无该命令）；所有 adb 命令 GNU timeout 3s + subprocess 超时双保险 | ✅ |
| device 表字段 | `project_name`/`board_dt`/`storage_type`/`has_wifi`/`source` 五列；`app/migrations.py` 幂等 ALTER，旧库不丢数据，既有 22 台标 `source=mock` | ✅ |
| settings / 分流 | `probe_backend` 的 `real` 取值接 RealXosProber（替换旧 getprop 版 AdbProber 实现，保留 `AdbProber` 旧名兼容单测）；新增 `get_prober_for(source)` / `probe_device(source=)`；巡检按 `device.source` 分流，mock 永远走 HTTP /probe | ✅ |
| 真实机队注册 | `scripts/register_real_fleet.py`：14 台全部入库；连不上/探测不完整按用户要求记 pending；position `REAL-AUTO-01..14`、owner `auto-import`；model_class 用 project_name（补 product_type 记录满足外键，不因字典拒绝） | ✅ |
| Web / API | `web/src/App.vue` 设备表加「工程配置」列并 rebuild dist；`/api/dashboard` 增 `projects`（工程配置名分布）、`/api/devices` 增 `project_name` 等字段 | ✅ |

## 结果

- 14 台真实设备入库：6 台 L3/available（moto_ld、qm10xh_full×2、qm10xv_full、framex×2），8 台 pending（含 esl2×2 因仅 fe: 随机 MAC 无主键、2 台 adb offline、4 台探测不完整）。
- 22 台 mock 仍 `source=mock` / `available` / `L3`；新代码 scoped patrol `scanned=22 l3=22`。
- `/api/dashboard` 与 `/api/devices` 均返回 `project_name`。

## 验证

- `.venv/bin/python -m pytest tests/ -q` → 30 passed（退出码 0）
- `scripts/verify_v1.py` → 32/32 PASS（退出码 0）
- `cd web && npm run build` → 退出码 0，dist 重建
- RealXosProber 对 172.16.10.192 → product_name=moto_ld / mac=00:0e:c6:7b:24:ba / storage=nand / L3
- `py_compile` 10 个改动文件 → 退出码 0
- 证据详见 `AGENT_BRIDGE_EVIDENCE.md`（task_id: t786）

## 未做

- 未动 `docs/` 下已有设计文档。
- 真实 adb 仅读文件，未 push / remount / 刷机；未调用 getprop。
- 未改动 `scripts/verify_v1.py`；老库迁移不删记录。
