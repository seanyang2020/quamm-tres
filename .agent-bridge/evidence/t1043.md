---
task_id: t1043
---

# t1043：P0 probe source guard（禁止 mock 静默落库）

## a. 编译/构建

- 命令：`timeout 60 /home/scm/github_gitee/quamm-res/.venv/bin/python -m py_compile app/constants.py app/config.py app/runtime_config.py app/domain/probe.py app/domain/catalog.py app/models.py app/migrations.py app/mcp/server.py app/main.py`
- 退出码：`0`
- 关键结果：语法编译通过（无独立前端构建；Web 仅改 `App.vue` 选项文案）

## b. 改动文件清单

- `app/constants.py` — 新增 `ProbeSource`（real/serial/mock/unknown + PRODUCTION/ALL）
- `app/config.py` — `probe_backend` 默认 `real`；新增 `allow_mock_probe=False`
- `app/runtime_config.py` — 运行时键 `allow_mock_probe`；保留 probe_backend real/mock
- `app/domain/probe.py` — `build_prober` 未知值抛 `ValueError`，不再回退 mock
- `app/domain/catalog.py` — `_resolve_probe_source`；probe/register 闸门；audit/source；`_public_device` 输出 `source`/`simulated`
- `app/models.py` — `Device.source` 默认 real；只读属性 `simulated`
- `app/migrations.py` — 新列默认 real；空值标 unknown；删除刷 mock 逻辑
- `app/mcp/server.py` — `res_device_probe` docstring 标明 mock 仅开发/测试
- `app/main.py` — 启动注释对齐默认 real
- `web/src/App.vue` — 去掉全局 mock 探测选项；注明生产只允许 real
- `tests/conftest.py` — 测试显式 `allow_mock_probe=True` + `probe_backend=mock`
- `tests/test_patrol_cycle.py` — 适配 build_prober 默认 real / 未知抛错
- `tests/test_probe_source_guard.py` — P0 闸门单测（新建）

## c. 版本/提交证据

- 分支：`feat/probe-source-guard`
- `git log -1 --oneline`：`41ae518 chore(release): v0.51.0；real 探测器兼容交互式 adbd 变体`
- `git status --short`：12 modified + 1 untracked（见上清单）；**未 commit、未 push**
- `git diff --stat`：`12 files changed, 126 insertions(+), 37 deletions(-)`（不含未跟踪测试文件行数）
- Gerrit change：不适用
- 部署：未部署 201；未写生产 DB

## d. 验证/自测

### d1. 目标测试

```
timeout 90 …/python -m pytest tests/test_probe_source_guard.py tests/test_patrol_cycle.py tests/test_pending_probe.py -q
```

- 退出码：`0`
- 结果：`18 passed`

### d2. 全量测试

```
timeout 600 …/python -m pytest -q --tb=no
```

- 结果：`210 passed, 17 failed, 37 warnings`
- 失败清单与归属（相对 base `41ae518` 对照，同域已红，**非本次引入**）：
  - `tests/test_apply_flow.py`：`test_list_products_and_whoami`、`test_product_metadata_completeness_and_stats`（products 夹具/口径，base 已失败）
  - `tests/test_flash.py`：10 个 AttributeError（flash 夹具，base 已失败）
  - `tests/test_mcp_http.py`：`test_mcp_http_full_flow`、`test_mcp_http_list_products_never_empty`
  - `tests/test_queue.py`：`test_apply_by_product_type_queues_in_model_queue`
  - `tests/test_registration.py`：`test_register_20_devices`、`test_probe_unreachable_registers_pending`（base 已失败：available=0 / apply 无 state）
