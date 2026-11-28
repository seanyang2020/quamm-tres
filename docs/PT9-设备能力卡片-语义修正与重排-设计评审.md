# PT9 设计评审：设备能力卡片 · 语义修正与重排

> 任务：PT9｜分支 `feat/capabilities`｜VERSION `0.54.2`｜**只落设计，不改业务代码**  
> 现场设备页：`#/device/dev_997b70adae`（ADB 卡 + 串口 `bull1 CH1` NO_BINDING / `ttyUSB0` available「只读」+ 电源 `bull1 CH1` available）  
> 上游事实：能力快照 Phase 1（`capabilities.py` 模块头注释「只做 discovery，不扩大执行面」）；PowerRes 物化为 `DeviceSerialPort(role=power)`。

---

## 0. 目标与范围

| 编号 | 问题 | 一句话目标 |
|------|------|------------|
| **P1** | 电源通道重复出现在串口区 | `role=power/mode` 只产出 power/mode item，不再冒充 `serial.io` |
| **P2** | `ttyUSB0` 误标「只读」 | `constraints.read_only` 只反映真实写约束；`source=observed` 单独展示 |
| **P3** | `adb_shell` 点了无效 | 能力声明与执行路径对齐：白名单 ADB 动作 + 回显；或降级为不可点 |
| **P4** | 布局 | ADB/串口/电源卡同一排横向等宽，操作项在卡内 |

**非目标（本设计不覆盖）**：PowerRes 协议改版、独立 Vue Router 拆分、任意自由 `adb shell`、改 MCP 租约语义、数据库 schema。

---

## 1. P1 — 电源通道不应出现在串口能力

### 1.1 现状定位（证据）

`_items_for_device` 把 ADB 与 `_serial_items(...)`（含 powerres）一起收集：

```332:347:app/domain/capabilities.py
def _items_for_device(ctx: dict, device: Device) -> list[dict]:
    items: list[dict] = []
    items.extend(_collect_adb(device))
    items.extend(
        _serial_items(
            device,
            bindings=ctx["bindings"].get(device.device_id, []),
            ports=ctx["ports"].get(device.device_id, []),
            ...
            powerres=ctx.get("powerres", {}).get(device.device_id, []),
        )
    )
    return items
```

`_serial_items` **对每个 declared port 无条件先建 `serial.io`**，再按 role 追加 power/mode：

```423:473:app/domain/capabilities.py
    for port in ports:
        binding = _match_binding_to_declared(...)
        ...
        pr_binding = _match_powerres_to_declared(port, powerres_rows)
        items.append(
            _build_serial_io_item(          # ← 对 role=power 也会进这里
                device, port=port, binding=binding, ..., source="declared",
            )
        )
        if port.role == "power" and port.enabled:
            items.append(_build_power_or_mode_item(..., kind=KIND_POWER, ...))
        elif port.role == "mode" and port.enabled:
            items.append(_build_power_or_mode_item(..., kind=KIND_MODE, ...))
```

PowerRes 注释明确：bind 后物化为 `DeviceSerialPort(role=power)`，**供 capabilities 产出 power item**：

```1:4:app/domain/powerres.py
"""PowerRes 电源资源域：独立于 WireLogPortBinding / COMRes。
...
DeviceSerialPort(role=power)，供 capabilities 产出 power item。
"""
```

`_build_serial_io_item` 的 label 取 `port.label`（PowerRes 常写 `bull1 CH1`）：

```503:506:app/domain/capabilities.py
    if port is not None:
        cap_id = f"serial.io:slot{port.slot}"
        label = port.label or f"slot {port.slot}"
```

当 PowerRes 口**没有** WireLogPortBinding（电源走 PowerResBinding，不走 COMRes）时，`binding is None` → `REASON_NO_BINDING`：

```522:525:app/domain/capabilities.py
    if binding is None:
        status = STATUS_UNAVAILABLE
        if REASON_DISABLED not in reasons:
            reasons.append(REASON_NO_BINDING)
```

与现场一致：串口区 `bull1 CH1` = NO_BINDING；电源区同名 = available（靠 `powerres_binding`）。

既有单测也固化了「power port 同时出 serial.io + power」：

```199:210:tests/test_capabilities.py
def test_declared_port_no_binding(session):
    ...
    io = next(i for i in snap["items"] if i["kind"] == "serial.io")
    assert "NO_BINDING" in io["status_reasons"]
    power = next(i for i in snap["items"] if i["kind"] == "power")
    assert "NO_BINDING" in power["status_reasons"]
```

