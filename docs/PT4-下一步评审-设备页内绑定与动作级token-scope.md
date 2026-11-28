# PT4 下一步评审：设备页内绑定 · 动作级 token scope / 关电保护

> 任务：PT4-review｜分支 `feat/capabilities`｜**只评审落盘，不改业务代码**  
> 上游：PT3（A 方案）已落地未 commit；单测 19 passed；本地 172.16.10.25:9100；生产 172.16.1.201:9100 仍为老版本。  
> 核查方式：只读 grep/read（`powerres.py` / `powerres_router.py` / `serial.py` / `web_auth.py` / `App.vue` / `mcp/server.py`）。

---

## 0. 结论先行

| 候选 | 判定 | 一句话理由 | 收益 | 风险（最坏） | 工作量 |
|------|------|------------|------|--------------|--------|
| **A** 详情页内绑定闭环 | **APPROVE_WITH_CHANGES** | 三跳绑定是真痛点，但**不必新 inventory 读接口**；关键是节点归属推断 + 页内选通道直接 bind | 运维/测试在 `#/device/{id}` 一次绑完，少切 CtrlRes、少在设备下拉里找本机 | 绑错通道 / 抢绑他机通道导致 DUT 误控电；节点不明时误选 inventory | **M**（前端为主；可选小 API） |
| **B** 动作级 scope / 关电保护 | **APPROVE_WITH_CHANGES**（轻量）；**REJECT** 完整 MCP token scope | 无只读角色；非 admin 已靠租约；真正缺口是 **hold_ms 无上限** + **power 无二次确认/显式允许位**；重 scope 过早 | 防误断电、防恶意/脚本把 hold 拉到分钟级占 holder；上线 201 前降运维事故面 | 过度收紧导致日常 PowerRes 卡片/详情页「关/重启」全 403；漏检 MCP 路径 | **S**（clamp）+ **S–M**（confirm/allow）；完整 scope **L**（不做） |

### 优先顺序（拍板建议）

1. **先做 B-lite（PT4b）**：服务端 `hold_ms` 夹紧 + power_off/cycle 二次确认（或复用 `PowerResBinding.enabled` 作为关电闸）。上线生产前应具备。
2. **再做 A（PT4a）**：详情页选通道 → 直接 bind/unbind 闭环，狗粮体验与减少绕路。
3. **不做 / 远期**：MCP/agent 动作级 token scope、per-device owner 复杂 ACL——等多租户或 agent 误操作有实证再开。

**比 A/B 更小更值、可夹在 PT4b 头部的事**：仅 `hold_ms` 服务端 `[500, 30000]` 夹紧（前端已 500–60000，服务端 `max(0, int)` 无上限）——单独也可叫 **PT4b0**，约 **S**，建议与 PT4b 同 PR 或先合。

---

## 1. 事实基线（只读核查）

### 1.1 权限与角色

| 项 | 现状 |
|----|------|
| Web 角色 | `web_auth` 仅 **`admin` \| `user`**，**无 readonly 角色** |
| 写操作门禁 | `require_login` / `require_admin`；PowerRes 管理面均为 `require_login` |
| 串口/电源执行 | `serial_svc.check_access`：`admin` 直接放行；非 admin 须持有该设备**活跃租约**；仅 `mode_edl` 额外要求 admin + `confirm=true` |
| power_on/off/cycle | **无**额外授权位；与普通串口动作同闸 |
| MCP | `res_serial_action` 等同走 `check_access` + `run_action`；actor 为 `agent_label (email)` 或 `email`（无 `web:` 前缀） |

**口径纠正**：任务描述中「只读角色」——当前产品**不存在**独立 readonly；边界是「未登录 / user+无租约 / user+有租约 / admin」。

### 1.2 hold_ms 与审计

