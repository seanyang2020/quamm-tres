# Quamm TRES 实现进度

> 任务：T5 TRES COMRes：offline 判定只对未分配端口生效  
> task_id: t5-comres-offline-allocated；承接 `f71d137`（T4）

## 状态：已完成（停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| A 已分配跳过 offline | `_mark_absent_ports_offline`：`_is_allocated` → 保持 health_state + 审计 `comres_port_skipped_allocated` | ✅ |
| A 未分配仍 offline | 未占用端口未上报 → `health_state=offline` + `comres_port_offline`（T4 语义） | ✅ |
| B 再上报恢复 | 已分配端口再次出现于 ports[] 时正常刷新 health_state / probe | ✅ |
| C smoke | ACCEPT-9 先清占用再验 T4；新增 ACCEPT-10；`ALL_PASS` | ✅ |

## 验收

- `tests/smoke_comres.py` → ALL_PASS / exit 0
- 证据：`AGENT_BRIDGE_EVIDENCE.md` + `.agent-bridge/evidence/t5-comres-offline-allocated.md`
- 提交：`fa04ffb`

## 额外改动理由

无（未改 router / wirelog / deskserialhub）。

## 说明

- 未推远程；停住等审
