# 测试资源分配管理系统 MCP 接入设计与工具契约（V1）

| 项 | 内容 |
|---|---|
| 版本 | V1 讨论稿 |
| 日期 | 2026-11-07 |
| 定位 | 对外的唯一接入形态：MCP Server。使用方是 AI 编程助手（Cursor / CodeBuddy / Claude 等），用于测试验证时申请设备 |
| 前置说明 | 与 Agent-Bridge 无关，本系统不依赖 Agent-Bridge，也不向它派发任务 |

---

## 1. 一句话架构

```
Cursor / CodeBuddy / Claude …（各自配置 MCP）
        │  MCP: streamable HTTP（或 SSE）
        ▼
┌─────────────────────────────────────────┐
│  MCP Server（团队共享，单实例）            │
│  tools: list / apply / renew / release … │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ 资源中心：目录 · 租约 · 队列 · 审计        │
└──────────────┬──────────────────────────┘
               ▼
   设备池（全公司产品设备）· SerialBridge · adb 网关
```

---

## 2. 三条硬约束（先定死，否则后面全是返工）

### 约束一：MCP Server 必须是"团队共享的单实例"

**不能**每个开发者在自己电脑上跑一份本地 stdio MCP Server。原因：排队和冲突检测依赖**全局唯一状态**。每人一套，就变成各自记一份台账，A 占了设备 B 不知道，冲突检测失效，排队更无从谈起。

- 部署：一台内网服务器，`streamable HTTP` 传输，所有人指向同一 URL。
- 客户端配置只能是一个 URL + 一个 token，不允许本地起进程。
- 唯一的例外是断网演示场景，不作为正式形态。

### 约束二：身份必须落到"人"，不能是共享 token

MCP 的调用方是"某个开发者的某个 AI 助手"。如果全公司共用一个 token，看板上就永远是"某个 agent 占着设备"，出问题查不到人。

- 每人一个 token，token 绑定 `user_id`（姓名/工号）+ 可用资源组 + 并发配额。
- 客户端配置里带 token（header 或 env），MCP Server 据此识别 holder。
- 一个开发者同时开 Cursor 和 CodeBuddy，视为同一个 holder，共用配额。

### 约束三：MCP 没有可靠的服务端主动推送

MCP 的 tool 是请求-响应，客户端不会因为服务端"有资源了"而被唤醒。所以"释放后及时拿到资源"必须靠**两种机制组合**，不能指望推送：

1. **短阻塞等待**：`apply` 带 `wait_seconds`，服务端挂起到授予或超时（建议 ≤60s，因为多数客户端单次 tool 调用有 30–60s 超时限制）。
2. **票据轮询**：拿不到就返回 `ticket_id` + 排队位次，客户端之后再调 `res_ticket` 查询。
3. **人工通道兜底**：排队超过阈值（如 15 分钟）时，通过企业微信/飞书消息通知到人。因为"等 40 分钟"这件事让 AI 干等没有意义，应该让人知道并决定是否换设备。

---

## 3. MCP 工具清单（V1 共 9 个）

### 3.1 只读类

| 工具 | 用途 | 关键参数 | 返回 |
|---|---|---|---|
| `res_list_products` | 看有哪些产品线 / 平台，各有多少台、多少空闲 | `keyword?`、`only_available?` | 树形目录：产品线 → 平台 → 设备数/空闲数 |
| `res_list_devices` | 列出具体设备及状态 | `platform?`、`product?`、`state?`、`tag?`、`limit?` | 设备列表（含 `device_id`、`display_name`、`state`、`occupant`、`occupied_since`） |
| `res_my_leases` | 我当前持有的所有租约 | 无 | 租约列表 + 剩余时间 + `adb_address` + 串口 |

### 3.2 占用类

