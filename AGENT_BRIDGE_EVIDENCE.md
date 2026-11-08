---
task_id: t784
---

# Quamm RES：统一探测层 + 巡检闭环 验收证据

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| 语法编译 | `.venv/bin/python -m py_compile app/domain/probe.py app/config.py app/constants.py app/domain/catalog.py app/domain/leasing.py app/domain/patrol.py app/api/router.py app/mcp/server.py scripts/demo_patrol_cycle.py tests/test_patrol_cycle.py` | 0 | 10 个文件编译通过（COMPILE_OK） |
| 单测 | `.venv/bin/python -m pytest tests/ -q` | 0 | `26 passed in 15.30s`（原有 23 + 新增 3） |
| stub 清理 | `grep -rn "is a stub" app tests scripts README.md` | 1（无匹配） | 不再出现 "is a stub" |

## b. 改动文件清单

```
app/config.py                    probe_backend(mock|real) + allow_apply_when_suspect
app/constants.py                 is_claimable 统一可用池口径
app/domain/probe.py              Prober 接口 + MockProber + AdbProber(real)，删除 stub
app/domain/catalog.py            注册探测共用 Prober；list_products/list_devices 按可申请数
app/domain/patrol.py             巡检复用同一 Prober；L3 恢复 / suspect / offline
app/domain/leasing.py            _find_available 与可用池同口径
app/api/router.py                dashboard available 按真实可申请数，新增 suspect
app/mcp/server.py                res_list_devices 默认 claimable_only；overview 同口径
app/main.py                      启动初始化探测后端（默认 mock）
deploy/.env.example              RES_PROBE_BACKEND / RES_ALLOW_APPLY_WHEN_SUSPECT
tests/conftest.py                改用 MockProber
tests/test_patrol_cycle.py       新增巡检闭环 3 条用例
scripts/demo_patrol_cycle.py     新增四阶段闭环演示脚本
README.md                        「模拟演示」补充巡检闭环
```

## c. 版本/提交证据

```
$ git log -1 --oneline
9b320e4 fix: 统一 mock/real 探测实现，巡检闭环与可用池口径对齐

$ git status --short
（仅 .agent-bridge/ 未跟踪；证据文档本次另起 docs commit）

$ git show --stat --oneline HEAD
 README.md                    |  29 ++++-
 app/api/router.py            |  14 ++-
 app/config.py                |  10 +-
 app/constants.py             |  17 ++-
 app/domain/catalog.py        |  18 ++-
 app/domain/leasing.py        |   8 +-
 app/domain/patrol.py         |  12 +-
 app/domain/probe.py          | 207 +++++++++++++++++++++++------
 app/main.py                  |   4 +-
 app/mcp/server.py            |  40 ++++++-
 deploy/.env.example          |   6 +-
 scripts/demo_patrol_cycle.py | 279 +++++++++++++++++++++++++++++++++++
 tests/conftest.py            |   4 +-
 tests/test_patrol_cycle.py   | 141 +++++++++++++++++++
 14 files changed, 726 insertions(+), 63 deletions(-)
```

- Gerrit change：无/不适用（按要求未 push）

## d. 验证/自测

### 1. 巡检闭环脚本（四阶段全部 PASS，退出码 0）

```
$ .venv/bin/python scripts/demo_patrol_cycle.py
[阶段1 全部在线] scanned=20 l3=20 suspect=0 offline=0
        dashboard: available=20 suspect=0 by_status={'available': 20}
[阶段2 关闭 5 台] scanned=20 l3=15 suspect=5 offline=0
        dashboard: available=15 suspect=5 by_status={'available': 15}
[阶段3 连续第 2 轮失败] scanned=20 l3=15 suspect=0 offline=5
        dashboard: available=15 suspect=0 by_status={'available': 15, 'offline': 5}
[阶段4 恢复 5 台] scanned=20 l3=20 suspect=0 offline=0
        dashboard: available=20 suspect=0 by_status={'available': 20}
run1: scanned=20 l3=20 suspect=0 offline=0
run2: scanned=20 l3=15 suspect=5 offline=0
run3: scanned=20 l3=15 suspect=0 offline=5
run4: scanned=20 l3=20 suspect=0 offline=0
/api/patrol/runs 条数 = 4；/api/notices 条数 = 10
结果: PASS
```

### 2. 对服务同库核对接口（TestClient，退出码 0）

```
$ RES_DATABASE_URL=sqlite:////tmp/quamm_patrol_api.db python - <<'PY'
（demo_patrol_cycle.py --db-url 同库跑完后读取 /api/patrol/runs、/api/notices、/api/dashboard）
patrol runs: 4
  20 20 0 0
  20 15 5 0
  20 15 0 5
  20 20 0 0
notices total: 10 device_offline: 5
dashboard available/suspect/by_status: 20 0 {'available': 20}
```

### 3. 单测

```
$ .venv/bin/python -m pytest tests/ -q
26 passed in 15.30s
```

新增用例：
- `test_patrol_cycle_offline_and_recover`：5 台全在线→关 2 台 suspect/池 3→offline+notice→重启回池 5
- `test_suspect_not_claimable_unless_configured`：默认 suspect 排队，开关打开后可申请
- `test_prober_backend_selection_and_no_stub`：mock/real/默认/未知值后端选择
