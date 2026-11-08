---
task_id: t786
---

# quamm-tres 接入真实 XOS 设备 验收证据（0.10.0）

## a. 编译/构建

| 项 | 命令 | 退出码 | 关键结果 |
|---|---|---|---|
| Python 编译 | `.venv/bin/python -m py_compile app/domain/prober_real.py app/domain/probe.py app/domain/patrol.py app/domain/catalog.py app/models.py app/migrations.py app/config.py app/api/router.py app/main.py scripts/register_real_fleet.py` | 0 | 10 个文件 COMPILE_OK |
| 前端构建 | `cd web && npm run build` | 0 | `dist/index.html`、`dist/assets/index-CcbOVeAg.js`(1,047.60 kB)、`dist/assets/index-DoZXzR03.css` 生成；构建产物含「工程配置」列（grep 命中） |
| 单测 | `.venv/bin/python -m pytest tests/ -q` | 0 | `30 passed in 15.58s` |

（所有可能阻塞/远程命令均以 `timeout` 包裹：单点探测 20s、机队注册 60s、adb 命令内部 GNU timeout 3s、verify_v1 180s。）

## b. 改动文件清单

```
app/domain/prober_real.py        新增：RealXosProber（adb connect/shell 读文件，无 getprop，L0/L3 判定）
app/domain/probe.py              real 后端改接 RealXosProber；保留 AdbProber 旧名兼容既有单测；
                                 新增 get_prober_for(source) / probe_device(source=) 按 source 分流
app/domain/patrol.py             巡检按 device.source 把 source 传给 probe_device
app/domain/catalog.py            _public_device 暴露 project_name/source/board_dt/storage_type/has_wifi；
                                 device_probe 增加可选 source 参数
app/models.py                    device 表加 project_name/board_dt/storage_type/has_wifi/source 五字段
app/migrations.py                新增幂等 ALTER（旧库不丢数据；既有行 source 补成 'mock'）
app/config.py                    probe_backend 注释补 real 语义
app/api/router.py                /api/dashboard 增 projects（工程配置名分布，含 project_name）
scripts/register_real_fleet.py  新增：14 台真实设备探测/注册；连不上或探测不完整均入库为 pending；
                                 position_code REAL-AUTO-01..14、owner=auto-import、
                                 model_class 匹配不到字典时用 project_name 本身（补 product_type 满足外键）
web/src/App.vue                  设备表加「工程配置」列
web/vite.config.js               本任务前既有未提交的 caddyadd 子路径 base 配置（随本任务一并提交，默认 '/' 无影响）
web/src/App.vue                  同时含本任务前既有未提交的 caddyadd API_BASE 微调（同一文件，无法拆分，一并保留提交）
AGENT_BRIDGE_PROGRESS.md         更新本轮进度
AGENT_BRIDGE_EVIDENCE.md         本文件（验收四要素）
```

未提交、保留不动（属其他任务遗留备份，非本任务产物）：
`web/dist.bak-caddyadd/`、`web/src/App.vue.bak-caddyadd`、`web/vite.config.js.bak-caddyadd`

## c. 版本/提交证据

```
$ git log -1 --oneline
9a60ffc feat: 接入真实 Quamm XOS 设备（RealXosProber + source 分流 + 设备指纹字段 + 机队注册 + 工程配置列）
        ↑ 本任务主提交（功能）；其后另有 AGENT_BRIDGE_* 证据定稿提交

$ git status --short
（功能提交后：AGENT_BRIDGE_EVIDENCE.md / AGENT_BRIDGE_PROGRESS.md 待提交；
 另有 3 个未跟踪 .bak-caddyadd 备份；无其它改动）

$ git diff --stat 6579187 9a60ffc
 11 files changed, 550 insertions(+), 134 deletions(-)  （本任务主提交的代码改动统计）
```

- 提交前基线 HEAD：`6579187 chore: 服务端口 8080 -> 9100，避开本机常用端口`
- `VERSION`：0.10.0（未改动）
- Gerrit change：无/不适用（remote 为 GitHub SSH `git@github.com:seanyang2020/quamm-tres.git`，无 Gerrit）
- push：`git push origin main`（SSH，已配好）

## d. 验证/自测

### 1. RealXosProber 单点校验（验收项）

命令：`timeout 20 .venv/bin/python -c "from app.domain.prober_real import RealXosProber; r=RealXosProber().probe('172.16.10.192',5555); ..."` → 退出码 0

```
product_name = moto_ld
固定 mac      = 00:0e:c6:7b:24:ba
storage       = nand
level         = L3
```
（wlan_mac=`fe:ab:7f:4f:cf:4d` 为随机地址，按规则排除，主键取 eth0 固定 mac。）