### 1.2 正确过滤口径

| role | 产出 kind | 是否进 `_build_serial_io_item` |
|------|-----------|--------------------------------|
| `power` | `power` | **否** |
| `mode` | `mode` | **否** |
| `log` / `spare` / 缺省 / 其它 IO 口 | `serial.io` | **是**（`log`/`spare` 可读约束另议，见 P2） |
| 仅 WireLog 绑定、无 declared port | `serial.io`（`source=observed`） | **是**（binding-only 分支不变） |

建议常量（后端）：

```python
NON_SERIAL_IO_ROLES = frozenset({"power", "mode"})
# 循环内：if (port.role or "").strip().lower() in NON_SERIAL_IO_ROLES: 跳过 serial.io，只走 power/mode 分支
```

`enrich_declared_port` 已按 role 映射 `power:slotN` / `mode:slotN` / `serial.io:slotN`（约 L167–173），与此口径一致，无需改语义。

### 1.3 方案对比

| | **A. 后端跳过（推荐）** | **B. 仅前端过滤** |
|--|------------------------|-------------------|
| 做法 | `_serial_items` 对 `power`/`mode` 不 append serial.io | `deviceCapsByKind` / 列表电源 popover 过滤掉 `role` 暗示的重复卡 |
| 正确性 | 快照/MCP/`capabilities_summary.serial` 一并正确 | 后端 summary 仍把假 serial.io 计入 `serial` 聚合 |
| 行数 | ~15–25 行 + 改 1 条既有单测 | ~10 行前端，根因仍在 |
| 风险 | `revision` 哈希变化；依赖「每台 power 必有 serial.io」的测试要改 | Agent 仍看到幽灵 serial.io |

**推荐 A。** 理由：现场是能力模型错误，不是展示层错误；`summary.serial` 用 `KIND_SERIAL_IO` 聚合（L1017–1042），不修后端则设备中心「串口」摘要也会被电源口污染。

---

## 2. P2 — `ttyUSB0`「只读」语义与控制台入口

### 2.1 现状定位（证据）

后端把 **`source == "observed"` 直接当成 read_only**：

```513:515:app/domain/capabilities.py
    reasons: list[str] = []
    status = STATUS_AVAILABLE
    read_only = source == "observed" or (port is not None and port.role in ("log", "spare"))
```

`source` 本身已是独立顶层字段（L589 `"source": source`）。observed 分支来自「binding 未匹配任何 declared port」（L475–488），表示 **COMRes 端口目录观测到的绑定**，不是「只能读」。

前端只要 `constraints.read_only` 就显示「只读」：

```2953:2958:web/src/App.vue
function capConstraintText(item) {
  const c = (item && item.constraints) || {}
  const parts = []
  if (c.read_only) parts.push('只读')
  if (c.shared_endpoint || c.shared) parts.push('共享')
  return parts.join(' / ') || '—'
}
```

既有单测把 observed → read_only 写成契约：

```157:161:tests/test_capabilities.py
    io = [i for i in snap["items"] if i["kind"] == "serial.io"]
    ...
    assert io[0]["source"] == "observed"
    assert io[0]["constraints"]["read_only"] is True
```

右上角「串口控制台」实际打开的是 **绑定列表弹窗**（再点行内链接进 shell），不是灰态语义：

```4821:4822:web/src/App.vue
            <el-button size="small" :disabled="!devicePage.serial_openable"
              @click="openDeviceWlBindings(devicePage)">串口控制台</el-button>
```

```3800:3807:web/src/App.vue
async function openDeviceWlBindings(row) {
  wlBindDlg.visible = true
  ...
  wlBindDlg.rows = await api('/api/wirelog/device/' + row.device_id + '/bindings')
}
```

真·控制台入口已有：`openWlShell(row)`（`/shell.html?node_id&slot...`，约 L3707–3725）与 `openPortConsole`（`/api/comres/console-url`，约 L1632–1652）。能力卡上的 `binding` 已含 `node_id` / `slot` / `com` / `endpoint`（`_binding_view` L909–918），**可直接复用，无需新权限模型**（与现网「登录即可开 shell 页」一致；租约闸仍由 serialhub 侧写锁处理）。

### 2.2 字段级语义设计

