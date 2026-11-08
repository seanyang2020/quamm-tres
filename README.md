# Quamm TRES

Quamm XOS 的测试资源分配中心：把公司所有 ADB 设备（网络 ADB，tcp 5555）与串口变成**可申请、可排队、可回收、可审计**的资源。

- 面向 **Agent**：只有一个入口 —— MCP Server（`quamm-tres`），凭 token 接入
- 面向 **人**：管理员 / 普通成员凭 rr_ token 登录 Web 控制台（普通成员只看自己的数据）

## 当前状态

**设计已定稿，第一阶段已实现**（可运行骨架 + 模拟设备端到端）。实现评审见 `docs/IMPLEMENTATION_REVIEW.md`。

| 阶段 | 状态 |
|---|---|
| 需求与架构设计 | ✅ 11 份文档 + 9 张框图 |
| 工程脚手架 / 领域模型 / MCP 17 工具 / Web 骨架 | ✅ 已实现 |
| 模拟设备 + 预置 20 token 端到端测试 | ✅ 已实现（`pytest tests/ -q`） |
| 本地部署（WSL Ubuntu，可迁移到其他局域网 Ubuntu） | ✅ 已实现 |

## 目录

```
docs/            # 11 份设计文档（按编号阅读顺序即演进过程）
docs/img/        # 架构图（svg 源 + png 位图）
```

后续代码目录规划（实现时创建）：

```
app/             # FastAPI 应用（Web API :8080 与 MCP Server :8765 同进程）
  domain/        # 领域服务（Web 与 MCP 共用，唯一事实来源）
  api/           # Web 管理接口
  mcp/           # MCP 工具（17 个）
  models/        # SQLAlchemy 模型
  workers/       # 巡检 worker、租约到期扫描
web/             # Vue 3 + Element Plus 控制台
deploy/          # docker-compose（app / worker / postgres 三容器）
tests/           # 端到端测试（含模拟设备）
```

## 文档索引

| 文件 | 内容 |
|---|---|
| `01_需求规格_V1.md` | 需求主文档；§1.1 定位「服务不是门禁」、§1.2 入口原则「Agent 只有一个门就是 MCP」 |
| `02_规则清单与待落实项_V1.md` | 10 个域约 80 条编号规则 |
| `03_MCP接入与工具契约_V1.md` | 原有 9 个工具的契约（**工具数已扩展，见 11 文档 §7.6**） |
| `04_设备注册与身份识别方案_V1.md` | 身份信号、A/B/C/D 分型、10 项打分、五种漂移 |
| `05_建议评审与软件架构_V1.md` | 12 条建议评审、必补风险、进程与端口 |
| `06_子系统清单_V1.md` | 13 个子系统、六条边界约定、三条主链路 |
| `07_三大功能模块_V1.md` | 三域划分（A 申请 / B 注册 / C 管理） |
| `08_A域_Agent身份与注册_V1.md` | A 域早期方案（**主路径已被 09 取代**） |
| `09_A域_邮件分发凭据_V1.md` | **A 域定稿**：邮件即凭据、一人一条、免登录、只认 token |
| `10_B域_设备注册_V1.md` | **B 域定稿**：双入口、probe→register 两段式、登记不受限/入池有门槛、解注册 |
| `11_C域_设备管理_V1.md` | **C 域定稿**：Web 八页、巡检运行状态、通知闭环、Web/MCP 暴露面对照 |

## 三条贯穿全局的判断

1. **租约而非借还登记** —— 申请方是 AI，崩溃不会记得还，必须系统自己回收。TTL 30min + 主动续约 + 宽限 10min，不做高频心跳。
2. **单一事实来源 + 双入口共用** —— Web 与 MCP 调同一套 domain service；同进程部署，事件天然同步。
3. **这是服务，不是门禁** —— 技术上拦不住绕过（5555 局域网开放），也不为此加固；目标是让「按规矩来」足够省事。

## 核心参数

| 项 | 值 |
|---|---|
| 规模 | 15 类机型 · 每类 ≤50 台 · 合计 500–750 台 |
| 接入 | 全部网络 ADB，tcp 5555 |
| 租约 | TTL 30min · 宽限 10min · 单人并发 2 台 |
| 巡检 | 15/30 分钟可配（默认 30）；单轮失败标 suspect，连续 2 轮判 offline |
| 凭据 | `rr_` 前缀 · 一人一条 · 90 天 · 续期不换 token |
| MCP | `quamm-tres`，团队共享单实例，streamable HTTP |

## 版本管理

- **`0.x` 为预发布**：每完成一批可验收的改动就升一位（0.10 → 0.11 → 0.12 ……）。
- **`1.0` 的门槛** = 真实 ADB 探测接入 + 串口资源纳入 + 至少一条真实通知渠道（企微或邮件）打通。
- **版本号只在三个地方出现**：根目录 `VERSION` 文件、`CHANGELOG.md` 标题、git tag；代码里不许硬编码版本字符串。
  `/api/version` 返回 `{"version": "<读 VERSION>", "name": "Quamm RES", "stage": "pre-release"}`。
  其中 `name` 为兼容历史验收脚本（`scripts/verify_v1.py`）保留的 API 契约值，产品展示名统一为 **Quamm TRES**。