补充：`.87` 无 eth0，wlan0=`88:00:33:77:d0:80` 非 fe: → fixed_mac 回退 wlan0，product=`qm10xh_full`，storage=nand，L3；`.20`/`.85` adb offline → L0。

### 2. 14 台真实设备（命令 `timeout 60 .venv/bin/python scripts/register_real_fleet.py`，退出码 0）

| IP | product_name | 固定 mac | storage | level | status | position |
|---|---|---|---|---|---|---|
| 172.16.10.192 | moto_ld | 00:0e:c6:7b:24:ba | nand | L3 | available | REAL-AUTO-01 |
| 172.16.10.20 | - | - | unknown | L0 | pending | REAL-AUTO-02 |
| 172.16.10.246 | qm10xv_full | 72:8d:a7:1d:22:f8 | unknown | L3 | available | REAL-AUTO-03 |
| 172.16.20.206 | qm10xh_full | 04:a1:a2:a3:a4:a5 | unknown | L3 | available | REAL-AUTO-04 |
| 172.16.20.212 | esl2 | - | nand | L0 | pending | REAL-AUTO-05 |
| 172.16.20.71 | esl2 | - | nand | L0 | pending | REAL-AUTO-06 |
| 172.16.60.104 | - | - | unknown | L0 | pending | REAL-AUTO-07 |
| 172.16.60.16 | - | - | unknown | L0 | pending | REAL-AUTO-08 |
| 172.16.60.245 | framex | a0:ac:78:a0:d7:48 | unknown | L3 | available | REAL-AUTO-09 |
| 172.16.60.49 | framex | a0:ac:78:a0:d7:4c | unknown | L3 | available | REAL-AUTO-10 |
| 172.16.60.57 | - | 84:1d:e8:c9:41:4e | unknown | L0 | pending | REAL-AUTO-11 |
| 172.16.60.85 | - | - | unknown | L0 | pending | REAL-AUTO-12 |
| 172.16.60.87 | qm10xh_full | 88:00:33:77:d0:80 | nand | L3 | available | REAL-AUTO-13 |
| 172.16.60.92 | - | 0c:c1:19:90:0b:eb | unknown | L0 | pending | REAL-AUTO-14 |

合计 14 台全部入库：6 台 L3/available（moto_ld、qm10xh_full×2、qm10xv_full、framex×2，≥4 台），8 台 pending。
- `esl2` 两台：无 eth0、wlan0 为 `fe:` 随机地址 → fixed_mac=None → 按"fe:/x2 随机地址不能当主键"规则记 L0/pending（仍入库）。
- `172.16.10.20` / `172.16.60.85`：adb offline，连不上 → pending。
- 其余探测不完整（无 product_name / 无 mac）的按用户明确要求"探测不完整也要入库"记 pending。

### 3. dashboard / 设备接口返回 project_name

- `GET /api/dashboard`（200）：`projects = [{'project_name':'-','count':28}, {'esl2':2}, {'framex':2}, {'moto_ld':1}, {'qm10xh_full':2}, {'qm10xv_full':1}]`
- `GET /api/devices?limit=500`（200）：返回 36 行，14 台 `source=real` 均带 `project_name` 与 `model_class`（=project_name），所有行都含 `project_name` 键。

### 4. 不破坏 mock 路径（边界）

- 22 台 mock DB 复核：全部 `source=mock` / `available` / `L3`，巡检后仍 22/22。
- 分流断言：`get_prober_for('mock')`→MockProber、`get_prober_for('real')`→RealXosProber、`get_prober_for(None)`→MockProber。
- 新代码 scoped patrol（仅 22 台 mock，退出码 0）：`scanned=22 l3=22 suspect=0 offline=0`。

### 5. verify_v1.py

命令：`timeout 180 .venv/bin/python scripts/verify_v1.py` → 退出码 0

```
合计：32 项，PASS 32，FAIL 0
结论：0.10.0 就绪（全部 PASS）
```

### 6. pytest

命令：`.venv/bin/python -m pytest tests/ -q` → 退出码 0，`30 passed in 15.58s`。

### 7. 边界自检

- `docs/` 下设计文档未改动。
- `getprop` 仅出现在注释里（明确"无/不调用"），代码中无任何可执行 `getprop`；真实探测只读文件（`cat` / `test -d`），无 push/remount/刷机。
- 迁移只 `ALTER TABLE ADD COLUMN`，旧库 22 条记录不丢；`mac` 唯一索引沿用未改。
- `scripts/verify_v1.py` 本身未修改。
