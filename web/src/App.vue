<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'

const pages = [
  { key: 'dashboard', label: '看板' },
  { key: 'devices', label: '设备' },
  { key: 'leases', label: '占用' },
  { key: 'queue', label: '排队' },
  { key: 'anomaly', label: '异常/待修复' },
  { key: 'credentials', label: '凭据' },
  { key: 'audit', label: '审计' },
  { key: 'settings', label: '设置' },
]
const active = ref('dashboard')
const settingsTab = ref('system')

// ---- 认证状态 ----
const booting = ref(true)
const me = ref(null)                 // {email, name, role} | null
const needsSetup = ref(false)
const loginToken = ref('')
const loginError = ref('')
const setupEmail = ref('')
const setupName = ref('')
const setupResult = ref(null)
const setupError = ref('')

// ---- 数据 ----
const dashboard = ref(null)
const devices = ref([])
const leases = ref([])
const credentials = ref([])
const notices = ref([])
const patrolRuns = ref([])
const queueData = ref(null)
const anomalies = ref(null)
const audit = ref(null)
const auditAction = ref('')
const settings = ref(null)

const contacts = ref([])
const contactForm = ref({ email: '', name: '', note: '' })
const editDialog = ref(false)
const editRow = ref({ email: '', name: '', note: '' })
const smtp = ref(null)
const smtpTestTo = ref('')
const sendForm = ref({ emails: [], manual_emails: '', role: 'user', note: '' })
const sendResults = ref([])

const newProduct = ref({ type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1 })
const productTypes = ref([])
const ptEditDialog = ref(false)
const ptEditRow = ref({ type_code: '', display_name: '', defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', jenkins_url: '', notes: '' })

const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
async function api(path, opts) {
  const r = await fetch(API_BASE + path, { credentials: 'include', ...opts })
  if (!r.ok) {
    let detail = String(r.status)
    try { const j = await r.json(); if (j.detail) detail = j.detail } catch (e) { /* ignore */ }
    if (r.status === 401 && me.value && !path.startsWith('/api/auth/')) me.value = null
    throw new Error(path + ' -> ' + detail)
  }
  return r.json()
}

async function loadDashboard() { dashboard.value = await api('/api/dashboard') }
async function loadDevices() { devices.value = await api('/api/devices') }
async function loadLeases() { leases.value = await api('/api/leases') }
async function loadCredentials() { credentials.value = await api('/api/credentials') }
async function loadNotices() { notices.value = await api('/api/notices') }
async function loadPatrol() { patrolRuns.value = await api('/api/patrol/runs') }
async function loadQueue() { queueData.value = await api('/api/queue') }
async function loadAnomalies() { anomalies.value = await api('/api/anomalies') }
async function loadSettings() { settings.value = await api('/api/settings') }
async function loadProductTypes() { productTypes.value = await api('/api/product-types') }
async function loadContacts() { contacts.value = await api('/api/contacts') }
async function loadSmtp() { smtp.value = await api('/api/settings/smtp') }

async function loadAudit() {
  const q = auditAction.value ? '?action=' + encodeURIComponent(auditAction.value) : ''
  audit.value = await api('/api/audit' + q)
}

function switchPage(key) {
  active.value = key
  if (key === 'dashboard') loadDashboard()
  else if (key === 'devices') loadDevices()
  else if (key === 'leases') loadLeases()
  else if (key === 'credentials') loadCredentials()
  else if (key === 'queue') loadQueue()
  else if (key === 'anomaly') { loadAnomalies(); loadNotices(); loadPatrol() }
  else if (key === 'audit') loadAudit()
  else if (key === 'settings') { loadSettings(); loadProductTypes(); loadContacts(); loadSmtp() }
}

// ---- 启动 / 登录 ----
async function boot() {
  booting.value = true
  try {
    const st = await api('/api/auth/setup-status')
    needsSetup.value = !!st.needs_setup
  } catch (e) { needsSetup.value = false }
  if (!needsSetup.value) {
    try {
      const r = await fetch(API_BASE + '/api/auth/me', { credentials: 'include' })
      me.value = r.ok ? await r.json() : null
    } catch (e) { me.value = null }
  }
  booting.value = false
  if (me.value) loadDashboard()
}
onMounted(boot)

async function doLogin() {
  loginError.value = ''
  try {
    me.value = await api('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: loginToken.value.trim() }),
    })
    loginToken.value = ''
    active.value = 'dashboard'
    loadDashboard()
  } catch (e) { loginError.value = e.message }
}

async function doLogout() {
  try { await api('/api/auth/logout', { method: 'POST' }) } catch (e) { /* ignore */ }
  me.value = null
  active.value = 'dashboard'
  setupResult.value = null
}

async function doSetup() {
  setupError.value = ''
  try {
    setupResult.value = await api('/api/auth/setup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: setupEmail.value.trim(), name: setupName.value.trim() }),
    })
    needsSetup.value = false
  } catch (e) { setupError.value = e.message }
}

