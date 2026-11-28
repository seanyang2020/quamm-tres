# PT3 设计评审：PowerRes 动作面 · 设备独立页 · 绑定对话框优化

> 任务：PT3-REVIEW2｜分支 `feat/capabilities`｜**只落设计，不改代码**  
> 上游事实（只读评审已确认）：`power_cycle` 已支持 `hold_ms`；PowerRes bind 默认 steps 写 `{"hex"}`，执行层 `serial_tcp.normalize_steps` 要 `{"data","encoding"}` —— 字段冲突。  
> 本轮核对（≤4 处契约）：`POST /api/devices/{id}/serial/action`（含 lease/hold_ms）、`run_action` composed `power_cycle`（serial.py:632-668）、`_ensure_default_power_actions` steps、`GET /api/devices` query（`model_class`/`status`/`limit`）。

---

## 0. 目标与范围

用户三条需求统一规划为 PT3a / PT3b / PT3c：

| 阶段 | 需求 | 一句话 |
|------|------|--------|
| **PT3a** | PowerRes 卡片直接控制 | 通道行：开 / 关 / 定时重启（间隔），与 deskserialhub 通道口能力镜像 |
| **PT3b** | 设备独立页 | `#/device/{device_id}`：指纹 + 能力 + 健康 + 可操作 power + 串口入口 |
| **PT3c** | 绑定对话框优化 | 机型过滤 + 关键字搜索；可绑优先；显示已绑数 |

**非目标（本设计不覆盖）**：PowerRes 协议改版、继电器固件、暗色主题、新建独立 Vue Router 工程拆分。

---

## 1. PowerRes 卡片直接控制（PT3a）

### 1.1 API 选型结论：**复用** `POST /api/devices/{id}/serial/action` + SerialAction

**推荐：不新增 `POST /api/powerres/action`。**

| 维度 | 复用 serial/action | 新建 powerres/action |
|------|-------------------|----------------------|
| 权限 | 已有 `web_auth.require_login` + `_require_serial_lease` | 需重写一套，易漏 confirm/lease |
| 租约 | `_require_serial_lease` 已按 action_key 分流 | 重复或漂移 |
| 审计 | `_audit` / `_write_state` 已写流水 | 需另接 |
| 超时 | `hard_timeout_s` + step 级 timeout | 需复制 |
| 并发 holder | `serial_tcp.acquire_wait` / `SERIAL_BUSY` | 必须再接同一 holder，否则双通道互踩 |
| 前端 | 设备串口「测试」按钮已走同一口 | 多一套 payload |

PowerRes 通道一旦 bind，即物化为目标设备的 `DeviceSerialPort`（role/power）+ `SerialAction`（`power_on`/`power_off`，及可组合的 `power_cycle`）。卡片动作语义 =「对绑定目标设备的 power 口发动作」，与 deskserialhub / 设备串口页一致。

**前端调用约定（PT3a）：**

```http
POST /api/devices/{bound_device_id}/serial/action
Authorization: (web session)
Content-Type: application/json

{
  "action_key": "power_on" | "power_off" | "power_cycle",
  "hold_ms": 3000,          // 仅 power_cycle；缺省走服务端 POWER_CYCLE_HOLD_MS
  "confirm": true,          // 若租约策略要求
  "lease_id": null,
  "wait_ms": 3000,
  "dry_run": false
}
```

卡片侧从 inventory/bindings 行取 `device_id`（已绑）→ 调上述 API；**未绑定通道禁用动作按钮**，tooltip：「请先绑定目标设备」。

若未来要「未绑定也能对裸 endpoint 打电」——那才需要 powerres/action；当前产品口径是绑定后控制，故不引入。

### 1.2 「定时重启」序列与 `power_cycle` 取舍

**结论：UI「定时重启」= 调用既有 composed `power_cycle`，间隔映射为 `hold_ms`；不另造 steps 数组塞 delay。**

既有逻辑（`app/domain/serial.py` `run_action`）：

