# t1060 COMRes 心跳间隔按节点上报 — 进度

> 任务：honor node heartbeat_interval_s；按节点 stale；页面心跳年龄  
> task_id: t1060  
> 工作区：`/home/scm/worktree/tres-comres-heartbeat`  
> 分支：`fix/comres-heartbeat`（基线 0.54.1 / 4fb12e2）

## 状态：已完成（停住等审）

## 本轮工作

| 项 | 内容 | 状态 |
|---|---|---|
| register/heartbeat | 上报优先；持久化 config_json；非法 → COMRES_BAD_HEARTBEAT_INTERVAL | ✅ |
| stale helpers | `node_heartbeat_interval_s` / `node_stale_timeout_s`；floor 默认 180 | ✅ |
| prune / expire | 逐节点超时；prune 默认 60s（scheduler min 30） | ✅ |
| wirelog / reach | list_nodes 只读 stale + age；probe 跳过超时节点 | ✅ |
| UI | 最近心跳年龄 + 超时标签；详情间隔/年龄 | ✅ |
| 测试 | test_comres_heartbeat 8；comres+wirelog 65 passed | ✅ |
| VERSION | 0.54.2 + CHANGELOG | ✅ |
| 提交 | `5677478` fix(comres): honor node heartbeat interval (0.54.2) | ✅ |
| 部署/push | 未部署 / 未 push | ✅ |

## 已知限制

- `tests/smoke_comres.py` 仍有既有失败：端点独占导致「双节点同 ip:port」count=1；F6 下 auto 占位 `allocated=false`；与本改无关
- 未改鉴权/申请契约、未做破坏性迁移
