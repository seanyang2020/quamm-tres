# Quamm TRES 实现进度

> 任务：t979 —— 设备中心串口按钮灰显 + 去掉写授权面板  
> task_id: t979

## 状态：已完成（停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| A 后端派生 | `GET /api/devices` 增 `serial_openable` / `serial_ports` | ✅ |
| A 前端灰显 | 设备中心「串口」`:disabled` + tooltip | ✅ |
| B 去写授权 UI | 删表单 / 列 / `wlGrant`·`wlRevoke`；接口保留 deprecated | ✅ |
| 单测 | `tests/test_serial_openable.py` 8 项 | ✅ |
| 构建/全量 | npm build 0；pytest 172 passed / 21 failed（基线+8） | ✅ |
| 真机截图 | 未分类列表操作列（4 可点 / 5 灰） | ✅ |

## 提交

- `7834e4f` / `7834e4f10546abbaf76b295b2b3c7d0bf9bd732b`

## 说明

- 停住等审
