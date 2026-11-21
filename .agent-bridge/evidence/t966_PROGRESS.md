# Quamm TRES 实现进度

> 任务：t966 —— 统一节点授权口径 + 串口写权限默认开放  
> task_id: t966

## 状态：已完成（停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| 一 auth 口径 | `auth_status` + `auth_source`(apply/token/none)；enroll→token | ✅ |
| 二 enroll 补写 | `ensure_enroll_apply` 幂等写 approved 行（source=enroll） | ✅ |
| 三 回填 | `scripts/backfill_enroll_applies.py`；真库 esl2-122 → approved/token | ✅ |
| 四 写权限 | shell WS：approved→can_write；write-grant 兼容保留 | ✅ |
| 五 前端 | 授权列显示来源；shell 去掉「申请写权限」文案 | ✅ |
| 六 单测 | `tests/test_auth_source_and_write.py` 7 passed（含 comres_apply 共 14） | ✅ |
| 七 构建 | `cd web && npm run build` exit 0 | ✅ |

## 提交

- `937f7f2` / `937f7f29b86e9c4509b0da9dd1d714f7434e44de`

## 说明

- 停住等审
