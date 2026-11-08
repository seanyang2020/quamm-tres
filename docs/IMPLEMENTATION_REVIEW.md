# 实现评审：Quamm TRES 第一阶段（可运行骨架 + 模拟设备端到端）

> 评审日期：2026-11-08
> 范围：`docs/` 11 份设计文档 → 第一阶段代码落地
> 结论：**无阻塞性矛盾，可直接进入实现。** 所有已发现的冲突均被任务要求或较新文档明确拍板，本文记录结论与默认建议。

---

## 1. 文档间冲突与缺口

### 1.1 MCP 工具数量三处不一致（已定稿：17）

| 来源 | 数量 | 说明 |
|---|---|---|
| `03_MCP接入与工具契约_V1.md` §3 | 9 | 原始九工具，文档自身 §9 已声明"工具数已扩展，见 11 文档 §7.6" |
| `10_B域_设备注册_V1.md` §7 | 13 | 说"把工具数从 9 改成 13"，只加了注册类 4 个，漏了 11 文档 §7.4 新增的 3 个（device_info/whoami/health） |
| `11_C域_设备管理_V1.md` §7.6 | **17** | 9 + 注册 5（probe/register/flash/confirm/unregister）+ 新增 3（device_info/whoami/health）|

**定稿 17 个**，与任务要求一致。10 文档 §7 的"13"是中间态，已过期。03 文档 §9 明确指到 11 文档，无冲突。

### 1.2 每人凭据数：09 文档残留"每人 3 条"（已定稿：1 条）

`09_A域_邮件分发凭据_V1.md` 通篇写"每人 3 条"（§1 步骤 2、§2.2、§5、§6 表、§8、§9 G1）。但：
- 任务要求明确"**预分配 20 个 token（20 个邮箱，每人 1 条，写入 seed）**"
- `README.md` 核心参数表写"凭据 | `rr_` 前缀 · **一人一条** · 90 天"

**定稿：一人一条。** 09 文档的"3 条"是 V1.1 修订遗留的旧口径，已被任务覆盖。代码层面 `credential` 对 `principal` 是一对一（一人一条），但数据模型保留扩展空间（不硬编码 seq=3）。

### 1.3 holder 并发上限：01 文档写 3，其余写 2（已定稿：2）

| 来源 | 值 |
|---|---|
| `01_需求规格_V1.md` §5.3 | 3 台（"单 holder 并发占用上限 3 台"）|
| `02` R-B11、`03` §8、`08` §4、`README` | 2 台 |

**定稿：默认 2，可配。** 01 是 V1 讨论稿，未随 02/03 修订同步。任务要求明确"holder 并发上限 2"。

### 1.4 心跳模型：01 文档残留心跳端点（已定稿：TTL+续约+宽限）

`01` §5.2 状态机与 §9 API 草案仍写 `heartbeat`（30s），与 `02` R-B3~R-B7、`03` §4.2 的"MCP 场景不做高频心跳，改 TTL + 主动续约 + 宽限 10min"冲突。

**定稿：TTL(30min) + 主动续约 + 宽限(10min)。** 01 是讨论稿，03 §9 已明确同步修正 02 的 R-B3~R-B5。第一阶段**不实现 heartbeat 工具**。

### 1.5 设备主键：02 文档头仍写"MAC 为主键"（已定稿：内部 device_id）

`02` 文档头部元信息"设备身份基线 | MAC 地址为主键（用户已确认默认假设）"与 `04` G1、`05` §3.1、任务要求"device 用内部 device_id 做主键，不要用 MAC 做主键"冲突。

**定稿：内部 `device_id` 主键，MAC/SN/asset_tag/position_code 建唯一索引。** 02 文档头是过时描述，04 文档 G1 已按此结论推进，任务已拍板。

### 1.6 表结构收敛（缺口，非冲突，已确认可接受）

文档 02/04/08/10 提出 `identity_claim`、`identity_event`、`port`、`agent`、`agent_auth_request`、`credential_event` 等表。任务第一阶段只要求 9~10 张表：