| 工具 | 用途 | 关键参数 | 返回 |
|---|---|---|---|
| `res_apply` | 申请设备（按平台/产品/指定设备/标签） | `target`（平台名或 `device_id`）、`purpose`、`expected_minutes`、`wait_seconds`、`ports`（要哪些串口）、`priority?` | `GRANTED`：完整租约（含连接参数）；`QUEUED`：`ticket_id` + 位次 + 预计等待 |
| `res_renew` | 续约（延长到期时间） | `lease_id`、`extra_minutes` | 新 `expires_at` |
| `res_release` | 释放 | `lease_id`、`reason?` | `ok`（幂等，重复释放也返回 ok） |

### 3.3 排队类

| 工具 | 用途 | 关键参数 | 返回 |
|---|---|---|---|
| `res_ticket` | 查询排队票据状态 | `ticket_id` | `QUEUED(pos, eta)` / `OFFERED`（有 60s 确认窗口）/ `GRANTED(lease)` / `CANCELLED(reason)` |
| `res_ticket_cancel` | 放弃排队 | `ticket_id` | `ok` |

### 3.4 管理类（仅管理员 token 可见）

| 工具 | 用途 |
|---|---|
| `res_admin` | 注册/下线设备、强制回收、看全局占用（子命令式：`action=register/offline/force_release/overview`） |

> 管理类工具在普通 token 下**不注册**，MCP 客户端根本看不到这些 tool。这比"能调到但被拒绝"更安全，也避免 AI 误操作。

---

## 4. 核心工具契约细节

### 4.1 `res_apply`

```jsonc
// 入参
{
  "target": "QM102V",              // 平台名 | device_id | product/平台
  "purpose": "验证 QM102V 摄像头 bring-up",
  "expected_minutes": 40,           // 声明预期占用，用于算 TTL
  "wait_seconds": 60,               // 0=不等待，直接返回排队号
  "ports": ["uart0"],               // 可选，默认连带锁定该设备全部串口
  "tag_filter": {"system": "12"}    // 可选，按标签缩小范围
}

// 授予返回
{
  "state": "GRANTED",
  "lease_id": "L-20261107-0007",
  "device_id": "aabbccddeeff",
  "display_name": "QM102V-板3",
  "adb_address": "172.16.10.31:5555",
  "ports": [
    {"name": "uart0", "physical_ref": "COM11", "bridge_device_id": "sb-dev-11", "baud": 115200}
  ],
  "expires_at": "2026-11-07T15:40:00+08:00",
  "ttl_minutes": 60,
  "renew_hint": "长任务请每 20 分钟调一次 res_renew"
}

// 排队返回
{
  "state": "QUEUED",
  "ticket_id": "T-20261107-0031",
  "position": 3,
  "eta_minutes": 25,
  "poll_hint": "调 res_ticket(ticket_id) 查询，或调 res_apply 时带 wait_seconds 继续等"
}
```

**必须做到的三件事**：

1. `adb_address` 与 `ports` 直接可用，Agent 不需要再查一次。
2. `apply` 幂等：同一 `purpose` + 同一 holder 在短时间内重复调用，返回同一租约或同一票据（防 AI 重试刷出多条）。
3. `wait_seconds` 到点仍未授予，**必须正常返回 `QUEUED`**，不能超时异常，否则客户端会重试并重复排队。

### 4.2 租约时长与"长任务"（MCP 模式下最大的现实问题）

MCP 是短调用。Agent 拿到设备后去刷机、跑 30 分钟验证，这段时间它**不会**频繁调 MCP 工具。所以"靠心跳维持租约"这条规则在 MCP 场景下不成立，会误杀正在跑的长任务。

修正后的规则：

```
apply(expected_minutes=40)
  → TTL = min(expected × 1.5, 单次上限 240min) = 60min

TTL 到期前 10min：发提醒（tool 返回里带提醒 + Web 看板标黄 + 可选企微消息）
TTL 到期      ：租约转 EXPIRING，进入宽限期（默认 10min），不回收，继续可用
宽限期内 renew → 立即恢复 ACTIVE，重新计时
宽限期结束    → 强制回收，唤醒队列，通过企微/飞书通知原 holder（"你的 QM102V-板3 已超时回收"）
```