| 字段 | 推导规则（推荐） | UI |
|------|------------------|-----|
| `source` | 已有：`declared` \| `observed`（顶层，不变） | 约束行旁或新行：**来源：声明端口** / **来源：端口目录观测** |
| `constraints.read_only` | **仅**真实写约束：① `role in ("log",)`（tres-logs 独占读通道，可保留）；② 未来 binding/端口显式 `ro` 标志；③ 可选：写锁被他人持有时前端灰写、不必写进快照 | 文案「只读」 |
| `constraints.shared_endpoint` | 已有 shared 逻辑，不变 | 「共享」 |
| **不要** | `source=="observed"` → `read_only` | — |
| `role=="spare"` | **建议从 read_only 默认里拿掉**（spare 常是备用 IO，可写）；若产品坚持 spare 不可写，在待拍板确认 | — |

observed 仍可不下发 `serial_read` action（Phase 1「不扩大声明执行面」可保留 `actions=[]`），但 **不得**因此标「只读」——用户可经控制台对 TCP COM 读写。

可选增强（非必须）：`constraints.source_label` 或前端 map：

```js
const SOURCE_LABEL = { declared: '声明端口', observed: '端口目录观测' }
```

### 2.3 方案对比

| | **A. 纠正 read_only + 卡内控制台（推荐）** | **B. 仅改文案，不动 read_only** |
|--|------------------------------------------|--------------------------------|
| 后端 | `read_only` 去掉 `source=="observed"`；改单测 | 不改 |
| 前端 | `capConstraintText` 增加「来源：…」；串口卡加「控制台」按钮 → `openWlShell`/`openPortConsole` | 把「只读」改成「观测」映射（hack） |
| 控制台位置 | **复制**到串口卡内；头栏按钮可保留（多口时仍有总入口） | 不变 |

**推荐 A。** 理由：字段语义与用户心智一致；控制台复用成本低（binding 已有 node/slot）。

**控制台「移动 vs 复制」**：推荐 **复制**（卡内直达 + 头栏总览弹窗保留）。单口设备体验最佳；多口时头栏仍可看全部绑定。

### 2.4 卡内控制台可行性摘要

| 项 | 结论 |
|----|------|
| 组件 | 无内嵌 xterm；现有是 `window.open('/shell.html?...')` 或 COMRes console-url |
| 路由 | 设备页 hash `#/device/{id}` 不变；shell 新标签 |
| 权限 | `require_login` 即可调 bindings / console-url；与现头栏一致 |
| Token scope | 不新增动作级 scope；shell 页走节点 token 片段（既有） |
| 数据 | `item.binding.node_id` + `item.binding.slot`；缺 binding 则禁用并 tooltip |

---

## 3. P3 — `adb_shell` 不可用

### 3.1 现状定位（证据）

能力层在 available/degraded 时声明 `adb_shell` 且 `enabled: true`：

```364:373:app/domain/capabilities.py
    actions = []
    if status in (STATUS_AVAILABLE, STATUS_DEGRADED):
        actions = [{
            "action_key": "adb_shell",
            "kind": "adb",
            "enabled": True,
            "inter_step_ms": 0,
            "steps": [],
            "steps_sha256": _steps_sha([]),
        }]
```

模块头写明 Phase 1 **只做 discovery，不扩大执行面**（`capabilities.py` L1–5）。

前端非 power 动作一律打进 **串口** action API：

```3059:3070:web/src/App.vue
async function doDeviceCapAction(item, action) {
  ...
  await runDeviceSerialAction(id, key, {
    scope: (item && item.id) || key,
    okText: action.display_name || key,
  })
}
```

```2982:3004:web/src/App.vue
async function runDeviceSerialAction(deviceId, actionKey, opts) {
  ...
  const out = await api('/api/devices/' + encodeURIComponent(deviceId) + '/serial/action', {
    method: 'POST',
    ...
    body: JSON.stringify(body),  // action_key 原样传入
  })
```

后端 `POST /devices/{id}/serial/action` 只调 `serial_svc.run_action`（`router.py` L2264–2288），而 `_find_action` **只在 DeviceSerialPort + SerialAction 表查找**，且按 `power_`/`mode_` 前缀筛 role——**无任何 `kind=adb` / `adb_shell` 分支**：

