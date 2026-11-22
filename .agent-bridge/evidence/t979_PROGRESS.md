# Quamm TRES 实现进度

> 任务：t979 —— 设备中心串口按钮灰显 + 去掉写授权面板  
> task_id: t979

## 状态：已完成（停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| devices 派生字段 | serial_openable / serial_ports | ✅ |
| 串口按钮灰显 | disabled + tooltip | ✅ |
| 去掉写授权 UI | 表单/列/入口；API deprecated 注释 | ✅ |
| 单测 + build + 真机 | 8 passed；build 0；截图 | ✅ |

## 说明

- 停住等审