| 项 | 现状 |
|----|------|
| 服务端 | `run_action`：`hold = POWER_CYCLE_HOLD_MS if hold_ms is None else max(0, int(hold_ms))` —— **仅下界 0，无上界** |
| 前端 | PowerRes 行 / 详情页 `el-input-number`：**500–60000**（与 PT3 设计「500–60000」一致；候选文案「30000」更严） |
| 审计 `_audit` | `actor=by`（Web 为 `web:{email}`）、`action=serial_action`、detail 含 `action_key`/`result`/`duration_ms` 等；**无独立 source 字段**，但 actor 前缀可区分 web；MCP 侧靠 label/email |
| 结论 | 追责「谁操作」**基本够用**；缺的是 **来源通道显式字段**（可选增强）与 **防滥用 hold**，不是缺 actor |

### 1.3 PowerRes inventory / bind（对候选 A）

| 接口 | 能力 | 是否够用 |
|------|------|----------|
| `GET /api/powerres/overview` | 有 inventory 或 binding 的节点列表 | 详情页选节点够用 |
| `GET /api/powerres/inventory?node_id=` | 返回通道 + `bound` / `bound_device_id` / `bound_slot` / `endpoint` | **够用**；前端可滤「未绑 / 绑他机 / 已绑本机」；**不必**新增 `unbound=1` |
| `POST /api/powerres/bind` | `node_id, power_dev, channel, endpoint, device_id, slot?`；slot 可省略由 `_allocate_slot` | **够用**；详情页固定 `device_id=本页` |
| `POST /api/powerres/unbind` | 按通道解绑 | 详情页**已实现** `doDevicePowerUnbind` |
| `GET /api/powerres/bindings?node_id=` | 仅按节点；**无 `device_id` 过滤** | 详情页现用 overview×N 拉 bindings 再滤本机（能用偏慢） |

**设备无 `node_id` 列**：节点归属需从 COMRes `WireLogPortBinding`、已有 PowerRes binding、或用户手选 overview 节点推断——这是 A 的主要产品缺口，不是缺 inventory API。

**范围澄清**：候选 A 正文是「列本节点未绑/绑别处通道 → 直接 bind」，**不是**在详情页做 inventory upsert「新建电源设备」。新建通道仍归 CtrlRes PowerRes 卡片 / hub sync。

### 1.4 其它相关现状

- `PowerResBinding.enabled`：库字段存在；capabilities 过滤 `enabled=True`；**`run_action` 不读 binding.enabled**（只认端口/动作 enabled）。
- 详情页空态「去绑定」= `goBindPowerRes` → `#/ctrlres?card=powerres`（三跳起点属实）。
- 前端 hold 上限 60s；服务端可被 MCP/curl 打到任意大值并 `sleep(hold/1000)` 占 holder。

---

## 2. 候选 A：详情页内绑定闭环

### 2.1 判定

**APPROVE_WITH_CHANGES**

理由：痛点真实（空态只能跳走再选通道再搜设备）；后端 bind/unbind/inventory **已具备**；改动重心在详情页 UX + 节点选择策略。拒绝「为 unbound 专开读接口」与「在详情页登记新 inventory」。

### 2.2 收益 / 风险 / 工作量

- **收益**：设备负责人、现场测试在详情页完成「看见空电源 → 选通道 → 绑定 → 立刻开/关/重启」；减少 CtrlRes 往返与设备下拉里找本机。
- **风险**：节点选错 → 列出无关 inventory；一键抢绑「已绑他机」通道导致他机失电控制；endpoint 为空的 inventory 行误点 bind 400。
- **工作量**：**M**（主要 `App.vue`；可选 `bindings?device_id=` 为 S）。

### 2.3 ADR 式取舍

| 方案 | 内容 | 选/不选 |
|------|------|---------|
| **A1（推荐）** | 详情页：选/推断 `node_id` → `GET inventory` 前端滤未绑优先、绑他机次之 → 选中行带 `endpoint` 调已有 `POST bind`（`device_id` 固定本页，slot 省略）；解绑保持现有 | **选**：零新契约或仅可选增强 |
| **A2** | 新增 `GET /api/powerres/inventory?unbound=1` 或 `GET …/devices/{id}/powerres/candidates` | **不选（本期）**：现 inventory 已含 bound 字段；服务端过滤收益小 |
| **A3 不做** | 继续「去绑定」跳 CtrlRes | **不选**：PT3b 空态体验未闭环，日常抱怨会反复出现 |
| **A4（可选顺带）** | `GET /api/powerres/bindings?device_id=` 减轻详情页 N 节点扫描 | **可选**，不阻塞 A1 |