```490:530:app/domain/serial.py
def _find_action(...):
    expect_role = (
        "power" if action_key.startswith("power_")
        else ("mode" if action_key.startswith("mode_") else None)
    )
    ...
    raise DomainError(
        "SERIAL_ACTION_NOT_FOUND",
        f"{device_id} 没有定义动作 {action_key}",
        ...
    )
```

结论：点击后几乎必然 `SERIAL_ACTION_NOT_FOUND`（或被误认为灰态/失败 toast）。**能力声明与执行面脱节。**

已有可复用的 ADB 子进程封装（均带 `timeout`）：

- `app/screenshot.py`：`_adb` / `_shell`（GNU timeout + subprocess）
- `app/domain/devicelock.py`：`_run(["connect", addr])` 等

连通与鉴权：设备指纹区展示 `adb_address`（如 `172.16.60.198:5555`）；真实 `adb connect` / `unauthorized` 需实现轮探测，设计阶段不强制现场打 adb（避免阻塞）。失败回显应对齐截图模块的 rc/stdout 解析。

### 3.2 方案对比

| | **A. 最小可用：白名单 ADB 动作（推荐）** | **B. 能力降级：不下发/禁用按钮** | **C. 任意 `adb shell` 透传** |
|--|------------------------------------------|----------------------------------|------------------------------|
| 后端 | 新 `POST /api/devices/{id}/adb/action` 或 serial/action 分支 `kind=adb`；白名单命令 | `_collect_adb` 的 `actions=[]` 或 `enabled=false` + reason | 接收自由 argv |
| 前端 | `doDeviceCapAction` 识别 `kind==='adb'` → 新 API；卡内下拉选预设 + 输出面板 | 按钮不渲染或灰态 tooltip「尚未接通」 | 自由输入框 |
| 安全 | 高：命令封闭 | 最高（无执行） | **低：任意 shell** |
| 工作量 | ~80–150 行后端 + ~60 行前端 + 测试 | ~20 行 | 小但不建议 |

**推荐 A（产品要「能跑」时）**；若本迭代只修展示，可先落地 **B** 作为止血，再开 A。

#### A 最小可用规格

| 项 | 建议 |
|----|------|
| 白名单 `action_key` | `adb_devices`（`adb devices -l`）、`adb_getprop`（`shell getprop ro.product.model` 等只读 prop 列表）、`adb_battery`（`shell dumpsys battery`）、`adb_id`（`shell id`）；**废弃笼统 `adb_shell` 或映射为默认 `adb_id`** |
| 执行器 | 新 `app/domain/adb_actions.py`（或挂 `screenshot._adb`），流程：`adb connect` → `-s {addr} …` → 截断输出（如 32KiB） |
| 超时 | 单命令 `timeout 15s`（对齐仓库惯例，≤60s） |
| 鉴权 | `require_login` + 设备租约或 admin（对齐 `_require_serial_lease`；只读命令可放宽为「登录即可」——待拍板） |
| 审计 | `audit` 记 `adb_action` + action_key + rc + 输出哈希/前 N 字 |
| 回显 | JSON `{result, stdout, stderr, rc, duration_ms}`；前端卡内 `<pre>` 或 MessageBox |
| 失败 | `ADB_CONNECT_FAILED` / `ADB_UNAUTHORIZED` / `ADB_TIMEOUT` / `ADB_NOT_ALLOWED` |

**风险**：即便白名单，`dumpsys` 仍可能较慢；网络 ADB `unauthorized` 需明确 UI。**禁止**把任意 shell 绑到 `adb_shell` 这个名字上。

---

## 4. P4 — 三类能力卡同一排 + 操作在卡内

### 4.1 现状定位（证据）

设备页按 **kind 竖向分块**，每块内 `cap-grid`：

```4889:4936:web/src/App.vue
              <div v-else>
                <div v-for="g in deviceCapsByKind" :key="g.kind" class="cap-kind-block">
                  <div class="cap-kind-title">{{ g.label }}</div>
                  <div class="cap-grid">
                    <div v-for="it in g.items" ... class="cap-card">
                      ...
                      <div v-if="capKindActions(it).length" class="cap-card-actions">
                        <template v-if="it.kind === 'power'"> …开/关/重启… </template>
                        <template v-else>
                          <el-button v-for="act in capKindActions(it)" ...>
```

```7647:7672:web/src/App.vue
.cap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
...
.cap-card-actions {
  display: flex; flex-wrap: wrap; align-items: center; gap: 4px;
  margin-top: 8px;
}
```

