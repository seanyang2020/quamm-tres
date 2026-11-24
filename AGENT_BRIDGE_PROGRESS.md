# 交互式 adbd 变体探测回退 — 进度

> 任务：tres-interactive-shell-fallback-20261124  
> task_id: tres-interactive-shell-fallback-20261124

## 状态：已完成（停住等审）

## 本轮工作

| 项 | 内容 | 状态 |
|---|---|---|
| 交互式 shell 回退 | `prober_real.py`：常规 shell 探针失败后 stdin 灌入采集 | ✅ |
| 开关 | `TRES_INTERACTIVE_SHELL_FALLBACK` 默认开；0/false/off 关 | ✅ |
| 单测 | fake adb A（忽略参数）/ B（正常）+ 关闭开关 + shell dead | ✅ |
| 真机 206 | L3 / nxf7e / MAC wlan0；关闭回退 → L0 | ✅ |
| 对照 198 | fallback on/off 逐字段一致 | ✅ |
| 提交 | `feat/comres` | ✅ |

## 说明

- 正常设备路径优先且零行为变化；仅 shell `<cmd>` 拿不到 `__TRES_SHELL_OK__` 时回退
- `mac_source` 沿用既有 `eth0`/`wlan0`（非新枚举）
- 未部署 201、未改 DB、未碰 lease/patrol/flash/serial
