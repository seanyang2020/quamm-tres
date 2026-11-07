# A 域细化（一）：Agent 身份、注册与授权

| 项 | 内容 |
|---|---|
| 版本 | V1 |
| 日期 | 2026-11-07 |
| 归属 | A 域（设备申请）的前置：先有身份，才谈得上申请 |
| 配套 | `Agent授权流程.png`、`arch-agent-auth.svg` |

---

## 1. 三个概念先分清

| 概念 | 是什么 | 为什么单独存在 |
|---|---|---|
| **人（principal）** | 真实的人，或一个服务账号 | 配额、责任、审批都挂在人身上；人离职要能一次收干净 |
| **Agent（agent）** | 一个注册单元，比如"张三的 Cursor"、"李四的 CodeBuddy" | 一个人会有多个 Agent，审计要知道是哪个在申请；某个 Agent 泄露只吊销它，不影响别的 |
| **凭据（credential）** | 一串 token，绑定 {人, Agent} | 是唯一的鉴权单元；一个 Agent 可以有多条（多机使用） |

一句话：**人负责"额度与责任"，Agent 负责"是谁在操作"，凭据负责"怎么证明"。**

---

## 2. 两条注册路径（都保留，按场景选）

### 路径一：Agent 自助注册 + 人在 Web 批准（默认）

| 步 | 发生什么 | 谁做 |
|---|---|---|
| 1 | Agent 提交注册：名称、用途、所属人邮箱 | Agent |
| 2 | 系统返回配对码 `A7K-92F3` + 授权链接，10 分钟失效 | 系统 |
| 3 | 人在 Web「待授权 Agent」看到这条申请：来源 IP、UA、用途说明 | 人 |
| 4 | 人点「批准」——**此时系统才生成凭据** | 人 |
| 5 | Agent 用配对码轮询，拿到 token（只显示一次） | Agent |
| 6 | token 写进 MCP 配置，之后每次调用校验状态 | Agent |

**为什么第 4 步要卡在人的手上**：如果不卡，任何知道 URL 的人都能注册一个"张三的 Agent"，然后拿张三的配额去占设备、去排队——这不是越权拿数据，是**消耗别人的额度**，比越权更难察觉。

### 路径二：人在 Web 预创建 + 分发凭据

| 步 | 发生什么 |
|---|---|
| 1 | 人新建 Agent 槽位：命名（"Cursor-1"）、用途、资源组、有效期 |
| 2 | 系统生成一次性 token，或生成"领取码"让 Agent 自己来取 |
| 3 | 填进 MCP 配置（格式与路径一完全一致） |
| 4 | 首次调用即生效，无需再审批 |
| 5 | 到期前 7 天提醒续期 |

适用：给全组批量发、给 CI 机器人固定配置、给不方便交互的 Agent 预置。

### 不采用：用人的 SSO 直接换 token

问题有两个：
1. 审计里全是"张三"，分不清是 Cursor 还是 CodeBuddy，多人共用一台机器时更是糊成一团。
2. 出问题只能吊销"人的全部权限"，没法只掐一个 Agent。

---

## 3. 一个人多个 Agent：三条规则

| # | 规则 | 不这么做的后果 |
|---|---|---|
| 1 | **配额按人合计**：默认 2 台，N 个 Agent 共享这 2 台 | 注册三次就能绕过配额，配额形同虚设 |
| 2 | **队列按人合并**：同一人的多个 Agent 同时排队，只占一个队位，后来者返回同一个 ticket | 自己的三个 Agent 互相排队抢，还堵住别人 |
| 3 | **看板显示"人 · Agent"**：如「张三 · Cursor-1」 | 人找得到责任人；排查时知道是哪个 Agent 干的 |

补充两条约定：

- **一期不做"每个 Agent 的子配额"**（比如限制 Cursor 只能用 1 台）。先按人合计，但**按 agent_id 记录用量**，等真出现"某个 Agent 把额度吃光"的情况，再加子配额，数据是现成的。
- **排队优先级按人算**，不按 Agent 算。否则一个人注册 10 个 Agent 就能刷高优先级。

---

## 4. 凭据规则

| 项 | 取值 | 说明 |
|---|---|---|
| 格式 | `rr_<agent前8位>_<32位随机>` | 前缀便于识别来源，方便日志与人眼排查 |
| 存储 | 只存 SHA-256 哈希 + 前缀 | 库被拖走也拿不到可用 token |
| 有效期 | 90 天 | 到期前 7 天提醒；不搞无限期长期票 |
| 单个 Agent 活跃凭据上限 | 3 条 | 支持多台机器；超了要吊销旧的 |
| 吊销 | 即时生效 | 每次调用查一次状态（有缓存则带版本号，版本变了立即失效） |
| scope | 申请类默认全给（list/apply/renew/release/ticket）；**管理类不注册** | 普通 token 在客户端根本看不到管理工具 |