电源操作已在卡内；ADB/串口只有 action 按钮（ADB 无效见 P3）；串口无控制台入口。

### 4.2 布局方案对比

| | **A. 扁平一网格 + kind 徽章（推荐）** | **B. 三列 el-col（ADB \| 串口 \| 电源）** |
|--|--------------------------------------|------------------------------------------|
| 结构 | 去掉 `cap-kind-block` 外层循环，单 `el-row`/`CSS grid` 铺所有 items；卡头加 `el-tag` 标 ADB/串口/电源 | `el-row :gutter="12"` → 三 `el-col :xs="24" :sm="12" :md="8"`，列内再排该 kind 的卡 |
| 等宽 | `minmax(260px, 1fr)` 或 col span 均分 | 列等宽；列内多卡纵向 |
| 换行 | 窄屏自动 1～2 列 | xs 堆叠为 1 列，sm 2 列，md 3 列 |
| 高度对齐 | `align-items: stretch`；动作区 `margin-top: auto` | 同左，列内卡各自 stretch |
| 空态 | 无该项不占位 | 列内 `el-empty` 小图「无电源」等 |

**推荐 A**（卡少时「三卡一排」最直观）；若产品强烈要「分区标题」保留，用 **B**。

#### Element Plus 落点（A）

```html
<el-row :gutter="12" class="cap-row">
  <el-col
    v-for="it in deviceCapsFlat"
    :key="it.id"
    :xs="24" :sm="12" :md="8" :lg="8"
  >
    <div class="cap-card cap-card--stretch">
      <div class="cap-card-head">
        <span class="cap-card-name">{{ it.label }}</span>
        <el-tag size="small">{{ kindLabel(it.kind) }}</el-tag>
        <el-tag size="small" :type="capStatusTagType(it)">{{ capStatusLabel(it) }}</el-tag>
      </div>
      <!-- 端点 / 约束+来源 -->
      <div class="cap-card-actions">…按 kind 渲染…</div>
    </div>
  </el-col>
</el-row>
```

断点建议（待拍板）：`md≥992` 三列；`sm≥768` 两列；`xs` 一列。`minmax` 阈值 **260px**。

灰态：`status !== available|degraded` 时动作 `disabled` + `opacity: .65`；`NO_BINDING` 保持 warning tag。

### 4.3 前端函数最小改动点

| 符号 | 改动 |
|------|------|
| `deviceCapsByKind` | 保留供列表 popover；详情页新增 `deviceCapsFlat`（过滤 P1 后的 items，可选隐藏 mode） |
| `capConstraintText` | 加来源文案；read_only 仅真只读 |
| `capKindActions` | 可扩展：串口卡合成「控制台」伪动作，或模板内独立按钮 |
| `doDeviceCapAction` | 分支 `kind==='adb'`；串口控制台不走 serial/action |
| 模板 L4889–4938 | 换 row/col 或扁平 grid；串口加控制台；ADB 加回显区 |
| CSS `.cap-card--stretch` | `height:100%; display:flex; flex-direction:column`；`.cap-card-actions { margin-top:auto }` |

---

## 5. 改动清单（实现阶段，本评审不落地）

| 文件 | 改动点 | 预估行数 |
|------|--------|----------|
| `app/domain/capabilities.py` | `_serial_items` 跳过 `power`/`mode` 的 serial.io；`_build_serial_io_item` 修正 `read_only`；可选重命名/拆分 `adb_shell` 白名单 action 声明 | ~40–60 |
| `app/domain/adb_actions.py`（新）或扩展 `screenshot.py` | 白名单执行 + connect + 截断输出 | ~80–120 |
| `app/api/router.py` | `POST /devices/{id}/adb/action`（或 serial/action 分流）+ lease | ~40–60 |
| `app/mcp/server.py` | 可选只读工具暴露白名单结果；**不改** `capabilities_summary` 键名 | ~20 |
| `tests/test_capabilities.py` | `test_declared_port_no_binding` 断言无 serial.io；`test_binding_only_serial` `read_only is False`；新增 power 不污染 serial summary | ~40 |
| `tests/test_*_adb*.py`（新） | 白名单/拒绝任意命令/超时 | ~80 |
| `web/src/App.vue` | 布局扁平化；`capConstraintText`；卡内控制台；ADB 动作分支与回显 | ~120–180 |
| `docs/` / CHANGELOG / VERSION | 实现轮再动 | — |