### 2.4 落地清单 — PT4a（若通过）

| 项 | 内容 |
|----|------|
| **任务 id** | PT4a |
| **改动文件** | `web/src/App.vue`（详情页电源卡：节点选择/推断、可绑通道列表、绑定确认、解绑已有）；可选 `app/api/powerres_router.py` + `app/domain/powerres.py`（`list_bindings` 增 `device_id` query）；`tests/test_powerres.py`（若做可选 API） |
| **接口契约** | **复用**：`GET /overview`、`GET /inventory?node_id=`、`POST /bind`、`POST /unbind`。**可选**：`GET /bindings?device_id=` → `list[binding]`。**不新增** `unbound=1`。bind body：`{node_id, power_dev, channel, endpoint, device_id}`，`slot` 省略。 |
| **节点推断规则（建议）** | ① 本机已有 PowerRes binding 的 `node_id`；② 否则 COMRes `WireLogPortBinding.bound_device_id==本机` 的 `node_id`（待实现时读现有 devices/comres API 或扩展详情载荷——**待确认**用哪条现成读路径）；③ 都没有 → UI 强制选 overview 节点。 |
| **验收标准** | 空电源详情页可不离开本页完成绑定；绑定后立刻出现开/关/重启；本机已绑可解绑；绑他机通道须二次确认文案；endpoint 空禁用绑定；未登录仍 401。 |
| **单测点** | 若加 `device_id` query：仅返回该设备 bindings。前端逻辑以手工/浏览器验收为主（现网无 Vue 单测惯例）。域层 bind 已有测，回归 `tests/test_powerres.py`。 |
| **非目标** | 详情页 upsert inventory、新建电源设备、改 bind 唯一键语义。 |

---

## 3. 候选 B：动作级 token scope / 关电保护

### 3.1 判定

- **B-lite：APPROVE_WITH_CHANGES**（建议先做）
- **完整 token scope（MCP 动作级 scope 矩阵）：REJECT（本期）**

理由：现网已有「登录 +（非 admin）租约」；缺的是 power 危险动作的**产品级闸**与 **hold 上限**，不是再造一套 OAuth-style scope。完整 scope 牵动 MCP middleware、凭据模型、邮件发 token 流程，ROI 低且易误伤。

### 3.2 收益 / 风险 / 工作量

- **收益**：防脚本/agent/`hold_ms=3600000` 锁死共享继电器；降低误点「关」导致 DUT 掉电；上线 201 前安全基线。
- **风险**：confirm 过烦 → 用户关确认或抱怨；`enabled=false` 若无 UI 入口会「绑了却控不了」难排查；只夹 Web 不夹 MCP 则形同虚设。
- **工作量**：**S**（hold clamp）+ **S–M**（confirm 或 enabled 闸）；完整 scope **L → 拒绝**。

### 3.3 ADR 式取舍

| 方案 | 内容 | 选/不选 |
|------|------|---------|
| **B1（推荐组合）** | ① `hold_ms` 服务端夹到 **[500, 30000]**（越界 400 或夹紧并回写实际值；推荐**夹紧并在响应回显**）；② `power_off` / `power_cycle` 要求 `confirm=true`（对齐 `mode_edl` 模式，可允许 admin 仍确认）；③ 执行 power 类动作时若存在 PowerResBinding 则尊重 **`binding.enabled`**（已有字段，避免新列） | **选** |
| **B2** | 新列 `Device.power_control_allow` 默认 true | **本期不选**：与 binding.enabled 重叠；多一处状态源 |
| **B3** | MCP/凭据级 action scope（如 token 声明 `serial:power`） | **REJECT**：无多租户/代理滥用证据；改动面 L；现 lease+confirm 更贴合 |
| **B4 不做** | 维持现状 | **不选**：生产放量前 hold 无顶是明确缺陷 |