---

## 5. 边界情况怎么处理

| 场景 | 处理 |
|---|---|
| 同名 Agent 重复注册 | 允许，靠 `agent_id` 区分；列表按人聚合展示，避免混淆 |
| 换机器 / 重装 | 可复用原凭据（把 token 拷过去），也可新建 Agent；不建议每次都新建 |
| 人离职 | 吊销该人名下**全部**凭据 + 释放其名下所有租约 + 队列条目作废 |
| 共享机器人 / CI | 建成"服务账号"类型的 principal，配额单独配，不占用某个人的额度 |
| 一个 Agent 被多人共用 | **不允许**。Agent 归属唯一 principal，要共用就建服务账号 |
| token 泄露 | 只吊销这一条，其余凭据与人不受影响 |
| 恶意注册（冒充他人 Agent） | 靠第 4 步人工批准 + 展示来源 IP/UA 拦；同名不拦，冒名批不下去 |

---

## 6. 数据表（4 张）

```sql
principal          -- 人 / 服务账号
  id, type(person|service), name, employee_no, email, status, created_at

agent              -- 注册单元
  id, principal_id, name, purpose, source_ip, user_agent,
  status(pending|active|disabled), approved_by, approved_at, created_at

agent_credential   -- 凭据
  id, agent_id, principal_id, token_hash, token_prefix, scope,
  expires_at, revoked_at, last_used_at

agent_auth_request -- 注册申请单（路径一用）
  id, pairing_code, agent_name, purpose, owner_email,
  source_ip, user_agent, status(pending|approved|rejected|expired),
  expires_at, approved_by, approved_at
```

---

## 7. 接口

注册必须走 **HTTP，不能走 MCP** —— 因为 MCP 连接本身就要 token，首次接入时还没有。

| 方法 | 路径 | 谁调 | 说明 |
|---|---|---|---|
| POST | `/api/agent/register` | Agent | 提交注册，返回配对码与授权链接 |
| GET | `/api/agent/register/{code}` | Agent | 轮询状态；批准后返回一次性 token |
| POST | `/api/agent/register/{code}/approve` | 人（Web，需登录） | 批准 |
| POST | `/api/agent/register/{code}/reject` | 人（Web） | 拒绝 |
| GET | `/api/my/agents` | 人（Web） | 我名下所有 Agent 与凭据状态 |
| POST | `/api/my/agents` | 人（Web） | 预创建槽位（路径二） |
| POST | `/api/my/agents/{id}/credentials` | 人（Web） | 生成新凭据 / 领取码 |
| DELETE | `/api/credentials/{id}` | 人（Web）/ 管理员 | 吊销 |

Agent 侧接入可以做成一行的形式，降低摩擦：

```bash
curl -s -X POST https://res.corp.com/api/agent/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"Cursor-1","purpose":"刷机验证","owner_email":"me@corp.com"}'
# → {"pairing_code":"A7K-92F3","verification_url":"https://res.corp.com/agent/approve","expires_in":600}
```

批准后：

```bash
curl -s https://res.corp.com/api/agent/register/A7K-92F3
# → {"status":"approved","token":"rr_3f9c2a11_8c7d...","expires_at":"2027-02-05"}
```

---

## 8. 这对 A 域其他部分的影响

| 影响点 | 具体变化 |
|---|---|
| A2 申请 | 请求里必须带 `agent_id`（或从凭据解出），租约记录 `holder_principal_id` + `holder_agent_id` 两个字段 |
| A4 排队 | 队位按 principal 去重，ticket 记录发起它的 agent_id |
| A5 释放 | 释放只能由"持有它的 principal 名下任一 Agent"发起，或管理员强制回收 |
| C5 审计 | 每条流水都记「人 + Agent」，看板聚合时按人，明细里能看到 Agent |
| C1 一般 | 人离职时，靠 principal 一次性清理 |

---

## 9. 需要你确认

| # | 问题 | 我的建议 |
|---|---|---|
| 1 | 是否接受"配额按人合计、不按 Agent 分" | 建议接受，一期简单；数据留着，随时能加子配额 |
| 2 | 路径一是否作为默认（即 Agent 必须先注册等批准） | 建议是。预分配只作为批量场景的加速手段 |
| 3 | 凭据有效期 90 天够不够 | 90 天 + 提前 7 天提醒，比"永久"安全，比"30 天"少打扰 |
| 4 | 一个人默认 2 台是否合适 | 现在按 2；如果实际经常要 3–4 台，按组配置更合理 |
| 5 | 是否需要"服务账号"这一类（CI 机器人） | 建议要，否则 CI 只能挂到某个人名下，人一走就断 |
