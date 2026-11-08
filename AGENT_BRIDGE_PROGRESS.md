# Quamm TRES 实现进度

> 任务：v0.16 系统设置在线修改 + 设备类型删除 + 无邮箱凭据生成（task_id: t795）

## 状态：已完成（功能 + 单测 + 前端 build + 证据；未 commit / 未 push，停住等审）

## 本轮进展

| 项 | 内容 | 状态 |
|---|---|---|
| 运行时配置 | `app/runtime_config.py`：DB KV 覆盖 pydantic；白名单 9 键；int/bool 范围校验 | ✅ |
| PUT /api/settings | require_admin；非法 400（fields）；返回完整 GET 视图；立即生效 | ✅ |
| 业务读取 | leasing（TTL/offer/grace/max/allow_suspect）、catalog（allow_suspect）、patrol（fail_threshold）、凭据签发（quota/valid_days） | ✅ |
| DELETE 机型 | 设备 / 活动租约 / 活动排队引用闸 → 409；无引用物理删除 | ✅ |
| manual_holders | 占位 `manual-<id>@local`；返回不暴露占位；batch-send 跳过 | ✅ |
| 前端 | 设置可编辑表单；删除二次确认；凭据「仅姓名」条目 + 预览标注 | ✅ |
| 测试 / build | 新增 4 项；全量 47 passed；`npm run build` 成功 | ✅ |

## 结果

- `pytest tests/ -q` → 47 passed（原 43 + 新增 4）
- `web/` `npm run build` → dist 重建成功（vite base 仍为相对 `./`）
- 未重启 uvicorn/worker；未写入 mock 业务数据；未 push

## 未做 / 说明

- 未本地 git commit（等审）；未 push origin
- `web/*.bak-caddyadd` 未跟踪文件未触碰
- worker 进程内 APScheduler 的 `patrol_interval` 仍在启动时注册；改 DB 后巡检/租约业务读值立即生效，调度周期需 worker 重启才变（本任务按要求不重启）