## Release 0.11.0

Quamm TRES 0.11.0 预发布（`/api/version` 返回 `{"version":"0.11.0","name":"Quamm RES","stage":"pre-release"}`，`git tag v0.11.0`）。变更明细见 `CHANGELOG.md`。

本版新增：Web 登录（rr_ token 换 8h HttpOnly cookie，admin/user 两档权限）、首次管理员引导、
人员通讯录、发接入凭据（SMTP 邮件发 MCP 配置 token）、凭据续期/吊销台账、租约显示申请人姓名、全局改名 Quamm TRES。

## Release 0.10.0

Quamm TRES 0.10.0 预发布（`git tag v0.10.0`）。变更明细见 `CHANGELOG.md`。

### 能力清单（三域）

- **A 域 · 申请**：`res_whoami` / `res_list_products` / `res_list_devices` / `res_device_info` / `res_apply` / `res_my_leases` / `res_renew` / `res_release` / `res_ticket` / `res_ticket_cancel` / `res_health`
- **B 域 · 注册**：`res_device_probe` / `res_device_register` / `res_device_flash` / `res_device_confirm` / `res_device_unregister`
- **C 域 · 管理**：Web 八页（看板 · 设备 · 占用 · 排队 · 异常/待修复 · 凭据 · 审计 · 设置）+ `res_admin`

### 管理员与 res_admin

仅 `admin@corp.com` 是管理员（`principal.is_admin=True`），其余账号（含 user01~user20）一律 `is_admin=False`。
`res_whoami` 与 `res_admin` 都读这个字段：普通 token 调 `res_admin` 返回 `FORBIDDEN`，且不留任何管理员操作记录。

`res_admin` 的 `action` 枚举：

| action | 说明 | 必填参数 |
|---|---|---|
| `force_release` | 强制回收租约（通知占用人 + 落审计） | `lease_id`、`reason` |
| `overview` | 设备池概览（total / available / suspect / occupied / offline / active_leases） | — |
| `stats` | 汇总统计（devices / leases / queue / notices 四组计数） | — |
| `list_credentials` | 列出全部凭据（token 尾号 / 有效期 / 是否吊销） | — |

### 已知限制

- **真实 ADB 未接**：探测默认 `mock`（`GET /probe` 模拟设备）。`settings.probe_backend=real` 已实现 L0–L3 四级判定，真实板子的 getprop / MAC 键名留 TODO。
- **串口未接**：机型字典保留 `serial_count` 字段，串口注册 / 占用链路未实现。
- **通知渠道仅落库未发信**：`notice` / `notice_receipt` 只写库，SMTP 配置预留、未真正投递。

### 一键验收

```bash
.venv/bin/python scripts/verify_v1.py
```

不依赖 pytest，直接打真服务（MCP + REST），输出一张表
`| # | 检查项 | 期望 | 实际 | PASS/FAIL |`，覆盖身份 / 健康 / 目录 / 申请 / 续约 / 释放 /
管理员 / 排队 offer / 巡检 run / 八页接口；**全部 PASS 且退出码 0 才算 0.10.0 就绪**。

排队 offer 链路真服务实测（confirm 分支 + 超时顺延分支）：

```bash
.venv/bin/python scripts/demo_queue_offer.py
```

### 迁移到别的 Ubuntu

只改 `deploy/.env` 里的库地址、设备网段、SMTP 与域名，三个容器整体搬迁。首次启动后：

```bash
python -m app.seed                         # 生成管理员 + 20 个 token，并做 is_admin 轻量迁移
.venv/bin/python scripts/verify_v1.py      # 一键验收
```

老库升级到 0.10.0 无需清库：启动时 `app.migrations.migrate()` 幂等补 `principal.is_admin` 列并修正管理员标志（仅 `admin@corp.com` 为管理员），现有记录不丢。

## 如何运行

### 0. 环境

- Python 3.11+（WSL Ubuntu 下用 3.11/3.12 均可）
- Node.js 18+（仅构建前端时需要）

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 预置数据（机型字典 + 管理员 + 20 个 token）

```bash
python -m app.seed
# 输出 5 类机型 + 管理员 admin@corp.com + 20 个普通 token（每人 1 条，90 天，is_admin=False）
# 并打印可复制的 MCP 配置（含管理员 token）
```

### 3. 构建前端看板（可选；不构建则退化为内联看板）

```bash
cd web && npm install && npm run build && cd ..
```