这样：
- 长任务不会被误杀（有 10 分钟宽限 + 明确提醒）
- 僵尸占用最终一定回到池子（宽限结束强制回收）
- 排队者有明确预期（看板上显示"预计 X 分钟后可能释放"）

### 4.3 排队与唤醒在 MCP 下的完整时序

```
Agent A: res_apply(target=QM102V, wait_seconds=60)
  → 无空闲 → QUEUED(ticket T1, pos 2)

（用户看板：队列里能看到 T1，占用人能看到自己还剩多久）

现在占用者 res_release(L-0005)
  → 资源空闲 → 队首 T0 收到 OFFER，进入 RESERVED（60s 窗口）
  → T0 的客户端调 res_ticket(T0) 得到 OFFERED + 携带完整租约
  → T0 调 res_renew 或 res_confirm 视为接受（V1 建议：拿到 OFFERED 后调用 res_ticket 时自动 confirm，减少一步）
  → 60s 内无响应 → 顺延 T1，T1 变 pos 1 → 再 OFFER
```

> 注意：MCP 没有"客户端被唤醒"的能力，所以队首必须在自己的轮询/等待里发现 OFFERED。这就是为什么 `apply` 的 `wait_seconds` 和 `res_ticket` 轮询必须都实现 —— 二者是排队体验的全部。

---

## 5. 设备注册：从"手工登记"转成"自动发现 + 人工认领"

"公司所有产品的设备"这个量级，手工一台台录不现实。设计三条发现通道：

| 通道 | 采集什么 | 说明 |
|---|---|---|
| adb 扫描 | `adb devices` → `adb shell` 读网卡 MAC、型号、系统版本 | 覆盖 USB 直连与已 `adb connect` 的设备 |
| 网络扫描 | ARP/DHCP 租约表、`adb connect ip:5555` 探测 | 覆盖已联网的板子，拿到 IP-MAC 对应 |
| SerialBridge 清单 | 调 `sb_list_devices` 拿串口设备枚举 | 你已有这套服务，直接同步，不必重造串口台账 |

流程：

```
定时扫描（如 5 分钟）
  → 发现新 MAC → 建 pending 记录（state=pending_identity，不进资源池）
  → 管理员在 Web 上"认领"：命名、选产品线/平台、填位置、打标签
  → 转 available，进池
  → 已登记但扫描不到 → 标 offline（不删除，历史保留）
```

**reg 的幂等靠 MAC**：同一 MAC 反复被扫到，只更新 `current_ip` / `last_seen_at`，不新建。

---

## 6. 资源目录（支撑"所有产品设备"）

三级结构：

```
产品线（Product Line）  e.g. 车载 / 座舱 / 边缘计算
  └ 平台（Platform）    e.g. QM102V / QM10XD / QM108H / BES2800BP
      └ 设备（Device）  e.g. QM102V-板3 / QM102V-板7
          └ 串口（Port）e.g. uart0 / uart1
```

**申请粒度**：`target` 可以填产品线、平台、或 `device_id`。

- 填平台（`QM102V`）→ 从该平台所有空闲设备里挑一台，单台坏了不影响申请。
- 填 `device_id` → 精确排该设备（"我就要刷好镜像的板3"）。

排队单位跟着 `target` 走：平台级队列、设备级队列分开，互不阻塞。

---

## 7. 客户端配置示例

```jsonc
// Cursor / CodeBuddy 的 MCP 配置
{
  "mcpServers": {
    "test-resource": {
      "url": "http://172.16.10.20:8765/mcp",
      "headers": { "Authorization": "Bearer tr_xxx_<个人token>" }
    }
  }
}
```

配置完，AI 助手侧就能看到 `res_list_devices`、`res_apply` 这些工具，用户一句话"申请一台 QM102V 的板子做摄像头验证"，Agent 自己调工具。

---

## 8. 权限与安全