**审计**：维持现 `_audit`；可选小增强（非阻塞）在 detail 增加 `"source": "web"|"mcp"` —— Web 已有 `web:` 前缀，**可不做**。

### 3.4 落地清单 — PT4b（若通过）

| 项 | 内容 |
|----|------|
| **任务 id** | PT4b（可拆 PT4b0=clamp-only） |
| **改动文件** | `app/domain/serial.py`（`run_action` hold 夹紧；`check_access` 对 power_off/cycle 的 confirm；可选 `_find_action` 后查 PowerResBinding.enabled）；`app/api/router.py` / `app/mcp/server.py`（确认 MCP 传 confirm）；`web/src/App.vue`（关/重启带 `confirm: true`；hold 上限与服务端对齐 30000）；`tests/test_powerres.py` 或新建 `tests/test_serial_power_guard.py` |
| **接口契约** | `POST …/serial/action`：`hold_ms` 有效范围 **500–30000**（缺省仍 `POWER_CYCLE_HOLD_MS=1200`）；`power_off`/`power_cycle` 无 `confirm=true` → `CONFIRM_REQUIRED`；`binding.enabled=false` → `FORBIDDEN`/`POWER_DISABLED`（错误码拍板时定一）。MCP `res_serial_action` 同语义。 |
| **验收标准** | curl/MCP 传 `hold_ms=999999` 被夹或 400，且实测 sleep 不超过 30s；无 confirm 的 power_off 被拒；Web 按钮带 confirm 仍可关/重启；`enabled=0` 的 binding 不可执行 power（若做该闸）；admin 与持租 user 行为符合文档。 |
| **单测点** | hold 夹紧边界（0/499/500/30000/30001）；power_off 缺 confirm；power_on 是否要求 confirm（**建议 power_on 不要求**，便于断电残窗补偿）；binding.enabled=false 拒绝；回归 composed `power_cycle` phase。 |
| **非目标** | 新 token scope 表、改 Credential schema、只读角色体系、per-owner 绑定 ACL。 |

### 3.5 REJECT 项的替代路径

完整「动作级 token scope」→ 替代为：

1. 继续用 **租约** 约束非 admin；
2. **confirm** 约束危险 power；
3. **binding.enabled**（或日后设备页一键「禁止远程断电」写该字段）做运维闸；
4. 审计靠现有 actor；若合规要 source 字段再开 **PT5-audit-source**（S）。

---

## 4. 总览：建议实施切片

| 顺序 | 切片 | 判定摘要 | 档位 |
|------|------|----------|------|
| 1 | **PT4b0 / PT4b** hold 夹紧 + power confirm（+可选 binding.enabled） | APPROVE_WITH_CHANGES | S–M |
| 2 | **PT4a** 详情页内选通道 bind | APPROVE_WITH_CHANGES | M |
| — | MCP action token scope | REJECT | — |
| — | `inventory?unbound=1` | REJECT（本期） | — |

### 待确认（落地前拍板）

| # | 问题 | 怎么确认 |
|---|------|----------|
| T1 | 详情页无 PowerRes 绑定时，COMRes 绑定是否足够稳定作为默认 `node_id` | 查测试库 / 201 库：`wl_port_binding` 与 `power_res_inventory` 同 node 覆盖率；或问现场拓扑 |
| T2 | hold 上限用 **30000**（候选 B）还是与前端现状 **60000**（PT3）对齐 | 产品拍板；建议服务端 30000、前端改齐 |
| T3 | `power_off` 是否对 **admin 也强制 confirm** | 建议是（防误触）；与 `mode_edl` 一致 |
| T4 | 抢绑「已绑他机」默认禁止还是确认后允许（现 bind API **允许换绑**） | 读 `powerres.bind`：换 device 会 disable 旧口；UI 必须强确认 |

---

## 5. 本轮约束与非范围

- 本文件为评审产出；**未改**任何业务代码；未 build / 未重启 / 未 commit。
- 不评估 PowerRes 协议/固件、暗色主题、Vue Router 拆分。

---

*文档状态：价值评审稿｜对应任务 PT4-review｜无业务代码变更。*
