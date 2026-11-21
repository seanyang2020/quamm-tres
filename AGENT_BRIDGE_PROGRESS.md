# Quamm TRES 实现进度

> 任务：T933 清理节点发放流程中过时的 ADB/fw_setenv（改为开放登记）  
> task_id: t933

## 状态：已完成（停住等审）

## 本轮改动

| 项 | 内容 | 状态 |
|---|---|---|
| A 发放表单 | 移除 ADB 地址；标题改为「发放新节点」；按钮「发放 token」 | ✅ |
| A 发放 API | 前端改调 `/api/wirelog/nodes/provision`；结果弹窗可复制 node_id/token | ✅ |
| B 后端兼容 | `enroll`/`provision_host` 不要求 adb；未知 kwargs 忽略；库字段保留；`fw_setenv` 仍返回给老客户端 | ✅ |
| C 节点详情 | 增加「ADB/fw_setenv 已废弃」轻量提示；已有节点详情不报错 | ✅ |
| D 构建 | `web` 下 `npm run build` exit 0；dist 已生成（gitignore，不入库） | ✅ |

## 验收

- smoke_wirelog / http_wirelog → ALL_PASS
- 开放 enroll（无 adb）+ provision 自测 OK
- `get_node(wle19b7266e729846c)` → online / 4 ports
- 证据：`AGENT_BRIDGE_EVIDENCE.md` + `.agent-bridge/evidence/t933.md`

## 额外改动理由

无。

## 说明

- 提交：`05e454f`（分支 `feat/comres`；任务文案写 master，COMRes 系列均在本分支）
- 停住等审