```
power_cycle 且 steps 为空：
  1) 执行 power_off.steps
  2) sleep(hold_ms)          # 默认 POWER_CYCLE_HOLD_MS；请求可覆盖
  3) 执行 power_on.steps
```

与需求「off_fb → delay → on_fb」同构：`hold_ms` = 断电保持间隔（UI 输入，建议默认 3000，范围 500–60000，步进 500）。

**取舍理由：**

- 已有 holder 覆盖整段 off→hold→on，避免中间释放被他人抢占。
- 已有 `phase: power_off | power_on` 失败回包，前端可区分告警文案。
- 避免在 JSON steps 里塞伪 `delay` step（执行层当前无该语义）。

**不推荐**：把 `power_cycle` 落库成显式三步 `[{data:off},{delay},{data:on}]` —— 与现网 composed 路径分叉，且 delay 需改 `normalize_steps`。

#### 断电残窗 / on 未确认 — 补偿与告警

| 失败相位 | 物理含义 | 服务端现状 | UI / 产品补偿 |
|----------|----------|------------|---------------|
| `phase=power_off` | 可能仍通电 | `result=fail`，不进入 hold/on | 红字「关机指令失败，设备可能仍通电」；允许重试关/重启 |
| `phase=power_on` | **已断电残窗** | `result=fail`，hold 已过 | **高优告警**「重启中途失败：设备可能处于断电；请立即点『开』补偿」；一键触发 `power_on` |
| `SERIAL_BUSY` | 未执行 | holder 信息 | 「通道被 {holder} 占用」；禁用连点、展示进行中 |
| 超时 / transport | 不确定 | error_code | 「结果不确定，请探测/读状态后再操作」；不自动连发 |

**状态色（卡片行）：** idle 灰 / running 蓝 loading / ok 绿闪一下 / fail-off 红 / fail-on **橙红+补偿按钮**。

> 说明：当前执行层对 `expect` 匹配失败才算 step 失败；继电器常无回读，「未确认」≈ 传输/超时失败，而非逻辑电平反馈。PT3a 不新增读电平 API；补偿靠显式 `power_on`。

### 1.3 `hex` / `data` steps 字段冲突 — 修正口径

| 层 | 现状 |
|----|------|
| 执行契约 `serial_tcp.normalize_steps` | **规范字段**：`{"data": "...", "encoding": "hex"|"text", "delay_ms"?, "expect"?}` |
| PowerRes bind 默认 `_ensure_default_power_actions` | 写入 `steps=[{"hex": hx}]` |
| 部分测试 / 历史样例 | 亦用 `{"hex": ...}` |

**统一口径（推荐 A）：以 `data`+`encoding` 为唯一写入规范；读路径做兼容。**

1. **写路径（bind 默认、UI 保存、迁移脚本）**：一律 `{"data": "A00101A2", "encoding": "hex"}`。
2. **读/执行路径（兼容旧数据）**：在 `normalize_steps`（或进入其前的适配）若存在 `hex` 且无 `data`，则映射为 `data=hex, encoding=hex`；双字段并存时以 `data` 为准并打 debug 日志。
3. **是否兼容旧数据**：是——库内已绑设备的 `{"hex"}` 仍可执行，避免绑定后一点「开」就炸。
4. **是否批量改库**：PT3a 可选轻量 migration「把 SerialAction.steps 里的 hex 键改写为 data」；非阻塞，推荐与写路径修正同批做。

**推荐拍板：A（data 规范 + hex 读兼容 + bind 写修正）。** 备选 B：全面改执行层只认 `hex`——会破坏 deskserialhub / skill 文档里已公开的 `data` 契约，否决。

### 1.4 UI：通道行按钮（与 hub 卡片风格一致）

ASCII 线框（CtrlRes → PowerRes 卡片，inventory 表行）：