---

## 6. 风险与兼容

| 面 | 影响 | 策略 |
|----|------|------|
| 能力快照 JSON | `items` 中少掉假 `serial.io`；`constraints.read_only` 对 observed 从 `true→false`；`source` 字段已存在 | **不需要双写**。属 bugfix；`revision` 会变属预期 |
| `capabilities_summary.serial` | 不再被 power 口抬成 available/unavailable 噪音 | 列表「串口」摘要更准；前端若缓存 summary 需刷新 |
| MCP `res_device_info.capabilities` / `res_list_devices.capabilities_summary` | 同上；`res_serial_caps` 走 `serial.caps`，**不受** items 过滤影响 | 文档说明即可 |
| 前端旧字段 | 仍读 `constraints.read_only`；新增展示依赖已有 `source` | 兼容：旧前端只是少显示「来源」 |
| 单测契约 | `test_binding_only_serial` / `test_declared_port_no_binding` 必须改 | 与产品口径一并改 |
| 版本化 | 不新增 `constraints` 版本字段；CHANGELOG 记 breaking-for-bugfix | 可选在 snapshot 加 `schema_hints`——**不推荐**本迭代 |

---

## 7. 验收标准（实现后可执行）

### 7.1 接口 JSON

1. 对仅有 PowerRes `role=power`、另有 observed `ttyUSB0` 的设备：  
   - `items` 中 **不存在** `kind=="serial.io"` 且 label/transport 指向电源通道；  
   - 存在恰好一个 `kind=="power"`；  
   - 存在 `kind=="serial.io"` 且 `source=="observed"`、`constraints.read_only==false`。
2. `summary.serial` 不因 power 口单独变为 available。
3. ADB（若选方案 A）：`POST .../adb/action` `{action_key:"adb_id"}` → `result=="ok"` 且 `stdout` 非空；任意命令 → `ADB_NOT_ALLOWED`。

### 7.2 页面

1. 设备页能力区：**电源通道不再出现在串口分区**（无第二张 `bull1 CH1` NO_BINDING 串口卡）。
2. `ttyUSB0`：**不显示「只读」**；显示来源「端口目录观测」；**卡内有控制台入口**且能打开 shell。
3. ADB：白名单命令可跑并回显（或方案 B 下按钮明确不可用且无假 enabled）。
4. **三卡（或同类多卡）一排**，窄屏换行；**操作在卡内**（电源开/关/重启、串口控制台、ADB 预设）。

---

## 8. 待拍板问题

1. **`adb_shell`**：落地白名单执行（A）还是先禁用声明（B）？是否允许非 admin、无租约执行只读 ADB？
2. **控制台**：卡内 **复制**（推荐）还是从头栏 **移走**？
3. **`role=spare`**：是否继续默认 `read_only`？
4. **布局**：扁平网格（A）还是三列分区（B）？`md` 三列断点是否用 Element 默认 992？
5. **笼统 `adb_shell` action_key**：删除并改为多个预设 key，还是保留别名映射到 `adb_id`？（MCP/旧前端若写死 `adb_shell` 需兼容）
6. **实现优先级**：是否强制 P1+P2+P4 同 PR，P3 可拆 PT9b？

---

## 9. 推荐落地顺序（供派发）

1. **P1 后端过滤** + 单测（立刻消重）  
2. **P2 read_only / 来源文案** + 卡内控制台  
3. **P4 布局**  
4. **P3 ADB**（独立小 PR，含审计与白名单测试）

---

## 10. 关键结论摘要

- **P1**：根因是 `_serial_items` 对所有 declared port（含 `role=power`）无条件生成 `serial.io`；应跳过 `power`/`mode`，只保留 power/mode item。  
- **P2**：`read_only = (source == "observed")` 误判；observed 应展示为来源，只读留给 log 等真约束；控制台复用 `openWlShell` + `binding.node_id/slot`。  
- **P3**：`adb_shell` 仅 discovery 声明，无执行器；点按钮走 `serial/action` 必失败。最小可用是白名单 ADB API + 前端分流，禁止任意 shell。  
- **P4**：现为按 kind 竖排；改为扁平 `el-col`/`grid` 等宽一排，动作区留在卡底，函数级改动集中在 `App.vue` 能力模板与 3～4 个 helper。