### 4. 启动服务（Web + MCP 同进程）

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
# Web 控制台  http://localhost:8080/
# MCP 端点    http://localhost:8080/mcp/   （streamable HTTP）
```

> 进程模型：一个 app 挂两个路由组（Web API + MCP）。生产/容器部署时，
> `docker-compose` 把宿主 `:8765` 映射到容器内 `:8080`，因此对外 MCP 端点写作
> `http://<host>:8765/mcp/`，与 Web 的 `:8080` 分开。

### 5. 巡检 worker（独立进程）

```bash
python -m app.workers.main   # 租约到期扫描(10s) + 巡检(默认 30min)
```

### 6. 跑测试

```bash
pytest tests/ -q
```

### 7. 用任意 token 走通 MCP（客户端配置示例）

```json
{
  "mcpServers": {
    "quamm-tres": {
      "url": "http://localhost:8765/mcp/",
      "headers": {
        "Authorization": "Bearer rr_xxxxxxxx_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

流程：`res_list_products`（查机型）→ `res_list_devices`（查设备）→
`res_apply`（申请，拿到 `adb_address`）→ `res_renew`（续约）→ `res_release`（释放）。

### 8. Docker 部署（app / worker / postgres 三容器）

```bash
cp deploy/.env.example deploy/.env   # 按环境改库地址、设备网段、SMTP、域名
docker compose -f deploy/docker-compose.yml up -d --build
# Web 控制台 http://localhost:8080
# MCP 端点    http://localhost:8765/mcp/
```

迁移到其他局域网 Ubuntu：只改 `deploy/.env` 里的库地址、设备网段、SMTP 与域名，
三个容器整体搬迁。首次启动后执行一次 `python -m app.seed`（或 `docker compose exec app python -m app.seed`）生成 token。

## 模拟演示（20 台 mock 设备 + 租约全流程）

前置：服务已在 `:8080` 运行（见「如何运行」）。一条命令注册 20 台模拟设备：

```bash
.venv/bin/python scripts/demo_seed_devices.py
```

脚本会：

- 在 `127.0.0.1:7001-7020` 起 20 个 mock 设备端点（`tests/mock_device.py` 的 `GET /probe`），机型按 5 类轮转，位置编号 `302-R2-01` … `302-R2-20`；
- 用 20 个 token（`user01~user20@corp.com`）**逐个走真实 MCP streamable HTTP**（`POST /mcp/`，`Authorization: Bearer rr_...`）调用 `res_device_probe → res_device_register`，每台落各自的 `registered_by`；
- 打印注册结果表，并从 `/api/dashboard`、`/api/devices` 复核；退出时关停全部 mock 端点，不残留端口。

注册完成后跑租约演示：

```bash
.venv/bin/python scripts/demo_lease_flow.py
```

依次演示 `res_whoami → res_list_products → res_list_devices(QM102V) → res_apply → res_my_leases → res_renew → res_release`，并确认设备回到可用池。

### 巡检闭环演示（掉线 → 摘出可用池 → 恢复 → 回池）

注册完成后跑巡检闭环（无需先启动服务，脚本在临时 SQLite 上自建 20 台 mock）：

```bash
.venv/bin/python scripts/demo_patrol_cycle.py
```

脚本分四个阶段，并逐轮打印 `scanned / l3 / suspect / offline` 与 dashboard 的
`available / suspect / by_status` 对照：

1. 起 20 台 mock → 巡检一轮 → `l3=20`、`available=20`；
2. 关掉 5 台 → 巡检一轮 → 5 台 `suspect`、`available=15`；
3. 仍关着 → 再巡检一轮 → 5 台 `offline`，产生 `device_offline` 通知；
4. 重新起这 5 台 → 巡检一轮 → 恢复 `l3=20`、`available=20`。

任一项断言失败脚本以退出码 1 结束。需要对着正在运行的服务核对
`/api/patrol/runs` 与 `/api/notices` 时，追加 `--db-url sqlite:///./quamm_res.db`
（脚本仍直接调用领域层，因为巡检当前没有对外触发接口）。

巡检与注册共用同一套探测实现：`settings.probe_backend` 选择 `mock`（默认，GET `/probe`）
或 `real`（`adb connect` + `adb shell getprop`，按 L0/L1/L2/L3 判定）。可用池统一按
`status=available` 且探测健康的口径计算；`suspect` 默认不可申请，可用
`settings.allow_apply_when_suspect=True` 打开"降级仍可用"。

说明：

- 这 20 台是 **mock 设备**，不是真实 ADB。接真实 ADB 时把 `settings.probe_backend`
  改为 `real` 即可，领域层、MCP 工具契约与注册/租约链路都不变（real 后端已实现，
  真实板子的 getprop/MAC 键名留有 TODO）。
- 脚本幂等：mock 设备的 MAC/SN/asset_tag 按序号固定，重复运行会命中已有设备（`matched_as=existing`），不新增记录；位置编号不会重复。
- token 明文只缓存在 `scripts/.demo_tokens.json`（0600，已 gitignore），避免反复吊销；需要重新签发时加 `--rotate`。