```
+------------------------------------------------------------------+
| PowerRes          [节点 v]     [刷新]                             |
+------------------------------------------------------------------+
| 电源资源                                                          |
| +------+-----+--------+----------+-------------------------------+
| |设备  |通道 |端点    |绑定目标  | 操作                          |
| |PWR-A | ch1 | host:x | board-01 | [开] [关] [重启 v] [解绑]     |
| |      |     |        |          |        └ 间隔: [3000] ms      |
| |PWR-A | ch2 | host:x | (未绑定) | [开] [关] [重启] 灰禁         |
| +------+-----+--------+----------+-------------------------------+
+------------------------------------------------------------------+
```

- **开** → `power_on`；**关** → `power_off`；**重启** → `power_cycle` + 行内/弹出 `hold_ms`（默认 3000）。
- 进行中：三钮 loading，禁止连点；失败用 `ElMessage` + 行内 tag；`phase=power_on` 失败额外出「补偿开」。
- 按钮密度：`size="small"`，与 COMRes 行操作一致；不新增大卡片套娃。
- 设备名（绑定目标）可点击 → `#/device/{id}`（PT3b）。

### 1.5 权限 / 租约 / 并发（实现注意）

- 登录态与现网串口动作相同；无绑定时前端禁用，后端仍以 device_id 鉴权。
- `_require_serial_lease`：power 动作是否要求租约以现网策略为准（与设备页「测试」一致），卡片不另开后门。
- 同一 endpoint 多设备共享时，`acquire_wait` 仍按 port hold key 排队；UI 展示 BUSY holder。

---

## 2. 设备独立页面（PT3b）

### 2.1 路由与入口

| 入口 | 行为 |
|------|------|
| 设备列表行：探测/编辑/截图/记录/串口/移除 | **保留原动作**；另增「进入」或行点击主区 → `#/device/{device_id}`（建议：设备名/ID 点击进页；操作列仍就地弹窗，避免破坏习惯） |
| PowerRes 绑定目标名 | `router`/hash → `#/device/{id}` |
| 页内「返回列表」 | `#/` 或原设备中心 hash |

现网为 hash 单页（`App.vue`），采用 `#/device/{device_id}`，在既有 hashchange 解析中增加分支（与 CtrlRes 深链同模式）。

### 2.2 信息架构 + 区块

```
#/device/{device_id}
+----------------------------------------------------------+
| [< 返回列表]   board-01          [探测] [编辑] [移除]     |
+----------------------------------------------------------+
| A. 指纹                                                      |
|   USB SN / VID:PID / MAC / 位置 / 负责人 / Agent            |
+----------------------------------------------------------+
| B. 能力清单 (capabilities items)                             |
|   [serial.io ...] [power ch1 → endpoint · bound · ok] ...  |
+----------------------------------------------------------+
| C. Power 通道（可直接操作）                                  |
|   ch1  [开][关][重启+间隔]   状态 tag                        |
+----------------------------------------------------------+
| D. 串口入口                                                  |
|   slot0 log  [打开控制台]   slot1 power [配置/测试]          |
+----------------------------------------------------------+
| E. 最近动作与健康                                            |
|   最近 serial action / health_level / 租约 holder            |
+----------------------------------------------------------+
```

### 2.3 数据来源（尽量复用）

| 区块 | API | 说明 |
|------|-----|------|
| 指纹 / 基础 | `GET /api/devices/{id}` | display_name、usb、vid/pid、mac、position、owner、agent 等 |
| 能力清单 | 设备详情已嵌或 `capabilities` 聚合（现网 caps 入口以实装为准；优先详情内 `capabilities`/`ports`） | items 含 kind、binding、status |
| Power 绑定 | `GET /api/powerres/bindings?node_id=` **或** 从 caps/powerres 视图滤 `device_id` | 避免全量 overview 时前端滤 |
| 动作 | 同 PT3a：`POST .../serial/action` | 页面内嵌与卡片同组件更好 |
| 串口 | 现有 `#` 跳转 / `serialhub` URL 构造（COMRes 已有） | 复用 `openSerialConsole` 一类函数 |
| 健康/最近 | device_info 字段 + 若有 audit 摘要则附带；无则先展示 health_level / last_seen | PT3b 不强制新审计 API |