| 项 | 规则 |
|---|---|
| token | 每人一个，绑定 user + 资源组白名单 + 并发上限（默认 2 台） |
| 管理工具 | 普通 token 不注册 `res_admin`，不可见 |
| 设备访问 | 本系统只发连接参数，实际 adb 与串口访问走原有通道（adb 网关 / SerialBridge） |
| 串口二次校验 | 理想状态：SerialBridge 按 token 限定可访问 `device_id`，否则拿不到地址的人也能直连绕过 |
| 审计 | 每次 apply/renew/release/强制回收 都记 holder、时间、purpose、原因 |

---

## 9. 与前面文档需要同步修正的地方

| 原文档位置 | 原规则 | 修正为 |
|---|---|---|
| 02 文档 R-B3 | 心跳续约延长到期 | 改为 `res_renew` 主动续约；MCP 场景不做高频心跳 |
| 02 文档 R-B4/R-B5 | 心跳丢 1/3 次判 SUSPECT/EXPIRED | 改为 TTL 到期转 `EXPIRING` + 10min 宽限 + 宽限结束强制回收 |
| 02 文档 H 域 | 以 HTTP API 为主 | 改为 **MCP Server 为唯一对外形态**，HTTP API 只在服务内部与 Web 使用 |
| 02 文档 V2-11 | 与 Agent-Bridge 对接 | 删除。改为"实现 MCP Server（9 个工具）" |
| 02 文档第三部分 | 部署形态待定 | 新增：MCP Server 共享单实例的部署位置与传输方式 |

---

## 10. 这一版新增的待确认项

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| M1 | MCP 传输方式 | streamable HTTP / SSE / stdio | streamable HTTP（主流客户端已支持，且能团队共享） |
| M2 | 排队等待靠什么 | 短阻塞 / 票据轮询 / 两者都要 | 两者都要，短阻塞 ≤60s |
| M3 | 长任务租约上限 | 2h / 4h / 不限 | 单次上限 4h，超时提示改用"预约"（二期） |
| M4 | 宽限期时长 | 5 / 10 / 20 分钟 | 10 分钟 |
| M5 | 设备自动发现是否上线 | 一期做 / 二期做 | 一期先做 adb 扫描（最小可用），SerialBridge 清单同步紧随 |
| M6 | 排队超阈值是否发企微/飞书 | 发 / 不发 | 发（>15 分钟），否则等人干等没体验 |
| M7 | 是否需要"预约未来时段" | 要 / 不要 | 二期。与即时排队是两种语义，混在一起会复杂化 |
| M8 | 设备访问是否也由本系统代理 | 代理 / 不代理 | 不代理，只发连接参数 |

---

## 11. 双入口：Web 与 MCP 是同一套状态的两个视图

```
                 ┌──────────────────────────────┐
   Web 浏览器 ────►│                              │
                 │   中心服务（唯一事实来源）      │───► 设备池 / 串口 / 巡检
   MCP 客户端 ────►│  注册 · 租约 · 队列 · 审计     │
                 └──────────────────────────────┘
                        ▲                ▲
                     WS 推送         同一次方法调用
```

三条实现纪律：

1. **同一个 service 方法**：Web 点"申请"和 MCP 调 `res_apply` 走的是服务里**同一个函数**，只是入参来源不同。不写两套。
2. **同一次查询**：可用性（available / RESERVED / pending / offline）由服务端算一次，两端口径一致。Web 上看是"空闲"的设备，MCP 一定申请得到；反之亦然。
3. **配额合并**：一个 holder 的并发上限跨入口合计。人在 Web 占 2 台，他的 Agent 从 MCP 就只能再占 N-2 台。

**为什么强调这三点**：双入口的系统，90% 的线上纠纷都来自"两端算得不一样"。这不是架构洁癖，是必须写进验收标准的东西 —— 验收时专门测一条：Web 上显示空闲的 3 台设备，MCP 立刻申请 3 台，必须全部成功；再申请第 4 台必须排队。
