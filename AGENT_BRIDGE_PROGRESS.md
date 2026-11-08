# Quamm RES 实现进度

> 任务：修复探测层不一致，打通巡检「掉线→摘池→恢复→回池」闭环（task_id: t784）

## 状态：已完成（等审）

## 本轮进展

| 项 | 内容 | 状态 |
|---|---|---|
| 统一探测 | `app/domain/probe.py`：`Prober` 接口 + `MockProber` + `AdbProber(real)`；注册与巡检共用 | ✅ |
| 配置 | `probe_backend=mock|real`（默认 mock）；删除 stub 分支 | ✅ |
| 可用池 | `is_claimable` 统一口径；`allow_apply_when_suspect` 默认 false | ✅ |
| 巡检 | 复用 Prober；单轮 suspect、连续 2 轮 offline+notice、恢复回池保持 | ✅ |
| dashboard | available 按真实可申请数计算，新增 suspect | ✅ |
| 演示脚本 | `scripts/demo_patrol_cycle.py` 四阶段断言 + dashboard 对照 | ✅ |
| 文档 | README「模拟演示」补充巡检闭环命令 | ✅ |
| 测试 | 新增 `tests/test_patrol_cycle.py` 3 条 | ✅ |

## 验证

- `.venv/bin/python -m pytest tests/ -q` → 26 passed（退出码 0）
- `scripts/demo_patrol_cycle.py` → 四阶段全部 PASS（退出码 0）
- 同库接口核对：`/api/patrol/runs` 4 条、`/api/notices` 10 条（5 条 device_offline）
- 证据详见 `AGENT_BRIDGE_EVIDENCE.md`（task_id: t784）

## 未做

- 未接真实板子联调（`AdbProber` 已实现四级判定，getprop/MAC 键名留 TODO）
- 未 push（按任务要求由主控统一推送）