**组件划分（建议，仍可落在 `App.vue` 大文件内用区块 ref，避免大拆）：**

- `DeviceDetailPage`（区块容器）
- `DeviceFingerprintBlock`
- `DeviceCapabilitiesList`
- `PowerChannelActions`（**与 PowerRes 卡片共用**同一动作逻辑）
- `DeviceSerialEntries`
- `DeviceHealthStrip`

### 2.4 列表行操作策略（拍板项见 §6）

- **推荐**：操作列保持就地；**设备名/ID** 进独立页（低打扰）。
- 备选：六个按钮全部改为「先跳详情再操作」（改变现网心智，工作量大）。

---

## 3. 绑定对话框优化（PT3c）

### 3.1 现状

- 对话框 `prBindDlg`；目标设备下拉源 `devices` 全量。
- `GET /api/devices` query **仅**：`model_class`、`status`、`limit`（默认 Web 500）。
- **无** keyword / owner / position / adb /「可绑定」服务端参数。

### 3.2 UI 行为

```
绑定到设备
  机型过滤  [全部机型 v]     关键字 [________]  (ID/名称/位置/负责人/ADB)
  目标设备  [v 下拉]
            board-01  BM88  工位A  owner@x  串口槽 1/4  已绑电源 0
            board-02  ...                     串口槽 0/4  已绑电源 1  ← 可绑靠前
  说明：仅列出非 retired；无空闲 serial slot 的置底或标记「无空闲槽」
```

- **过滤 + 搜索**：PT3c **默认前端完成**（devices 已加载；机型用 `model_class`；关键字对 id/name/position/owner/adb_address 做 includes）。
- **可绑定排序**：有空闲 slot（或尚未绑 power 口）的排前；已绑电源通道数展示（来自 powerres bindings 计数或 caps）。
- **后端是否新增筛选参数**：**PT3c 不必须**。当设备池 ≫ 500 或要服务端「仅可绑」时，再加可选 query（见下）——列为 PT3c+。

**可选后续 API（非本阶段阻塞）：**

```
GET /api/devices?model_class=&status=&q=&bindable_power=1&limit=
```

`q`：id/name/position/owner/adb 模糊；`bindable_power=1`：有空闲 slot 且未冲突。现阶段用前端即可验收。

### 3.3 已绑数

- 打开对话框时用当前 node 的 `bindings` 按 `device_id` 聚合 `bound_power_count`。
- 下拉 label：`{id} · {model} · 电源已绑 {n} · 槽 {used}/{max}`。

---

## 4. 视觉与一致性约束

| 项 | 约束 |
|----|------|
| 组件库 | Element Plus（`el-button`/`el-tag`/`el-select`/`el-input-number`/`el-alert`） |
| 布局 | 卡片化与 COMRes/PowerRes 现网一致；详情页分区用 `el-card` 或带标题的 section，避免仪表盘堆砌 |
| 状态色 | success / warning / danger / info 对齐 Element；断电残窗用 **warning+danger 文案** 强调 |
| 按钮密度 | `size="small"` 行内操作；主 CTA 不超过 1 个 per 区 |
| 空态 | 未绑定 / 无能力 / 无串口 → `el-empty` 短文案 + 一个引导操作 |
| 错误态 | `ElMessage` + 行内 tag；不静默失败 |
| 暗色 | **不要求** |

---

## 5. 分阶段实施

### PT3a — PowerRes 动作面 + 定时重启

| 项 | 内容 |
|----|------|
| 改动文件（预估） | `web/src/App.vue`（通道行按钮+hold_ms+反馈）；`app/domain/powerres.py`（默认 steps 改 `data`）；可选 `app/serial_tcp.py`（`hex`→`data` 兼容）；可选 migration/小脚本；`tests/test_powerres.py` |
| 验收标准 | 已绑通道可开/关/重启；`hold_ms` 生效；未绑禁用；BUSY/失败相位文案正确；`phase=power_on` 失败可补偿开；新 bind 的 steps 为 `data` 且旧 `hex` 仍可跑 |
| 工作量 | **M** |