`product_type` / `device` / `principal` / `credential` / `lease` / `queue_entry` / `audit_event` / `notice` / `notice_receipt` / `patrol_run`

**收敛决策**：身份声明（identity_claim/event）与串口（port）推迟到二期；`mac/sn/asset_tag/position_code` 唯一索引直接落在 `device` 表上，一期不做"多历史声明可退役"的完整模型。`registered_by` / `register_channel` 作为溯源字段直接落在 device（对齐 10 文档 §1.2）。

### 1.7 位置编号格式自相矛盾（缺口）

`10` §6 要求严格格式 `房间-排-位`（如 `302-R2-05`）"格式不符直接拒绝"；但 `10` §1.5 又允许"工位/临时/桌面"等非正式位置。**第一阶段默认：`position_code` 非空 + 全局唯一，不强制正则。** mock seed 使用正规格式（`302-R2-01` 等）。

### 1.8 MCP streamable HTTP 实际端点带尾斜杠

FastMCP 4.x 的 streamable HTTP 端点在挂载后重定向到 `/mcp/`（尾斜杠）。客户端配置示例必须用 `http://host:8765/mcp/`。这是实现层事实，非文档冲突。

---

## 2. 必须拍板才能起代码的点（最多 5 条 + 默认建议）

| # | 问题 | 文档依据 | **默认建议（已按此实现）** |
|---|---|---|---|
| 1 | 设备主键方案 | 04 G1 / 02 头 / 任务 | **内部 `device_id` 主键 + MAC/SN/asset_tag/position_code 唯一索引**（已拍板，无返工风险）|
| 2 | 每人凭据数 | 09（3 条）vs 任务/README（1 条）| **1 条**；credential 与 principal 一对一，模型不写死 seq |
| 3 | holder 并发上限 | 01（3）vs 02/03/08（2）| **2 台，可配**（`settings.holder_concurrency_limit`）|
| 4 | 进程/端口模型 | 05 §6、README、任务 | **单 app 挂两路由组**：Web 路由 + MCP 挂载 `/mcp`。本地 `uvicorn app.main:app --port 8080`（Web :8080/，MCP :8080/mcp/）；docker-compose 映射 `8765→8080`，对外 `:8765/mcp/` 即 MCP 规范端口 |
| 5 | 身份声明表是否一期做 | 02/04/08 vs 任务表清单 | **一期收敛到 device 唯一索引，不做 identity_claim/event**；二期补"多声明可退役" |

补充：`res_admin` 的"普通 token 不注册"语义——FastMCP 启动时统一注册工具，per-request 动态工具列表需额外机制。**默认：注册 17 个，`res_admin` 运行时校验管理员身份并拒绝非管理员**，审计留痕。若需严格"不可见"，二期再上动态过滤。

---

## 3. 实现顺序与阶段划分

| 阶段 | 内容 | 产出 |
|---|---|---|
| 0 脚手架 | config / db / models / security / 机型字典 | 表结构 + token 哈希 + `product_type` 5 类 |
| 1 领域服务 | domain：catalog（探测/注册/注销）、leasing（申请/续约/释放/过期）、queueing（offer/顺延）、patrol（suspect/offline/恢复）、notices、audit | 全部业务规则，Web/MCP 共用 |
| 2 MCP | 17 个工具 + 鉴权中间件（Bearer/`?t=`，日志脱敏）| `quamm-res` MCP 单实例 |
| 3 Web | REST `/api/*` + Vue3+Element Plus 看板（看板/设备/占用/凭据四页，其余占位）| 最小可用看板 |
| 4 worker | 巡检循环 + 租约到期扫描（APScheduler，独立进程）| 状态自动流转 + notice |
| 5 模拟与测试 | mock 设备 + seed（20 token/20 邮箱）+ pytest 端到端 | `pytest tests/ -q` 全绿 |
| 6 部署 | docker-compose（app/worker/postgres）+ `.env` + README「如何运行」| 可迁移部署 |

依赖关系：0→1 严格；2、3 依赖 1；4 依赖 1；5 依赖 2；6 收尾。