async function gotoLoginFromSetup() {
  loginToken.value = setupResult.value.token
  setupResult.value = null
  await doLogin()
}

// ---- 通讯录 ----
async function addContact() {
  if (!contactForm.value.email.trim()) { ElMessage.warning('请填邮箱'); return }
  try {
    await api('/api/contacts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(contactForm.value),
    })
    ElMessage.success('已新增联系人')
    contactForm.value = { email: '', name: '', note: '' }
    loadContacts()
  } catch (e) { ElMessage.error(e.message) }
}
function openEdit(row) { editRow.value = { email: row.email, name: row.name, note: row.note }; editDialog.value = true }
async function saveEdit() {
  try {
    await api('/api/contacts/' + encodeURIComponent(editRow.value.email), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: editRow.value.name, note: editRow.value.note }),
    })
    ElMessage.success('已保存')
    editDialog.value = false
    loadContacts()
  } catch (e) { ElMessage.error(e.message) }
}
async function deleteContact(row) {
  try {
    await api('/api/contacts/' + encodeURIComponent(row.email), { method: 'DELETE' })
    ElMessage.success('已删除')
    loadContacts()
  } catch (e) { ElMessage.error(e.message) }
}

// ---- SMTP ----
async function saveSmtp() {
  try {
    smtp.value = await api('/api/settings/smtp', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(smtp.value),
    })
    ElMessage.success('SMTP 配置已保存')
  } catch (e) { ElMessage.error(e.message) }
}
async function testSmtp() {
  if (!smtpTestTo.value.trim()) { ElMessage.warning('请填收件人邮箱'); return }
  try {
    await saveSmtp()
    const r = await api('/api/settings/smtp/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to: smtpTestTo.value.trim() }),
    })
    ElMessage.success('测试邮件已发送至 ' + r.to)
  } catch (e) { ElMessage.error('发送失败：' + e.message) }
}

// ---- 发接入凭据 ----
async function sendCredentials() {
  sendResults.value = []
  try {
    const r = await api('/api/credentials/send', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sendForm.value),
    })
    sendResults.value = r.results
    const ok = r.results.filter(x => x.sent).length
    ElMessage.success('已生成 ' + r.count + ' 条凭据，成功发送 ' + ok + ' 封邮件')
    loadCredentials()
  } catch (e) { ElMessage.error(e.message) }
}

// ---- 凭据 / 机型 ----
async function revokeCredential(id) {
  await api('/api/credentials/' + id + '/revoke', { method: 'POST' })
  ElMessage.success('已吊销'); loadCredentials()
}
async function renewCredential(id) {
  await api('/api/credentials/renew', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ credential_id: id, days: 90 }),
  })
  ElMessage.success('已续期 90 天'); loadCredentials()
}
async function addProductType() {
  await api('/api/product-types', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(newProduct.value),
  })
  ElMessage.success('已新增机型')
  newProduct.value = { type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1 }
  loadSettings()
}
async function disableProductType(code) {
  await api('/api/product-types/' + encodeURIComponent(code) + '/disable', { method: 'POST' })
  ElMessage.success('已停用 ' + code); loadSettings()
}
function openPtEdit(row) {
  ptEditRow.value = {
    type_code: row.type_code, display_name: row.display_name,
    defconfig: row.defconfig || '', sdk_repo_cmd: row.sdk_repo_cmd || '',
    build_cmd: row.build_cmd || '', image_path: row.image_path || '',
    jenkins_url: row.jenkins_url || '', notes: row.notes || '',
  }
  ptEditDialog.value = true
}
async function savePtEdit() {
  try {
    const r = await api('/api/product-types/' + encodeURIComponent(ptEditRow.value.type_code), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        defconfig: ptEditRow.value.defconfig, sdk_repo_cmd: ptEditRow.value.sdk_repo_cmd,
        build_cmd: ptEditRow.value.build_cmd, image_path: ptEditRow.value.image_path,
        jenkins_url: ptEditRow.value.jenkins_url, notes: ptEditRow.value.notes,
      }),
    })
    ElMessage.success('已保存（' + r.complete_label + '）')
    ptEditDialog.value = false
    loadProductTypes(); loadSettings()
  } catch (e) { ElMessage.error(e.message) }
}