### PT3b — 设备独立页

| 项 | 内容 |
|----|------|
| 改动文件（预估） | `web/src/App.vue`（hash 路由 + 详情区块；复用 PowerChannelActions）；少量样式；一般**无需**新后端（复用 devices/:id、bindings、serial/action） |
| 验收标准 | `#/device/{id}` 深链可进可返；指纹/能力/power/串口/健康区块齐全；PowerRes 设备名可跳入；列表操作列不回归 |
| 工作量 | **M**（若强拆组件文件则 **M–L**） |

### PT3c — 绑定对话框优化

| 项 | 内容 |
|----|------|
| 改动文件（预估） | `web/src/App.vue`（prBindDlg：机型 select + 搜索 + 排序 + 已绑数）；一般**无需**后端 |
| 验收标准 | 机型过滤与关键字生效；可绑/有空槽靠前；已绑数可见；绑定成功率不下降 |
| 工作量 | **S** |

**建议顺序**：PT3a → PT3c → PT3b（先打通动作与 bind 数据质量，再做详情页承载；PT3c 可与 PT3a 尾部并行）。

---

## 6. 风险清单 + 待拍板 A/B

### 6.1 风险

| ID | 风险 | 缓解 |
|----|------|------|
| R1 | 旧 `{"hex"}` steps 导致「绑定成功但开不了」 | PT3a 读兼容 + 写修正（§1.3） |
| R2 | `power_cycle` 持锁时间 = hold_ms + 两边 IO，长间隔占 holder | UI 上限 60s；文案提示；BUSY 可理解 |
| R3 | 断电残窗（on 失败）误判为「已恢复」 | 强制补偿入口 + 醒目告警 |
| R4 | 详情页加深 `App.vue` 体量 | 先区块后拆文件；不阻塞 PT3b 验收 |
| R5 | 前端过滤在设备 >500 时不全 | 文档写明 limit；超限再上 `q=` API |
| R6 | 租约策略导致卡片动作 403/拒绝 | 与串口测试同一 `_require_serial_lease`；文案引导占租/confirm |

### 6.2 待拍板（带推荐）

| # | 议题 | A | B | **推荐** |
|---|------|---|---|----------|
| 1 | 动作 API | 复用 `/serial/action` | 新建 `/powerres/action` | **A** |
| 2 | 定时重启实现 | composed `power_cycle` + `hold_ms` | 自建 steps/delay | **A** |
| 3 | steps 字段 | `data` 规范 + `hex` 读兼容 | 执行层改认 `hex` | **A** |
| 4 | 列表进详情 | 名/ID 点击进页，操作列就地 | 操作全面迁详情 | **A** |
| 5 | 绑定筛选 | 纯前端过滤排序 | 先做后端 `q`/`bindable_power` | **A**（池大再 B） |
| 6 | 实施顺序 | a→c→b | a→b→c | **a→c→b** |

---

## 7. 附录：关键契约摘录（本轮核对）

```
GET  /api/devices?model_class=&status=&limit=500
GET  /api/devices/{device_id}
POST /api/devices/{device_id}/serial/action
     body: action_key, hold_ms?, confirm?, lease_id?, wait_ms?, dry_run?
GET  /api/powerres/overview | /inventory?node_id= | /bindings?node_id=
POST /api/powerres/bind | /unbind
```

- `run_action`：`power_cycle` 空 steps → off → `sleep(hold_ms)` → on；失败带 `phase`。
- `_ensure_default_power_actions`：当前 `steps=[{"hex": hx}]` ← **待修**。
- `normalize_steps`：要求非空 `data`。

---

*文档状态：设计评审稿｜对应任务 PT3-REVIEW2｜无代码变更。*
