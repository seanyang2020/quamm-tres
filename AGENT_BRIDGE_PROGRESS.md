# Quamm TRES 实现进度

> 任务：t981 —— 设备指纹接收/存储/展示 + 匹配 MAC→SN→IP  
> task_id: t981

## 状态：已完成（停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| 协议落库 | `fingerprint_json` 列 + register/heartbeat 落库（无则保留） | ✅ |
| identity 回填 | shell_mac/shell_ip 仅空时回填；port.sn/mac/ip 优先 identity | ✅ |
| 匹配顺序 | MAC → SN → IP；basis 增 `fp_mac`/`fp_sn`/`fp_ip` | ✅ |
| API | resources 返回 fingerprint/fp_verdict；单查 `/ports/{node}/{slot}/fingerprint` | ✅ |
| 审计 | `comres_fingerprint_update`（cleared_fields，不写全文） | ✅ |
| 前端 | 资源池+节点详情「设备信息」抽屉；fp_verdict 列 | ✅ |
| 单测 | `tests/test_comres_fingerprint.py` 9 项 | ✅ |
| 真机+截图 | register 冒烟 + `t981-shots/fp-drawer.png` | ✅ |

## 提交

- `0151105` / `0151105b3214078a0d3bc46afa9d8203f5af245d`

## 说明

- standalone 侧按同一 schema 上报另开任务
- 停住等审