function filterAudit(action) { auditAction.value = action || ''; loadAudit() }
function offerCountdown(row) {
  if (row.state !== 'OFFERED' || row.offer_remaining_seconds == null) return '-'
  const s = row.offer_remaining_seconds
  return s > 0 ? `${s} 秒后过期` : '已过期'
}
const statusTag = {
  available: 'success', occupied: 'warning', pending: 'info',
  pending_removal: 'warning', retired: 'info', offline: 'danger', maintenance: 'danger',
}
</script>

<template>
  <div v-if="booting" class="auth-screen"><p>加载中…</p></div>

  <!-- 首次引导：库中无任何 admin token -->
  <div v-else-if="needsSetup" class="auth-screen">
    <el-card class="auth-card">
      <h2>Quamm TRES · 首次引导</h2>
      <template v-if="!setupResult">
        <p>系统中还没有任何管理员凭据。请填写邮箱，生成第一个管理员 token。</p>
        <el-form label-width="70px">
          <el-form-item label="邮箱"><el-input v-model="setupEmail" placeholder="admin@quaming.com" /></el-form-item>
          <el-form-item label="姓名"><el-input v-model="setupName" placeholder="可选" /></el-form-item>
          <el-form-item><el-button type="primary" @click="doSetup">生成管理员 token</el-button></el-form-item>
        </el-form>
        <p v-if="setupError" class="err">{{ setupError }}</p>
      </template>
      <template v-else>
        <p>管理员凭据已生成，请复制保存（仅显示这一次）：</p>
        <el-input type="textarea" :rows="3" readonly :model-value="setupResult.token" />
        <el-button type="primary" style="margin-top: 12px" @click="gotoLoginFromSetup">用此 token 登录</el-button>
      </template>
    </el-card>
  </div>

  <!-- 登录页 -->
  <div v-else-if="!me" class="auth-screen">
    <el-card class="auth-card">
      <h2>Quamm TRES 登录</h2>
      <p>请输入你的 rr_ 接入凭据（与 MCP 同一套 token）。</p>
      <el-input v-model="loginToken" placeholder="rr_..." @keyup.enter="doLogin" />
      <div style="margin-top: 12px"><el-button type="primary" @click="doLogin">登录</el-button></div>
      <p v-if="loginError" class="err">{{ loginError }}</p>
    </el-card>
  </div>

  <!-- 主界面 -->
  <el-container v-else style="height: 100vh">
    <el-aside width="200px" style="border-right: 1px solid #eee; display: flex; flex-direction: column">
      <div style="padding: 16px; font-weight: 700">Quamm TRES</div>
      <el-menu :default-active="active" @select="switchPage" style="flex: 1">
        <el-menu-item v-for="p in pages" :key="p.key" :index="p.key">{{ p.label }}</el-menu-item>
      </el-menu>
      <div class="user-box">
        <div style="font-weight: 600">{{ me.name || me.email }}</div>
        <div style="font-size: 12px; color: #909399; word-break: break-all">{{ me.email }}</div>
        <el-tag size="small" :type="me.role === 'admin' ? 'danger' : 'info'" style="margin-top: 6px">{{ me.role }}</el-tag>
        <el-button size="small" style="margin-top: 8px; width: 100%" @click="doLogout">退出登录</el-button>
      </div>
    </el-aside>

    <el-main>
      <!-- 看板 -->
      <div v-if="active === 'dashboard'">
        <h2>看板</h2>
        <el-row :gutter="12" v-if="dashboard">
          <el-col :span="4"><el-card><div>设备总数</div><div style="font-size: 28px">{{ dashboard.total_devices }}</div></el-card></el-col>
          <el-col :span="4"><el-card><div>可用</div><div style="font-size: 28px; color: #67c23a">{{ dashboard.available }}</div></el-card></el-col>
          <el-col :span="4"><el-card><div>占用</div><div style="font-size: 28px; color: #e6a23c">{{ dashboard.by_status.occupied || 0 }}</div></el-card></el-col>
          <el-col :span="4"><el-card><div>离线</div><div style="font-size: 28px; color: #f56c6c">{{ dashboard.by_status.offline || 0 }}</div></el-card></el-col>
          <el-col :span="4"><el-card><div>活跃租约</div><div style="font-size: 28px">{{ dashboard.active_leases == null ? '-' : dashboard.active_leases }}</div></el-card></el-col>
          <el-col :span="4"><el-card><div>排队中</div><div style="font-size: 28px">{{ dashboard.queued == null ? '-' : dashboard.queued }}</div></el-card></el-col>
        </el-row>
        <p v-if="dashboard && dashboard.last_patrol" style="color: #909399">
          上次巡检：{{ dashboard.last_patrol.ended_at }}（L3 {{ dashboard.last_patrol.l3_count }} / suspect {{ dashboard.last_patrol.suspect_count }} / offline {{ dashboard.last_patrol.offline_count }}）
        </p>
        <h3>机型余量</h3>
        <el-table :data="dashboard ? dashboard.products : []" border>
          <el-table-column prop="type_code" label="机型" />
          <el-table-column prop="display_name" label="名称" />
          <el-table-column prop="total" label="总数" />
          <el-table-column prop="available" label="可用" />
          <el-table-column prop="leased" label="已租(leased)" width="120" />
          <el-table-column prop="queued" label="排队(queued)" width="120" />
          <el-table-column prop="occupied" label="占用" />
        </el-table>
      </div>

      <!-- 设备 -->
      <div v-else-if="active === 'devices'">
        <h2>设备</h2>
        <el-table :data="devices" border>
          <el-table-column prop="device_id" label="ID" width="140" />
          <el-table-column prop="display_name" label="名称" />
          <el-table-column prop="model_class" label="机型" width="130" />
          <el-table-column prop="project_name" label="工程配置" width="160" />
          <el-table-column prop="position_code" label="位置" width="130" />
          <el-table-column prop="owner" label="责任人" width="130" />
          <el-table-column prop="health_level" label="健康" width="80" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }"><el-tag :type="statusTag[row.status] || 'info'">{{ row.status }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="adb_address" label="ADB 地址" width="160" />
        </el-table>
      </div>

      <!-- 占用（申请人 email + name） -->
      <div v-else-if="active === 'leases'">
        <h2>占用</h2>
        <el-table :data="leases" border>
          <el-table-column prop="lease_id" label="租约" width="140" />
          <el-table-column prop="display_name" label="设备" />
          <el-table-column prop="email" label="申请人邮箱" width="200" />
          <el-table-column prop="name" label="申请人姓名" width="140" />
          <el-table-column prop="purpose" label="用途" />
          <el-table-column prop="expires_at" label="到期" width="200" />
          <el-table-column prop="state" label="状态" width="100" />
        </el-table>
      </div>

      <!-- 排队 -->
      <div v-else-if="active === 'queue'">
        <h2>排队</h2>
        <h3>按机型组</h3>
        <el-table :data="queueData ? queueData.by_model : []" border>
          <el-table-column prop="target_code" label="机型" />
          <el-table-column prop="queued" label="排队数" />
          <el-table-column prop="offered" label="已发 offer" />
          <el-table-column label="等待者"><template #default="{ row }">{{ row.waiters.length }}</template></el-table-column>
        </el-table>
        <h3>按设备</h3>
        <el-table :data="queueData ? queueData.by_device : []" border>
          <el-table-column prop="target_code" label="设备" />
          <el-table-column prop="queued" label="排队数" />
          <el-table-column prop="offered" label="已发 offer" />
          <el-table-column label="等待者"><template #default="{ row }">{{ row.waiters.length }}</template></el-table-column>
        </el-table>
        <h3>全部条目（点行首 + 展开看「是谁 / 等哪台机型 / 等多久」）</h3>
        <el-table :data="queueData ? queueData.entries : []" border>
          <el-table-column type="expand">
            <template #default="{ row }">
              <div style="padding: 8px 24px; line-height: 1.9">
                <div><b>申请人：</b>{{ row.name || '-' }} &lt;{{ row.email || '-' }}&gt;</div>
                <div><b>agent_label：</b>{{ row.agent_label || '-' }}</div>
                <div><b>等待机型：</b>{{ row.model || '-' }}（{{ row.target_type === 'product' ? '按机型池' : '指定设备 ' + row.target_code }}）</div>
                <div><b>已等待：</b>{{ row.waited_text }}</div>
                <div><b>用途：</b>{{ row.purpose || '-' }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="entry_id" label="ticket" width="140" />
          <el-table-column prop="target_code" label="目标" width="150" />
          <el-table-column prop="target_type" label="类型" width="90" />
          <el-table-column prop="email" label="申请者" width="180" />
          <el-table-column prop="priority" label="priority" width="90" />
          <el-table-column prop="position" label="队位" width="70" />
          <el-table-column prop="state" label="状态" width="110" />
          <el-table-column label="已等待" width="130"><template #default="{ row }">{{ row.waited_text }}</template></el-table-column>
          <el-table-column label="offer 倒计时" width="130"><template #default="{ row }">{{ offerCountdown(row) }}</template></el-table-column>
          <el-table-column prop="joined_at" label="入队时间" width="200" />
        </el-table>
      </div>

      <!-- 异常 / 待修复 -->
      <div v-else-if="active === 'anomaly'">
        <h2>异常 / 待修复</h2>
        <h3>offline 设备（连续失败轮次）</h3>
        <el-table :data="anomalies ? anomalies.offline : []" border>
          <el-table-column prop="device_id" label="ID" width="140" />
          <el-table-column prop="display_name" label="名称" />
          <el-table-column prop="model_class" label="机型" width="130" />
          <el-table-column prop="owner" label="责任人" width="150" />
          <el-table-column prop="position_code" label="位置" width="130" />
          <el-table-column prop="health_level" label="健康" width="80" />
          <el-table-column prop="consecutive_failures" label="连续失败" width="90" />
          <el-table-column prop="last_seen_at" label="最后在线" width="200" />
        </el-table>
        <h3>suspect（降级未掉线）</h3>
        <el-table :data="anomalies ? anomalies.suspect : []" border>
          <el-table-column prop="device_id" label="ID" width="140" />
          <el-table-column prop="display_name" label="名称" />
          <el-table-column prop="owner" label="责任人" width="150" />
          <el-table-column prop="position_code" label="位置" width="130" />
          <el-table-column prop="health_level" label="健康" width="80" />
          <el-table-column prop="consecutive_failures" label="连续失败" width="90" />
        </el-table>
        <h3>待清理（pending 超 {{ anomalies ? anomalies.pending_cleanup_days : 30 }} 天）</h3>
        <el-table :data="anomalies ? anomalies.cleanup : []" border>
          <el-table-column prop="device_id" label="ID" width="140" />
          <el-table-column prop="display_name" label="名称" />
          <el-table-column prop="owner" label="责任人" width="150" />
          <el-table-column prop="position_code" label="位置" width="130" />
          <el-table-column prop="days_pending" label="pending 天数" width="120" />
          <el-table-column prop="created_at" label="登记时间" width="200" />
        </el-table>
        <h3>通知流水</h3>
        <el-table :data="notices" border>
          <el-table-column prop="created_at" label="时间" width="200" />
          <el-table-column prop="event_type" label="类型" width="160" />
          <el-table-column prop="title" label="标题" />
          <el-table-column prop="severity" label="级别" width="90" />
        </el-table>
        <h3>最近巡检</h3>
        <el-table :data="patrolRuns" border>
          <el-table-column prop="ended_at" label="时间" width="200" />
          <el-table-column prop="scanned_count" label="扫描" width="90" />
          <el-table-column prop="l3_count" label="L3" width="80" />
          <el-table-column prop="suspect_count" label="suspect" width="100" />
          <el-table-column prop="offline_count" label="offline" width="100" />
        </el-table>
      </div>

      <!-- 凭据台账 -->
      <div v-else-if="active === 'credentials'">
        <h2>凭据</h2>
        <el-table :data="credentials" border>
          <el-table-column prop="email" label="邮箱" width="200" />
          <el-table-column prop="name" label="姓名" width="120" />
          <el-table-column prop="role" label="权限" width="80" />
          <el-table-column prop="token_prefix" label="token 前缀" width="140" />
          <el-table-column prop="note" label="备注" width="140" />
          <el-table-column label="是否激活" width="100">
            <template #default="{ row }"><el-tag :type="row.active ? 'success' : 'danger'">{{ row.active ? '激活' : '停用' }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="expires_at" label="到期时间" width="200" />
          <el-table-column prop="created_at" label="创建时间" width="200" />
          <el-table-column label="操作" width="160">
            <template #default="{ row }">
              <el-button size="small" @click="renewCredential(row.id)">续期</el-button>
              <el-button size="small" type="danger" @click="revokeCredential(row.id)">吊销</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 审计 -->
      <div v-else-if="active === 'audit'">
        <h2>审计</h2>
        <div style="margin-bottom: 12px">
          <el-select v-model="auditAction" placeholder="按动作筛选（全部）" clearable @change="filterAudit" style="width: 240px">
            <el-option v-for="a in (audit ? audit.actions : [])" :key="a" :label="a" :value="a" />
          </el-select>
        </div>
        <el-table :data="audit ? audit.events : []" border>
          <el-table-column prop="created_at" label="时间" width="200" />
          <el-table-column prop="actor" label="主体" width="180" />
          <el-table-column prop="action" label="动作" width="160" />
          <el-table-column prop="object" label="对象" width="150" />
          <el-table-column label="详情"><template #default="{ row }">{{ row.detail ? JSON.stringify(row.detail) : '-' }}</template></el-table-column>
        </el-table>
      </div>

      <!-- 设置：系统 + 通讯录 + 发凭据 + SMTP -->
      <div v-else-if="active === 'settings'">
        <h2>设置</h2>
        <el-tabs v-model="settingsTab">
          <el-tab-pane label="系统设置" name="system">
            <el-descriptions v-if="settings" :column="2" border style="margin-bottom: 16px">
              <el-descriptions-item label="位置规则">{{ settings.location_rule.format }}（{{ settings.location_rule.description }}）</el-descriptions-item>
              <el-descriptions-item label="探测后端">{{ settings.probe_backend }}</el-descriptions-item>
              <el-descriptions-item label="巡检间隔">{{ settings.patrol_interval_minutes }} 分钟</el-descriptions-item>
              <el-descriptions-item label="巡检失败阈值">{{ settings.patrol_fail_threshold }} 轮</el-descriptions-item>
              <el-descriptions-item label="单人并发上限">{{ settings.holder_concurrency_limit }} 台</el-descriptions-item>
              <el-descriptions-item label="租约 TTL">{{ settings.lease_ttl_minutes }} 分钟</el-descriptions-item>
              <el-descriptions-item label="宽限期">{{ settings.lease_grace_minutes }} 分钟</el-descriptions-item>
              <el-descriptions-item label="offer 窗口">{{ settings.offer_window_seconds }} 秒</el-descriptions-item>
              <el-descriptions-item label="凭据有效期">{{ settings.credential_valid_days }} 天</el-descriptions-item>
              <el-descriptions-item label="allow_apply_when_suspect"><el-switch :model-value="settings.allow_apply_when_suspect" disabled /></el-descriptions-item>
            </el-descriptions>
            <h3>机型字典</h3>
            <el-table :data="settings ? settings.product_types : []" border>
              <el-table-column prop="type_code" label="type_code" width="150" />
              <el-table-column prop="display_name" label="名称" />
              <el-table-column prop="has_screen" label="有屏" width="70" />
              <el-table-column prop="has_led" label="有灯" width="70" />
              <el-table-column prop="serial_count" label="串口数" width="90" />
              <el-table-column label="状态" width="90">
                <template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template>
              </el-table-column>
              <el-table-column label="操作" width="100">
                <template #default="{ row }"><el-button size="small" type="danger" :disabled="!row.enabled" @click="disableProductType(row.type_code)">停用</el-button></template>
              </el-table-column>
            </el-table>
            <h3>新增机型</h3>
            <el-form inline>
              <el-form-item label="type_code"><el-input v-model="newProduct.type_code" placeholder="QM200X" /></el-form-item>
              <el-form-item label="名称"><el-input v-model="newProduct.display_name" placeholder="QM200X" /></el-form-item>
              <el-form-item label="有屏"><el-switch v-model="newProduct.has_screen" /></el-form-item>
              <el-form-item label="有灯"><el-switch v-model="newProduct.has_led" /></el-form-item>
              <el-form-item label="串口数"><el-input-number v-model="newProduct.serial_count" :min="0" /></el-form-item>
              <el-form-item><el-button type="primary" @click="addProductType">新增</el-button></el-form-item>
            </el-form>
          </el-tab-pane>

          <el-tab-pane label="设备类型" name="product-types">
            <h3>设备类型元数据（defconfig / SDK 获取 / 编译 / 镜像 / Jenkins / 备注）</h3>
            <p style="color: #909399">四列（defconfig + SDK 获取命令 + 编译命令 + 镜像路径）都填才算【完整】，缺任一即【信息不全】，允许信息不全的机型存在。</p>
            <el-table :data="productTypes" border>
              <el-table-column prop="type_code" label="type_code" width="150" />
              <el-table-column prop="display_name" label="名称" width="140" />
              <el-table-column prop="defconfig" label="defconfig" width="200" show-overflow-tooltip />
              <el-table-column label="完整度" width="110">
                <template #default="{ row }"><el-tag :type="row.complete ? 'success' : 'warning'">{{ row.complete_label }}</el-tag></template>
              </el-table-column>
              <el-table-column label="操作" width="100">
                <template #default="{ row }">
                  <el-button v-if="me.role === 'admin'" size="small" @click="openPtEdit(row)">编辑</el-button>
                  <span v-else style="color: #c0c4cc; font-size: 12px">仅 admin 可改</span>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <el-tab-pane v-if="me.role === 'admin'" label="人员通讯录" name="contacts">
            <h3>人员通讯录</h3>
            <el-table :data="contacts" border>
              <el-table-column prop="email" label="邮箱" width="240" />
              <el-table-column prop="name" label="姓名" width="160" />
              <el-table-column prop="note" label="备注" />
              <el-table-column prop="created_at" label="创建时间" width="200" />
              <el-table-column label="操作" width="150">
                <template #default="{ row }">
                  <el-button size="small" @click="openEdit(row)">编辑</el-button>
                  <el-button size="small" type="danger" @click="deleteContact(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <h3>新增联系人</h3>
            <el-form inline>
              <el-form-item label="邮箱"><el-input v-model="contactForm.email" placeholder="name@quaming.com" /></el-form-item>
              <el-form-item label="姓名"><el-input v-model="contactForm.name" /></el-form-item>
              <el-form-item label="备注"><el-input v-model="contactForm.note" /></el-form-item>
              <el-form-item><el-button type="primary" @click="addContact">新增</el-button></el-form-item>
            </el-form>
          </el-tab-pane>

          <el-tab-pane v-if="me.role === 'admin'" label="发接入凭据" name="send">
            <h3>发接入凭据（生成 rr_ token 并邮件发送 MCP 配置）</h3>
            <el-form label-width="100px" style="max-width: 640px">
              <el-form-item label="从通讯录选">
                <el-select v-model="sendForm.emails" multiple filterable placeholder="可多选" style="width: 100%">
                  <el-option v-for="c in contacts" :key="c.email" :label="(c.name || c.email) + ' <' + c.email + '>'" :value="c.email" />
                </el-select>
              </el-form-item>
              <el-form-item label="或手填邮箱">
                <el-input v-model="sendForm.manual_emails" type="textarea" :rows="2" placeholder="多个邮箱用逗号 / 换行分隔" />
              </el-form-item>
              <el-form-item label="权限">
                <el-select v-model="sendForm.role" style="width: 160px">
                  <el-option label="user（普通）" value="user" />
                  <el-option label="admin（管理员）" value="admin" />
                </el-select>
              </el-form-item>
              <el-form-item label="备注"><el-input v-model="sendForm.note" placeholder="可选，写入凭据备注与邮件" /></el-form-item>
              <el-form-item><el-button type="primary" @click="sendCredentials">发送</el-button></el-form-item>
            </el-form>
            <el-table v-if="sendResults.length" :data="sendResults" border style="margin-top: 12px">
              <el-table-column prop="email" label="邮箱" width="240" />
              <el-table-column prop="role" label="权限" width="80" />
              <el-table-column label="token" width="220"><template #default="{ row }"><span style="font-size:12px">{{ row.token }}</span></template></el-table-column>
              <el-table-column label="发送结果" width="100"><template #default="{ row }"><el-tag :type="row.sent ? 'success' : 'danger'">{{ row.sent ? '成功' : '失败' }}</el-tag></template></el-table-column>
              <el-table-column prop="error" label="错误" />
            </el-table>
          </el-tab-pane>

          <el-tab-pane v-if="me.role === 'admin'" label="SMTP 配置" name="smtp">
            <h3>SMTP 配置</h3>
            <el-form v-if="smtp" label-width="100px" style="max-width: 560px">
              <el-form-item label="server"><el-input v-model="smtp.server" /></el-form-item>
              <el-form-item label="port"><el-input-number v-model="smtp.port" :min="1" :max="65535" /></el-form-item>
              <el-form-item label="user"><el-input v-model="smtp.user" /></el-form-item>
              <el-form-item label="password"><el-input v-model="smtp.password" type="password" show-password /></el-form-item>
              <el-form-item label="use_ssl"><el-switch v-model="smtp.use_ssl" active-value="true" inactive-value="false" /></el-form-item>
              <el-form-item label="from"><el-input v-model="smtp.from" /></el-form-item>
              <el-form-item><el-button type="primary" @click="saveSmtp">保存配置</el-button></el-form-item>
            </el-form>
            <h3>发测试邮件</h3>
            <el-form inline>
              <el-form-item label="收件人"><el-input v-model="smtpTestTo" placeholder="name@quaming.com" style="width: 260px" /></el-form-item>
              <el-form-item><el-button @click="testSmtp">发测试邮件</el-button></el-form-item>
            </el-form>
          </el-tab-pane>
        </el-tabs>

        <el-dialog v-model="editDialog" title="编辑联系人" width="440px">
          <el-form label-width="70px">
            <el-form-item label="邮箱"><el-input v-model="editRow.email" disabled /></el-form-item>
            <el-form-item label="姓名"><el-input v-model="editRow.name" /></el-form-item>
            <el-form-item label="备注"><el-input v-model="editRow.note" /></el-form-item>
          </el-form>
          <template #footer><el-button @click="editDialog = false">取消</el-button><el-button type="primary" @click="saveEdit">保存</el-button></template>
        </el-dialog>

        <el-dialog v-model="ptEditDialog" :title="'编辑设备类型 · ' + ptEditRow.type_code" width="640px">
          <el-form label-width="120px">
            <el-form-item label="defconfig"><el-input v-model="ptEditRow.defconfig" placeholder="project_moto_ld_defconfig" /></el-form-item>
            <el-form-item label="SDK 获取命令"><el-input v-model="ptEditRow.sdk_repo_cmd" type="textarea" :rows="2" placeholder="repo init -u http://.../manifest; repo sync -j8" /></el-form-item>
            <el-form-item label="编译命令"><el-input v-model="ptEditRow.build_cmd" type="textarea" :rows="2" placeholder="make distclean && make xxx_defconfig && make xos -j8" /></el-form-item>
            <el-form-item label="镜像路径"><el-input v-model="ptEditRow.image_path" placeholder="out/qm10xd_linux/qmimages/" /></el-form-item>
            <el-form-item label="Jenkins 链接"><el-input v-model="ptEditRow.jenkins_url" placeholder="http://.../Daily/XOS/moto_ld_102d/" /></el-form-item>
            <el-form-item label="备注"><el-input v-model="ptEditRow.notes" type="textarea" :rows="2" placeholder="其他备注" /></el-form-item>
          </el-form>
          <template #footer><el-button @click="ptEditDialog = false">取消</el-button><el-button type="primary" @click="savePtEdit">保存</el-button></template>
        </el-dialog>
      </div>
    </el-main>
  </el-container>
</template>

<style>
.auth-screen { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: #f5f7fa; }
.auth-card { width: 420px; }
.auth-card h2 { margin-top: 0; }
.err { color: #f56c6c; font-size: 13px; }
.user-box { padding: 12px 16px; border-top: 1px solid #eee; }
</style>
