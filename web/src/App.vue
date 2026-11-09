<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

const pages = [
  { key: 'dashboard', label: '看板' },
  { key: 'devices', label: '设备' },
  { key: 'scheduling', label: '资源调度' },
  { key: 'onboarding', label: '接入申请', admin: true },
  { key: 'credentials', label: '凭据' },
  { key: 'settings', label: '设置' },
]
const active = ref('dashboard')
const settingsTab = ref('system')
const schedulingTab = ref('leases')

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
// v0.17：设备按机型分组 + 关键字搜索 / 机型筛选
const deviceSearch = ref('')
const selectedDeviceType = ref(null)
const detailStatusTab = ref('all')
const classifiedDevices = computed(() => devices.value.filter((d) => d.model_class))
const deviceTotals = computed(() => {
  const list = classifiedDevices.value
  return {
    total: list.length,
    occupied: list.filter((d) => d.status === 'occupied').length,
    available: list.filter((d) => d.status === 'available').length,
    offline: list.filter((d) => d.status === 'offline').length,
    pending: list.filter((d) => d.status === 'pending').length,
  }
})
const deviceOverview = computed(() => {
  const map = new Map()
  for (const d of classifiedDevices.value) {
    if (!map.has(d.model_class)) map.set(d.model_class, [])
    map.get(d.model_class).push(d)
  }
  return [...map.entries()].map(([type, list]) => ({
    type,
    count: list.length,
    available: list.filter((d) => d.status === 'available').length,
    occupied: list.filter((d) => d.status === 'occupied').length,
    offline: list.filter((d) => d.status === 'offline').length,
    pending: list.filter((d) => d.status === 'pending').length,
  })).sort((a, b) => a.type.localeCompare(b.type))
})
const detailDevices = computed(() => {
  let list = classifiedDevices.value.filter((d) => d.model_class === selectedDeviceType.value)
  if (detailStatusTab.value !== 'all') list = list.filter((d) => d.status === detailStatusTab.value)
  const kw = deviceSearch.value.trim().toLowerCase()
  if (kw) {
    list = list.filter((d) =>
      [d.device_id, d.display_name, d.project_name, d.position_code, d.owner, d.adb_address,
       d.status, d.health_level, d.mac, d.register_agent]
        .filter((v) => v !== null && v !== undefined).join(' ').toLowerCase().includes(kw))
  }
  return list
})
function openDeviceType(type) {
  selectedDeviceType.value = type
  detailStatusTab.value = 'all'
  deviceSearch.value = ''
}
function backToOverview() { selectedDeviceType.value = null }

const leases = ref([])
const credentials = ref([])
const credentialTab = ref('active')
const notices = ref([])
const patrolRuns = ref([])
const queueData = ref(null)
const anomalies = ref(null)
const audit = ref(null)
const auditAction = ref('')
const feedbackList = ref([])
const feedbackStatus = ref('')
const settings = ref(null)
const settingsForm = ref({
  patrol_interval_minutes: 30,
  patrol_fail_threshold: 2,
  holder_concurrency_limit: 2,
  allow_apply_when_suspect: false,
  lease_ttl_minutes: 30,
  lease_grace_minutes: 10,
  lease_max_minutes: 240,
  offer_window_seconds: 60,
  credential_valid_days: 90,
  heartbeat_required: true,
  heartbeat_interval_minutes: 75,
  heartbeat_miss_grace_minutes: 10,
})
const settingsSaving = ref(false)

const contacts = ref([])
const contactForm = ref({ email: '', name: '', note: '' })
const editDialog = ref(false)
const editRow = ref({ email: '', name: '', note: '' })
const smtp = ref(null)
const smtpTestTo = ref('')
const sendForm = ref({ emails: [], manual_emails: '', manual_holders: [], role: 'user', note: '', valid_days: 90 })
const holderDraft = ref({ name: '', note: '' })
const previewVisible = ref(false)
const preview = ref(null)          // batch-generate 返回的完整响应
const generating = ref(false)
const sending = ref(false)
const sendResults = ref([])
// 同一批发放请求的幂等键：网络重试时保持不变，避免重复建凭据；发送完成或放弃后换新
const idempotencyKey = ref('')
function newIdempotencyKey() {
  idempotencyKey.value = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : 'idem-' + Date.now() + '-' + Math.random().toString(16).slice(2)
}
newIdempotencyKey()

// Web 手工添加设备（两步：探测 -> 确认注册）
const deviceAddVisible = ref(false)
const deviceAddStep = ref(1)
const deviceAddProbing = ref(false)
const deviceAddSaving = ref(false)
const deviceAddForm = ref({ ip: '', port: 5555, hint_model: '' })
const deviceAddProbe = ref(null)
const deviceAddConfirm = ref({ position_code: '', owner: '', model_class: '', note: '' })
// 设备发现模式
const deviceAddMode = ref('ip')
const discoveryNetworks = ref('')
const discovering = ref(false)
const discoveryResults = ref([])

function openDeviceAdd() {
  deviceAddStep.value = 1
  deviceAddMode.value = 'ip'
  deviceAddProbe.value = null
  deviceAddForm.value = { ip: '', port: 5555, hint_model: '' }
  deviceAddConfirm.value = { position_code: '', owner: '', model_class: '', note: '' }
  discoveryNetworks.value = ''
  discoveryResults.value = []
  deviceAddVisible.value = true
}

async function runDiscovery() {
  discovering.value = true
  try {
    const nets = discoveryNetworks.value.split(',').map((s) => s.trim()).filter(Boolean)
    const r = await api('/api/devices/discover', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(nets.length ? { networks: nets } : {}),
    })
    discoveryResults.value = r.devices || []
    ElMessage.success('发现 ' + r.found + ' 台设备（扫描 ' + r.total_hosts + ' 个 IP）')
  } catch (e) { ElMessage.error(e.message) } finally { discovering.value = false }
}

function pickDiscovered(row) {
  if (!row.form_id) { ElMessage.error(row.error || '该设备无可用表单，请重新探测'); return }
  deviceAddProbe.value = {
    form_id: row.form_id,
    probe: row.probe,
    identity_match: row.identity_match,
  }
  deviceAddConfirm.value = {
    position_code: '',
    owner: '',
    model_class: row.probe?.model || '',
    note: '',
  }
  deviceAddStep.value = 2
}

async function submitDeviceProbe() {
  if (!deviceAddForm.value.ip.trim()) { ElMessage.error('请填写设备 IP'); return }
  deviceAddProbing.value = true
  try {
    const r = await api('/api/devices/probe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ip: deviceAddForm.value.ip.trim(),
        port: deviceAddForm.value.port || 5555,
        hint_model: deviceAddForm.value.hint_model || null,
      }),
    })
    deviceAddProbe.value = r
    deviceAddConfirm.value.model_class = r.probe?.model || deviceAddForm.value.hint_model || ''
    deviceAddStep.value = 2
  } catch (e) { ElMessage.error(e.message) } finally { deviceAddProbing.value = false }
}

async function submitDeviceRegister() {
  if (!deviceAddConfirm.value.position_code.trim()) { ElMessage.error('请填写位置'); return }
  if (!deviceAddConfirm.value.owner.trim()) { ElMessage.error('请填写负责人'); return }
  deviceAddSaving.value = true
  try {
    const r = await api('/api/devices/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        form_id: deviceAddProbe.value.form_id,
        position_code: deviceAddConfirm.value.position_code,
        owner: deviceAddConfirm.value.owner,
        model_class: deviceAddConfirm.value.model_class || null,
        note: deviceAddConfirm.value.note || null,
      }),
    })
    ElMessage.success('设备已保存：' + (r.device_id || r.device?.device_id || ''))
    deviceAddVisible.value = false
    loadDevices()
  } catch (e) { ElMessage.error(e.message) } finally { deviceAddSaving.value = false }
}

const productTypes = ref([])
const ptEditDialog = ref(false)
const ptEditRow = ref({ type_code: '', display_name: '', defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', jenkins_url: '', notes: '' })

// v0.16：版本/术语说明移入设置页「关于」；版本信息仍在挂载时加载
const versionInfo = ref(null)
const healthInfo = ref(null)
const ptCreateDialog = ref(false)
const ptCreateForm = ref(null)
const ptAllowIncomplete = ref(false)
const ptCreateSaving = ref(false)
const ptCreateRow = ref({
  type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1,
  defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', jenkins_url: '', notes: '',
})
const ptCreateRules = computed(() => {
  const req = { required: true, message: '该字段默认必填（可勾选底部「允许不完整」放开）', trigger: 'blur' }
  return ptAllowIncomplete.value
    ? { type_code: [{ required: true, message: 'type_code 必填', trigger: 'blur' }] }
    : {
        type_code: [{ required: true, message: 'type_code 必填', trigger: 'blur' }],
        defconfig: [req], sdk_repo_cmd: [req], build_cmd: [req],
        image_path: [req], jenkins_url: [req],
      }
})

const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
async function api(path, opts) {
  const r = await fetch(API_BASE + path, { credentials: 'include', ...opts })
  if (!r.ok) {
    let detail = String(r.status)
    try {
      const j = await r.json()
      if (j.detail != null) {
        detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
      }
    } catch (e) { /* ignore */ }
    if (r.status === 401 && me.value && !path.startsWith('/api/auth/')) me.value = null
    throw new Error(path + ' -> ' + detail)
  }
  return r.json()
}

async function loadDashboard() { dashboard.value = await api('/api/dashboard') }
async function loadDevices() {
  devices.value = await api('/api/devices')
}
function goDevices() {
  active.value = 'devices'
  selectedDeviceType.value = null
  deviceSearch.value = ''
}
async function loadLeases() { leases.value = await api('/api/leases') }
async function loadCredentials() { credentials.value = await api('/api/credentials') }
async function loadNotices() { notices.value = await api('/api/notices') }
async function loadPatrol() { patrolRuns.value = await api('/api/patrol/runs') }
async function loadQueue() { queueData.value = await api('/api/queue') }
async function loadAnomalies() { anomalies.value = await api('/api/anomalies') }
function syncSettingsForm(data) {
  if (!data) return
  settingsForm.value = {
    patrol_interval_minutes: data.patrol_interval_minutes,
    patrol_fail_threshold: data.patrol_fail_threshold,
    holder_concurrency_limit: data.holder_concurrency_limit,
    allow_apply_when_suspect: !!data.allow_apply_when_suspect,
    lease_ttl_minutes: data.lease_ttl_minutes,
    lease_grace_minutes: data.lease_grace_minutes,
    lease_max_minutes: data.lease_max_minutes,
    offer_window_seconds: data.offer_window_seconds,
    credential_valid_days: data.credential_valid_days,
    heartbeat_required: !!data.heartbeat_required,
    heartbeat_interval_minutes: data.heartbeat_interval_minutes,
    heartbeat_miss_grace_minutes: data.heartbeat_miss_grace_minutes,
  }
}
async function loadSettings() {
  settings.value = await api('/api/settings')
  syncSettingsForm(settings.value)
}
async function saveSettings() {
  if (settingsSaving.value) return
  settingsSaving.value = true
  try {
    settings.value = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settingsForm.value),
    })
    syncSettingsForm(settings.value)
    ElMessage.success('系统设置已保存')
  } catch (e) { ElMessage.error(e.message) } finally { settingsSaving.value = false }
}
async function loadProductTypes() { productTypes.value = await api('/api/product-types') }
async function loadContacts() { contacts.value = await api('/api/contacts') }
async function loadSmtp() { smtp.value = await api('/api/settings/smtp') }

async function loadAudit() {
  const q = auditAction.value ? '?action=' + encodeURIComponent(auditAction.value) : ''
  audit.value = await api('/api/audit' + q)
}

async function loadFeedback() {
  const q = feedbackStatus.value ? '?status=' + encodeURIComponent(feedbackStatus.value) : ''
  feedbackList.value = await api('/api/feedback' + q)
}

async function reviewFeedback(row, status) {
  let note = ''
  try {
    const r = await ElMessageBox.prompt('可填写评审意见（采纳理由/驳回原因，可留空）', '评审 Agent 意见', {
      confirmButtonText: '确定', cancelButtonText: '取消', inputType: 'textarea',
      inputValue: row.admin_note || '',
    })
    note = r.value || ''
  } catch (e) { return }
  try {
    await api('/api/feedback/' + row.feedback_id + '/review', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, admin_note: note }),
    })
    ElMessage.success('已评审')
    loadFeedback()
  } catch (e) { ElMessage.error(e.message) }
}

function switchPage(key) {
  active.value = key
  if (key === 'dashboard') loadDashboard()
  else if (key === 'devices') loadDevices()
  else if (key === 'scheduling') {
    loadLeases(); loadQueue(); loadAnomalies(); loadNotices(); loadPatrol()
  }
  else if (key === 'onboarding') loadContacts()
  else if (key === 'credentials') loadCredentials()
  else if (key === 'settings') {
    loadSettings(); loadProductTypes(); loadContacts(); loadSmtp(); loadAudit(); loadFeedback()
  }
}

// 从看板等入口跳到资源调度并指定子标签
function goScheduling(tab) {
  schedulingTab.value = tab
  switchPage('scheduling')
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
  if (me.value) {
    loadDashboard()
    loadVersionInfo()
    loadHealthInfo()
  }
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

// ---- 发接入凭据（两步式：生成预览 -> 确认发送） ----
function addManualHolder() {
  const name = (holderDraft.value.name || '').trim()
  if (!name) { ElMessage.warning('请填写姓名'); return }
  const note = (holderDraft.value.note || '').trim()
  sendForm.value.manual_holders.push(note ? { name, note } : { name })
  holderDraft.value = { name: '', note: '' }
}
function removeManualHolder(idx) {
  sendForm.value.manual_holders.splice(idx, 1)
}
async function generateCredentials() {
  if (generating.value) return
  generating.value = true
  sendResults.value = []
  try {
    preview.value = await api('/api/credentials/batch-generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...sendForm.value, idempotency_key: idempotencyKey.value }),
    })
    previewVisible.value = true
  } catch (e) { ElMessage.error(e.message) } finally { generating.value = false }
}

async function confirmSend() {
  if (!preview.value || sending.value) return
  sending.value = true
  try {
    const r = await api('/api/credentials/batch-send', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ batch_id: preview.value.batch_id }),
    })
    sendResults.value = r.results || []
    const ok = sendResults.value.filter(x => x.sent).length
    const failed = sendResults.value.length - ok
    if (failed) ElMessage.warning('已发邮件给 ' + ok + ' 人，' + failed + ' 人发送失败')
    else ElMessage.success('已发邮件给 ' + ok + ' 人')
    previewVisible.value = false
    preview.value = null
    newIdempotencyKey()
    loadCredentials()
  } catch (e) { ElMessage.error(e.message) } finally { sending.value = false }
}

function closePreview() {
  previewVisible.value = false
  preview.value = null
  newIdempotencyKey()
}

async function copyText(text, okMsg) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success(okMsg || '已拷贝')
  } catch (e) { ElMessage.error('拷贝失败：' + e.message) }
}

function copyToken(token) { copyText(token, '已拷贝 token') }
function copyMcpConfig() {
  if (!preview.value) return
  copyText(preview.value.mcp_config_text, '已拷贝 MCP 配置 JSON')
}

// ---- 凭据 / 机型 ----
async function revokeCredential(id) {
  try {
    const { value: reason } = await ElMessageBox.prompt('请填写吊销原因（将强制回收其名下设备）', '吊销凭据', {
      confirmButtonText: '吊销并回收', cancelButtonText: '取消',
      inputValue: '管理员吊销', inputValidator: (v) => !!(v && v.trim()) || '原因不能为空',
    })
    await api('/api/credentials/' + id + '/revoke', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: reason.trim() }),
    })
    ElMessage.success('已吊销并强制回收其活跃租约'); loadCredentials(); loadLeases()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message || '操作失败') }
}
async function restoreCredential(id) {
  try {
    await ElMessageBox.confirm('确认恢复该凭据？恢复后即可重新用于 MCP/Web 登录。', '恢复凭据', {
      confirmButtonText: '恢复', cancelButtonText: '取消', type: 'warning',
    })
    await api('/api/credentials/' + id + '/restore', { method: 'POST' })
    ElMessage.success('已恢复'); loadCredentials()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message || '操作失败') }
}
async function deleteCredential(id) {
  try {
    await ElMessageBox.confirm('确认硬删除该凭据？此操作不可恢复；若仍有租约引用将被拒绝。', '删除凭据', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'error',
    })
    await api('/api/credentials/' + id, { method: 'DELETE' })
    ElMessage.success('已删除'); loadCredentials()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message || '操作失败') }
}
async function forceDeleteCredential(id) {
  try {
    const { value: confirmText } = await ElMessageBox.prompt(
      '强制删除将永久清除该凭据及其全部关联租约、排队记录，且不可恢复。如确认请输入“删除”。',
      '管理员强制删除', {
        confirmButtonText: '强制删除', cancelButtonText: '取消', type: 'error',
        inputValidator: (v) => v === '删除' || '请输入“删除”二字确认',
      })
    await api('/api/credentials/' + id + '?force=true', { method: 'DELETE' })
    ElMessage.success('已强制删除并清理关联数据'); loadCredentials()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message || '操作失败') }
}
async function forceReleaseLease(leaseId) {
  try {
    const { value: reason } = await ElMessageBox.prompt('请填写强制回收原因', '强制回收租约', {
      confirmButtonText: '强制回收', cancelButtonText: '取消',
      inputValue: '管理员强制回收', inputValidator: (v) => !!(v && v.trim()) || '原因不能为空',
    })
    await api('/api/leases/' + encodeURIComponent(leaseId) + '/force-release?reason=' + encodeURIComponent(reason.trim()), { method: 'POST' })
    ElMessage.success('已强制回收'); loadLeases(); loadDevices()
  } catch (e) { if (e !== 'cancel') ElMessage.error(e.message || '操作失败') }
}
async function renewCredential(id) {
  await api('/api/credentials/renew', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ credential_id: id, days: 90 }),
  })
  ElMessage.success('已续期 90 天'); loadCredentials()
}
async function disableProductType(code) {
  try {
    await api('/api/product-types/' + encodeURIComponent(code) + '/disable', { method: 'POST' })
    ElMessage.success('已停用 ' + code)
    loadProductTypes(); loadSettings()
  } catch (e) { ElMessage.error(e.message) }
}
async function deleteProductType(code) {
  try {
    await ElMessageBox.confirm(
      `确定删除机型「${code}」？此操作不可恢复。若仍有设备/活动租约/排队引用将拒绝删除。`,
      '删除设备类型',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch (e) { return }
  try {
    await api('/api/product-types/' + encodeURIComponent(code), { method: 'DELETE' })
    ElMessage.success('已删除 ' + code)
    loadProductTypes(); loadSettings()
  } catch (e) {
    const msg = e.message || String(e)
    // 409 时把后端 detail 原样提示（api() 已拼进 Error.message）
    ElMessageBox.alert(msg.replace(/^.* -> /, ''), '无法删除', { type: 'error' })
  }
}
async function enableProductType(code) {
  try {
    await api('/api/product-types/' + encodeURIComponent(code), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: true }),
    })
    ElMessage.success('已启用 ' + code)
    loadProductTypes(); loadSettings()
  } catch (e) { ElMessage.error(e.message) }
}
const enabledProductTypes = computed(() => productTypes.value.filter(p => p.enabled))
const disabledProductTypes = computed(() => productTypes.value.filter(p => !p.enabled))
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

// ---- v0.16：版本信息在挂载时加载，供设置页「关于」展示 ----
async function loadVersionInfo() {
  try { versionInfo.value = await api('/api/version') } catch (e) { versionInfo.value = null }
}
async function loadHealthInfo() {
  try { healthInfo.value = await api('/api/health') } catch (e) { healthInfo.value = null }
}

// ---- v0.15：新增机型（先 POST 建基础记录，再 PUT 补工程字段）----
function resetPtCreate() {
  ptCreateRow.value = {
    type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1,
    defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', jenkins_url: '', notes: '',
  }
  ptAllowIncomplete.value = false
  ptCreateForm.value?.clearValidate?.()
}
function openPtCreate() {
  resetPtCreate()
  ptCreateDialog.value = true
}
async function submitPtCreate() {
  if (!ptCreateForm.value) return
  try {
    await ptCreateForm.value.validate()
  } catch (e) {
    ElMessage.warning('请先补齐必填字段，或勾选「允许不完整（仅测试）」')
    return
  }
  const row = ptCreateRow.value
  const code = row.type_code.trim()
  ptCreateSaving.value = true
  try {
    await api('/api/product-types', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type_code: code, display_name: row.display_name,
        has_screen: row.has_screen, has_led: row.has_led, serial_count: row.serial_count,
      }),
    })
    await api('/api/product-types/' + encodeURIComponent(code), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        defconfig: row.defconfig, sdk_repo_cmd: row.sdk_repo_cmd, build_cmd: row.build_cmd,
        image_path: row.image_path, jenkins_url: row.jenkins_url, notes: row.notes,
      }),
    })
    ElMessage.success('已新增机型 ' + code)
    ptCreateDialog.value = false
    loadProductTypes(); loadSettings()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    ptCreateSaving.value = false
  }
}
function offerCountdown(row) {
  if (row.state !== 'OFFERED' || row.offer_remaining_seconds == null) return '-'
  const s = row.offer_remaining_seconds
  return s > 0 ? `${s} 秒后过期` : '已过期'
}
const statusTag = {
  available: 'success', occupied: 'warning', pending: 'info',
  pending_removal: 'warning', retired: 'info', offline: 'danger', maintenance: 'danger',
}
const legendHealth = [
  { level: 'L3', meaning: '健康，默认可申请' },
  { level: 'L2', meaning: '可疑，最近巡检有波动，默认不分配' },
  { level: 'L1', meaning: '降级，多次探测失败' },
  { level: 'L0', meaning: '离线 / 完全不可达' },
]
const legendDevice = [
  { state: 'available', meaning: '空闲，可申请' },
  { state: 'occupied', meaning: '已被租用' },
  { state: 'pending', meaning: '注册中' },
  { state: 'offline', meaning: '离线' },
]
const legendLease = [
  { state: 'GRANTED', meaning: '在用' },
  { state: 'EXPIRING', meaning: '宽限期，即将到期' },
  { state: 'RESERVED', meaning: '排到 offer 窗口，待确认' },
]
const legendQueue = [
  { state: 'QUEUED', meaning: '排队中' },
  { state: 'OFFERED', meaning: '已发 offer，60 秒内确认' },
  { state: 'GRANTED', meaning: '已分配' },
  { state: 'CANCELLED', meaning: '已取消' },
]
const legendRole = [
  { role: 'admin', meaning: '全量查看 + 强制回收 + 发凭据 + 机型管理' },
  { role: 'user', meaning: '只看自己的租约与申请' },
]
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
      <div style="padding: 16px; font-weight: 700">
        <span>Quamm TRES</span>
        <span class="title-version">v{{ versionInfo ? versionInfo.version : '—' }}</span>
      </div>
      <el-menu :default-active="active" @select="switchPage" style="flex: 1">
        <el-menu-item v-for="p in pages.filter(x => !x.admin || me.role === 'admin')" :key="p.key" :index="p.key">{{ p.label }}</el-menu-item>
      </el-menu>
      <div class="user-box">
        <div style="font-weight: 600">{{ me.name || me.email }}</div>
        <div style="font-size: 12px; color: #909399; word-break: break-all">{{ me.email }}</div>
        <el-tag size="small" :type="me.role === 'admin' ? 'danger' : 'info'" style="margin-top: 6px">{{ me.role }}</el-tag>
        <el-button size="small" style="margin-top: 8px; width: 100%" @click="doLogout">退出登录</el-button>
      </div>
      <div class="version-foot">v{{ versionInfo ? versionInfo.version : '—' }}</div>
    </el-aside>

    <el-main>
      <!-- 看板 -->
      <div v-if="active === 'dashboard'">
        <h2>看板</h2>
        <el-row :gutter="12" v-if="dashboard">
          <el-col :span="4"><el-card class="dash-card" @click="goDevices('')"><div class="dash-label">设备总数</div><div class="dash-num">{{ dashboard.total_devices }}</div></el-card></el-col>
          <el-col :span="4"><el-card class="dash-card" @click="goDevices('available')"><div class="dash-label">可用</div><div class="dash-num" style="color: #67c23a">{{ dashboard.available }}</div></el-card></el-col>
          <el-col :span="4"><el-card class="dash-card" @click="goDevices('occupied')"><div class="dash-label">占用</div><div class="dash-num" style="color: #e6a23c">{{ dashboard.by_status.occupied || 0 }}</div></el-card></el-col>
          <el-col :span="4"><el-card class="dash-card" @click="goDevices('offline')"><div class="dash-label">离线</div><div class="dash-num" style="color: #f56c6c">{{ dashboard.by_status.offline || 0 }}</div></el-card></el-col>
          <el-col :span="4"><el-card class="dash-card" @click="goScheduling('leases')"><div class="dash-label">活跃租约</div><div class="dash-num">{{ dashboard.active_leases == null ? '-' : dashboard.active_leases }}</div></el-card></el-col>
          <el-col :span="4"><el-card class="dash-card" @click="goScheduling('queue')"><div class="dash-label">排队中</div><div class="dash-num">{{ dashboard.queued == null ? '-' : dashboard.queued }}</div></el-card></el-col>
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
        <!-- 概况 -->
        <div v-if="!selectedDeviceType">
          <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
            <h2 style="margin: 0">设备概况</h2>
            <el-button v-if="me.role === 'admin'" size="small" type="primary" @click="openDeviceAdd">添加设备</el-button>
          </div>

          <el-row :gutter="12" style="margin: 14px 0">
            <el-col :span="4" :xs="12" :sm="8" :md="4">
              <el-card shadow="hover" class="ov-card">
                <div class="ov-label">全部设备</div>
                <div class="ov-num">{{ deviceTotals.total }}</div>
              </el-card>
            </el-col>
            <el-col :span="4" :xs="12" :sm="8" :md="4">
              <el-card shadow="hover" class="ov-card">
                <div class="ov-label">可用</div>
                <div class="ov-num" style="color: #67c23a">{{ deviceTotals.available }}</div>
              </el-card>
            </el-col>
            <el-col :span="4" :xs="12" :sm="8" :md="4">
              <el-card shadow="hover" class="ov-card">
                <div class="ov-label">占用</div>
                <div class="ov-num" style="color: #e6a23c">{{ deviceTotals.occupied }}</div>
              </el-card>
            </el-col>
            <el-col :span="4" :xs="12" :sm="8" :md="4">
              <el-card shadow="hover" class="ov-card">
                <div class="ov-label">离线</div>
                <div class="ov-num" style="color: #f56c6c">{{ deviceTotals.offline }}</div>
              </el-card>
            </el-col>
            <el-col :span="4" :xs="12" :sm="8" :md="4">
              <el-card shadow="hover" class="ov-card">
                <div class="ov-label">待检</div>
                <div class="ov-num" style="color: #909399">{{ deviceTotals.pending }}</div>
              </el-card>
            </el-col>
          </el-row>

          <p style="color: #909399; margin: 12px 0 8px">共 {{ deviceOverview.length }} 个品类，点击卡片查看该品类设备明细。</p>
          <el-row :gutter="12">
            <el-col v-for="g in deviceOverview" :key="g.type" :span="8" :xs="24" :sm="12" :md="8" style="margin-bottom: 12px">
              <el-card shadow="hover" class="cat-card" @click="openDeviceType(g.type)">
                <div style="display: flex; align-items: center; justify-content: space-between">
                  <span style="font-weight: 700; font-size: 15px">{{ g.type }}</span>
                  <el-tag size="small" type="info">共 {{ g.count }}</el-tag>
                </div>
                <div style="display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap">
                  <el-tag size="small" type="success">可用 {{ g.available }}</el-tag>
                  <el-tag size="small" type="warning">占用 {{ g.occupied }}</el-tag>
                  <el-tag size="small" type="danger">离线 {{ g.offline }}</el-tag>
                  <el-tag v-if="g.pending" size="small" type="info">待检 {{ g.pending }}</el-tag>
                </div>
              </el-card>
            </el-col>
          </el-row>
          <el-empty v-if="!deviceOverview.length" description="没有已分类设备" />
        </div>

        <!-- 品类详情 -->
        <div v-else>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <el-button size="small" @click="backToOverview">返回概况</el-button>
            <h2 style="margin: 0">{{ selectedDeviceType }}</h2>
          </div>
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin: 10px 0">
            <el-tabs v-model="detailStatusTab" style="flex: 1; min-width: 280px">
              <el-tab-pane label="全部" name="all" />
              <el-tab-pane label="占用" name="occupied" />
              <el-tab-pane label="可用" name="available" />
              <el-tab-pane label="离线" name="offline" />
              <el-tab-pane label="待检" name="pending" />
            </el-tabs>
            <el-input v-model="deviceSearch" placeholder="搜索 ID / 名称 / 位置 / 责任人 / ADB" clearable style="width: 280px" />
          </div>
          <el-table :data="detailDevices" border size="small">
            <el-table-column prop="device_id" label="ID" width="140" />
            <el-table-column prop="display_name" label="名称" width="130" />
            <el-table-column prop="project_name" label="工程配置" width="150" show-overflow-tooltip />
            <el-table-column prop="position_code" label="位置" width="120" />
            <el-table-column prop="owner" label="责任人" width="120" />
            <el-table-column prop="register_agent" label="Agent" width="130" show-overflow-tooltip>
              <template #default="{ row }">{{ row.register_agent || '-' }}</template>
            </el-table-column>
            <el-table-column prop="health_level" label="健康" width="70" />
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTag[row.status] || 'info'">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="adb_address" label="ADB 地址" width="160" />
          </el-table>
          <el-empty v-if="!detailDevices.length" description="该状态下没有设备" />
        </div>
      </div>

      <!-- 资源调度：占用 / 排队 / 异常 -->
      <div v-else-if="active === 'scheduling'">
        <h2>资源调度</h2>
        <el-tabs v-model="schedulingTab">
          <el-tab-pane label="占用" name="leases">
        <el-table :data="leases" border>
          <el-table-column prop="lease_id" label="租约" width="140" />
          <el-table-column prop="display_name" label="设备" />
          <el-table-column prop="email" label="申请人邮箱" width="200" />
          <el-table-column prop="name" label="申请人姓名" width="140" />
          <el-table-column prop="purpose" label="用途" />
          <el-table-column prop="agent_label" label="Agent" width="140" />
          <el-table-column prop="expires_at" label="到期" width="200" />
          <el-table-column prop="state" label="状态" width="100" />
          <el-table-column v-if="me.role === 'admin'" label="操作" width="120" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="danger" @click="forceReleaseLease(row.lease_id)">强制回收</el-button>
            </template>
          </el-table-column>
        </el-table>
          </el-tab-pane>

          <el-tab-pane label="排队" name="queue">
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
          </el-tab-pane>

          <el-tab-pane label="异常/待修复" name="anomaly">
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
          </el-tab-pane>
        </el-tabs>
      </div>

      <!-- 接入申请（发接入凭据，管理员） -->
      <div v-else-if="active === 'onboarding'">
        <h2>接入申请</h2>
        <p style="color:#909399">为新接入方发放 rr_ 凭据：先预览，再决定是否发邮件；邮件正文含 MCP 配置与接入说明。</p>
        <el-form label-width="100px" style="max-width: 640px">
          <el-form-item label="从通讯录选">
            <el-select v-model="sendForm.emails" multiple filterable placeholder="可多选" style="width: 100%">
              <el-option v-for="c in contacts" :key="c.email" :label="(c.name || c.email) + ' <' + c.email + '>'" :value="c.email" />
            </el-select>
          </el-form-item>
          <el-form-item label="或手填邮箱">
            <el-input v-model="sendForm.manual_emails" type="textarea" :rows="2" placeholder="多个邮箱用逗号 / 换行分隔" />
          </el-form-item>
          <el-form-item label="仅姓名（不发邮件）">
            <div style="width: 100%">
              <div style="display: flex; gap: 8px; margin-bottom: 8px">
                <el-input v-model="holderDraft.name" placeholder="姓名" style="width: 160px" />
                <el-input v-model="holderDraft.note" placeholder="备注（可选）" style="flex: 1" />
                <el-button @click="addManualHolder">添加</el-button>
              </div>
              <el-tag
                v-for="(h, idx) in sendForm.manual_holders"
                :key="idx"
                closable
                style="margin: 0 6px 6px 0"
                @close="removeManualHolder(idx)"
              >{{ h.name }}{{ h.note ? '（' + h.note + '）' : '' }}</el-tag>
              <div v-if="!sendForm.manual_holders.length" style="color: #909399; font-size: 12px">可添加无邮箱条目：只生成 token，确认发送时跳过发信</div>
            </div>
          </el-form-item>
          <el-form-item label="权限">
            <el-select v-model="sendForm.role" style="width: 160px">
              <el-option label="user（普通）" value="user" />
              <el-option label="admin（管理员）" value="admin" />
            </el-select>
          </el-form-item>
          <el-form-item label="备注"><el-input v-model="sendForm.note" placeholder="可选，写入凭据备注与邮件" /></el-form-item>
          <el-form-item label="有效期">
            <el-input-number v-model="sendForm.valid_days" :min="1" :max="3650" /> <span style="margin-left: 8px">天</span>
          </el-form-item>
          <el-form-item><el-button type="primary" :loading="generating" @click="generateCredentials">生成凭据（预览）</el-button></el-form-item>
        </el-form>
        <el-table v-if="sendResults.length" :data="sendResults" border style="margin-top: 12px">
          <el-table-column prop="email" label="邮箱" width="240" />
          <el-table-column prop="role" label="权限" width="80" />
          <el-table-column label="token" width="220"><template #default="{ row }"><span class="mono">{{ row.token }}</span></template></el-table-column>
          <el-table-column label="发送结果" width="100"><template #default="{ row }"><el-tag :type="row.sent ? 'success' : 'danger'">{{ row.sent ? '成功' : '失败' }}</el-tag></template></el-table-column>
          <el-table-column prop="error" label="错误" />
        </el-table>
      </div>

      <!-- 凭据台账 -->
      <div v-else-if="active === 'credentials'">
        <h2>凭据</h2>
        <el-tabs v-model="credentialTab">
          <el-tab-pane label="有效凭据" name="active">
            <el-table :data="credentials.filter((c) => !c.revoked)" border>
              <el-table-column prop="email" label="邮箱" width="200" />
              <el-table-column prop="name" label="姓名" width="120" />
              <el-table-column prop="role" label="权限" width="80" />
              <el-table-column prop="token_prefix" label="token 前缀" width="140" />
              <el-table-column prop="agent_label" label="Agent" width="140" />
              <el-table-column prop="note" label="备注" width="140" />
              <el-table-column prop="expires_at" label="到期时间" width="200" />
              <el-table-column prop="created_at" label="创建时间" width="200" />
              <el-table-column label="操作" width="180" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" @click="renewCredential(row.id)">续期</el-button>
                  <el-button size="small" type="danger" @click="revokeCredential(row.id)">吊销</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="'已吊销 (' + credentials.filter((c) => c.revoked).length + ')'" name="revoked">
            <el-table :data="credentials.filter((c) => c.revoked)" border>
              <el-table-column prop="email" label="邮箱" width="200" />
              <el-table-column prop="name" label="姓名" width="120" />
              <el-table-column prop="role" label="权限" width="80" />
              <el-table-column prop="token_prefix" label="token 前缀" width="140" />
              <el-table-column prop="agent_label" label="Agent" width="140" />
              <el-table-column prop="note" label="备注" width="140" />
              <el-table-column prop="expires_at" label="到期时间" width="200" />
              <el-table-column label="操作" width="300" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" type="warning" @click="restoreCredential(row.id)">恢复</el-button>
                  <el-button size="small" type="danger" @click="deleteCredential(row.id)">删除</el-button>
                  <el-button size="small" type="danger" plain @click="forceDeleteCredential(row.id)">强制删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </div>

      <!-- 设置：系统 + 设备类型 + 通讯录 + SMTP + 介绍 + 审计 -->
      <div v-else-if="active === 'settings'">
        <h2>设置</h2>
        <el-tabs v-model="settingsTab">
          <el-tab-pane label="系统设置" name="system">
            <el-form v-if="settings" :model="settingsForm" label-width="200px" style="max-width: 640px; margin-bottom: 16px">
              <el-form-item label="位置规则">
                <span>{{ settings.location_rule.format }}（{{ settings.location_rule.description }}）</span>
              </el-form-item>
              <el-form-item label="探测后端"><span>{{ settings.probe_backend }}</span></el-form-item>
              <el-form-item label="巡检间隔（分钟）">
                <el-input-number v-model="settingsForm.patrol_interval_minutes" :min="1" :max="1440" />
              </el-form-item>
              <el-form-item label="巡检失败阈值（轮）">
                <el-input-number v-model="settingsForm.patrol_fail_threshold" :min="1" :max="100" />
              </el-form-item>
              <el-form-item label="单人并发上限（台）">
                <el-input-number v-model="settingsForm.holder_concurrency_limit" :min="1" :max="20" />
              </el-form-item>
              <el-form-item label="租约 TTL（分钟）">
                <el-input-number v-model="settingsForm.lease_ttl_minutes" :min="1" :max="1440" />
              </el-form-item>
              <el-form-item label="宽限期（分钟）">
                <el-input-number v-model="settingsForm.lease_grace_minutes" :min="0" :max="1440" />
              </el-form-item>
              <el-form-item label="租约上限（分钟）">
                <el-input-number v-model="settingsForm.lease_max_minutes" :min="1" :max="10080" />
              </el-form-item>
              <el-form-item label="offer 窗口（秒）">
                <el-input-number v-model="settingsForm.offer_window_seconds" :min="5" :max="3600" />
              </el-form-item>
              <el-form-item label="要求心跳">
                <el-switch v-model="settingsForm.heartbeat_required" />
              </el-form-item>
              <el-form-item label="心跳间隔（分钟）">
                <el-input-number v-model="settingsForm.heartbeat_interval_minutes" :min="1" :max="1440" />
              </el-form-item>
              <el-form-item label="心跳超时宽限（分钟）">
                <el-input-number v-model="settingsForm.heartbeat_miss_grace_minutes" :min="1" :max="1440" />
              </el-form-item>
              <el-form-item label="凭据有效期（天）">
                <el-input-number v-model="settingsForm.credential_valid_days" :min="1" :max="3650" />
              </el-form-item>
              <el-form-item label="allow_apply_when_suspect">
                <el-switch v-model="settingsForm.allow_apply_when_suspect" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="settingsSaving" @click="saveSettings">保存</el-button>
              </el-form-item>
            </el-form>
            <h3>关于 Quamm TRES</h3>
            <p v-if="versionInfo" style="margin: 0 0 12px">
              <strong>{{ versionInfo.name }}</strong> · 版本 {{ versionInfo.version }}
              <span v-if="versionInfo.stage">（{{ versionInfo.stage }}）</span>
            </p>
            <p v-if="healthInfo" style="margin: 0 0 12px; color: #606266">
              启动时间：{{ healthInfo.started_at }}<br />
              已运行：{{ healthInfo.uptime_human }}
            </p>
            <p style="color: #909399; margin: 0 0 12px">
              测试资源分配管理系统：Agent 经 MCP 申请/释放 ADB 测试设备，管理员在此维护系统参数与设备类型。
              机型的新增、编辑、停用与删除统一在「设备类型」页操作，本页不再重复入口。
            </p>
            <h4>健康级别 health_level</h4>
            <el-table :data="legendHealth" border size="small">
              <el-table-column prop="level" label="级别" width="80" />
              <el-table-column prop="meaning" label="含义" />
            </el-table>
            <h4>设备状态</h4>
            <el-table :data="legendDevice" border size="small">
              <el-table-column prop="state" label="状态" width="120" />
              <el-table-column prop="meaning" label="含义" />
            </el-table>
            <h4>租约状态</h4>
            <el-table :data="legendLease" border size="small">
              <el-table-column prop="state" label="状态" width="120" />
              <el-table-column prop="meaning" label="含义" />
            </el-table>
            <h4>排队状态</h4>
            <el-table :data="legendQueue" border size="small">
              <el-table-column prop="state" label="状态" width="120" />
              <el-table-column prop="meaning" label="含义" />
            </el-table>
            <h4>角色</h4>
            <el-table :data="legendRole" border size="small">
              <el-table-column prop="role" label="角色" width="100" />
              <el-table-column prop="meaning" label="含义" />
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="设备类型" name="product-types">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px">
              <h3 style="margin: 0">设备类型元数据（defconfig / SDK 获取 / 编译 / 镜像 / Jenkins / 备注）</h3>
              <el-button v-if="me.role === 'admin'" type="primary" @click="openPtCreate">新增机型</el-button>
            </div>
            <p style="color: #909399">新增机型时 5 个工程字段默认必填；勾选「允许不完整（仅测试）」可放开。无引用时可物理删除（需二次确认）。</p>
            <el-table :data="enabledProductTypes" border>
              <el-table-column prop="type_code" label="type_code" width="150" />
              <el-table-column prop="display_name" label="名称" width="140" />
              <el-table-column prop="defconfig" label="defconfig" width="200" show-overflow-tooltip />
              <el-table-column label="完整度" width="110">
                <template #default="{ row }"><el-tag :type="row.complete ? 'success' : 'warning'">{{ row.complete_label }}</el-tag></template>
              </el-table-column>
              <el-table-column label="操作" width="260">
                <template #default="{ row }">
                  <template v-if="me.role === 'admin'">
                    <el-button size="small" @click="openPtEdit(row)">编辑</el-button>
                    <el-button size="small" type="danger" @click="disableProductType(row.type_code)">停用</el-button>
                    <el-button size="small" type="danger" plain @click="deleteProductType(row.type_code)">删除</el-button>
                  </template>
                  <span v-else style="color: #c0c4cc; font-size: 12px">仅 admin 可改</span>
                </template>
              </el-table-column>
            </el-table>
            <p v-if="!enabledProductTypes.length" style="color: #909399">暂无启用中的设备类型。</p>

            <el-collapse style="margin-top: 16px">
              <el-collapse-item :title="'已停用的类型 (' + disabledProductTypes.length + ')'" name="disabled">
                <el-table :data="disabledProductTypes" border>
                  <el-table-column prop="type_code" label="type_code" width="150" />
                  <el-table-column prop="display_name" label="名称" width="140" />
                  <el-table-column prop="defconfig" label="defconfig" width="200" show-overflow-tooltip />
                  <el-table-column label="状态" width="90">
                    <template #default><el-tag type="info">停用</el-tag></template>
                  </el-table-column>
                  <el-table-column label="操作" width="200">
                    <template #default="{ row }">
                      <template v-if="me.role === 'admin'">
                        <el-button size="small" type="success" @click="enableProductType(row.type_code)">启用</el-button>
                        <el-button size="small" type="danger" plain @click="deleteProductType(row.type_code)">删除</el-button>
                      </template>
                      <span v-else style="color: #c0c4cc; font-size: 12px">仅 admin 可改</span>
                    </template>
                  </el-table-column>
                </el-table>
                <p v-if="!disabledProductTypes.length" style="color: #909399">没有已停用的类型。</p>
              </el-collapse-item>
            </el-collapse>
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
          <el-tab-pane label="系统介绍" name="intro">
            <iframe src="intro.html" title="系统介绍"
              style="width:100%;height:78vh;border:1px solid var(--el-border-color);border-radius:10px;"></iframe>
          </el-tab-pane>
          <el-tab-pane v-if="me.role === 'admin'" label="审计" name="audit">
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
          </el-tab-pane>

          <el-tab-pane v-if="me.role === 'admin'" label="Agent 意见" name="feedback">
            <div style="margin-bottom: 12px">
              <el-select v-model="feedbackStatus" placeholder="按状态筛选（全部）" clearable @change="loadFeedback" style="width: 220px">
                <el-option label="待处理" value="pending" />
                <el-option label="已采纳" value="accepted" />
                <el-option label="暂缓" value="deferred" />
                <el-option label="已驳回" value="rejected" />
              </el-select>
            </div>
            <el-table :data="feedbackList" border>
              <el-table-column prop="created_at" label="时间" width="170" />
              <el-table-column label="类别" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.category === 'bug' ? 'danger' : (row.category === 'praise' ? 'success' : 'info')">{{ row.category }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="agent_label" label="Agent" width="150">
                <template #default="{ row }">{{ row.agent_label || '-' }}</template>
              </el-table-column>
              <el-table-column label="意见 / 评审备注">
                <template #default="{ row }">
                  <div>{{ row.content }}</div>
                  <div v-if="row.admin_note" style="color: #909399; font-size: 12px; margin-top: 4px">评审：{{ row.admin_note }}</div>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag v-if="row.status === 'pending'" size="small" type="warning">待处理</el-tag>
                  <el-tag v-else-if="row.status === 'accepted'" size="small" type="success">已采纳</el-tag>
                  <el-tag v-else-if="row.status === 'deferred'" size="small" type="info">暂缓</el-tag>
                  <el-tag v-else size="small" type="danger">已驳回</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="200">
                <template #default="{ row }">
                  <el-button size="small" link type="success" @click="reviewFeedback(row, 'accepted')">采纳</el-button>
                  <el-button size="small" link type="info" @click="reviewFeedback(row, 'deferred')">暂缓</el-button>
                  <el-button size="small" link type="danger" @click="reviewFeedback(row, 'rejected')">驳回</el-button>
                </template>
              </el-table-column>
            </el-table>
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

        <el-dialog v-model="ptCreateDialog" title="新增机型" width="720px" :close-on-click-modal="false">
          <el-form ref="ptCreateForm" :model="ptCreateRow" :rules="ptCreateRules" label-width="120px">
            <el-form-item label="type_code" prop="type_code"><el-input v-model="ptCreateRow.type_code" placeholder="如 moto_ld" /></el-form-item>
            <el-form-item label="名称"><el-input v-model="ptCreateRow.display_name" placeholder="展示名，留空则用 type_code" /></el-form-item>
            <el-form-item label="有屏"><el-switch v-model="ptCreateRow.has_screen" /></el-form-item>
            <el-form-item label="有灯"><el-switch v-model="ptCreateRow.has_led" /></el-form-item>
            <el-form-item label="串口数"><el-input-number v-model="ptCreateRow.serial_count" :min="0" /></el-form-item>
            <el-form-item label="defconfig" prop="defconfig"><el-input v-model="ptCreateRow.defconfig" placeholder="project_moto_ld_defconfig" /></el-form-item>
            <el-form-item label="SDK 获取命令" prop="sdk_repo_cmd"><el-input v-model="ptCreateRow.sdk_repo_cmd" type="textarea" :rows="2" placeholder="repo init -u http://.../manifest; repo sync -j8" /></el-form-item>
            <el-form-item label="编译命令" prop="build_cmd"><el-input v-model="ptCreateRow.build_cmd" type="textarea" :rows="2" placeholder="make distclean && make xxx_defconfig && make xos -j8" /></el-form-item>
            <el-form-item label="镜像路径" prop="image_path"><el-input v-model="ptCreateRow.image_path" placeholder="out/qm10xd_linux/qmimages/" /></el-form-item>
            <el-form-item label="Jenkins 链接" prop="jenkins_url"><el-input v-model="ptCreateRow.jenkins_url" placeholder="http://.../Daily/XOS/moto_ld_102d/" /></el-form-item>
            <el-form-item label="备注"><el-input v-model="ptCreateRow.notes" type="textarea" :rows="2" placeholder="其他备注" /></el-form-item>
            <el-form-item>
              <el-checkbox v-model="ptAllowIncomplete">允许不完整（仅测试）</el-checkbox>
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="ptCreateDialog = false">取消</el-button>
            <el-button type="primary" :loading="ptCreateSaving" @click="submitPtCreate">新增</el-button>
          </template>
        </el-dialog>
      </div>

      <!-- 全局弹窗：手工添加设备（两步：探测 -> 注册） -->
      <el-dialog v-model="deviceAddVisible" title="添加设备" width="680px" :close-on-click-modal="false">
        <el-steps :active="deviceAddStep - 1" align-center finish-status="success" style="margin-bottom: 16px">
          <el-step title="探测设备" />
          <el-step title="确认信息" />
        </el-steps>

        <template v-if="deviceAddStep === 1">
          <el-radio-group v-model="deviceAddMode" style="margin-bottom: 14px">
            <el-radio-button value="ip">IP 直连</el-radio-button>
            <el-radio-button value="discover">设备发现</el-radio-button>
          </el-radio-group>

          <el-form v-if="deviceAddMode === 'ip'" label-width="100px">
            <el-form-item label="设备 IP" required>
              <el-input v-model="deviceAddForm.ip" placeholder="如 172.16.60.16" />
            </el-form-item>
            <el-form-item label="ADB 端口">
              <el-input-number v-model="deviceAddForm.port" :min="1" :max="65535" />
            </el-form-item>
            <el-form-item label="机型提示">
              <el-select v-model="deviceAddForm.hint_model" placeholder="可选，留空以探测为准" clearable style="width: 100%">
                <el-option v-for="t in productTypes" :key="t.type_code" :label="t.type_code" :value="t.type_code" />
              </el-select>
            </el-form-item>
          </el-form>

          <div v-else>
            <div style="display: flex; gap: 8px; margin-bottom: 10px">
              <el-input
                v-model="discoveryNetworks"
                placeholder="网段，逗号分隔，如 172.16.60.0/24；留空自动探测本机网段"
              />
              <el-button type="primary" :loading="discovering" @click="runDiscovery">扫描</el-button>
            </div>
            <el-table :data="discoveryResults" border size="small" max-height="320">
              <el-table-column prop="ip" label="IP" width="140" />
              <el-table-column label="机型" width="130">
                <template #default="{ row }">{{ row.probe?.model || '-' }}</template>
              </el-table-column>
              <el-table-column label="MAC">
                <template #default="{ row }">{{ row.mac || row.probe?.mac || '-' }}</template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">
                  <el-tag v-if="row.already_registered" size="small" type="info">已登记</el-tag>
                  <el-tag v-else size="small" type="success">新设备</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="90">
                <template #default="{ row }">
                  <el-button size="small" link type="primary" @click="pickDiscovered(row)">选择</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>

        <template v-else>
          <el-descriptions v-if="deviceAddProbe" :column="2" border size="small" style="margin-bottom: 12px">
            <el-descriptions-item label="健康等级">{{ deviceAddProbe.probe?.level }}</el-descriptions-item>
            <el-descriptions-item label="MAC">{{ deviceAddProbe.probe?.mac || '-' }}</el-descriptions-item>
            <el-descriptions-item label="SN">{{ deviceAddProbe.probe?.sn || '-' }}</el-descriptions-item>
            <el-descriptions-item label="探测机型">{{ deviceAddProbe.probe?.model || '-' }}</el-descriptions-item>
          </el-descriptions>
          <el-alert
            v-if="deviceAddProbe?.identity_match?.result === 'existing'"
            type="warning" :closable="false" style="margin-bottom: 12px"
            :title="'识别为已有设备 ' + deviceAddProbe.identity_match.matched_device_id + '，注册将更新其信息而非新建'"
          />
          <el-form label-width="100px">
            <el-form-item label="位置" required>
              <el-input v-model="deviceAddConfirm.position_code" placeholder="工位/会议室/机架机位，如 302-R2-05" />
            </el-form-item>
            <el-form-item label="负责人" required>
              <el-input v-model="deviceAddConfirm.owner" placeholder="维护责任人" />
            </el-form-item>
            <el-form-item label="机型">
              <el-select v-model="deviceAddConfirm.model_class" placeholder="选择机型" clearable style="width: 100%">
                <el-option v-for="t in productTypes" :key="t.type_code" :label="t.type_code" :value="t.type_code" />
              </el-select>
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="deviceAddConfirm.note" type="textarea" :rows="2" />
            </el-form-item>
          </el-form>
        </template>

        <template #footer>
          <el-button @click="deviceAddVisible = false">取消</el-button>
          <el-button v-if="deviceAddStep === 2" @click="deviceAddStep = 1">上一步</el-button>
          <el-button v-if="deviceAddStep === 1 && deviceAddMode === 'ip'" type="primary" :loading="deviceAddProbing" @click="submitDeviceProbe">探测</el-button>
          <el-button v-else-if="deviceAddStep === 2" type="primary" :loading="deviceAddSaving" @click="submitDeviceRegister">确认注册</el-button>
        </template>
      </el-dialog>

      <!-- 全局弹窗：凭据预览（接入申请页与设置页都可能触发，故放在页面条件之外） -->
      <el-dialog v-model="previewVisible" title="凭据预览（尚未发送）" width="920px" :close-on-click-modal="false" @closed="closePreview">
        <template v-if="preview">
          <div style="margin-bottom: 12px">
            <el-button type="primary" plain @click="copyMcpConfig">拷贝 MCP 配置 JSON</el-button>
            <span style="margin-left: 12px; color: #909399">共 {{ preview.count }} 人 · 有效期 {{ preview.valid_days }} 天 · 批次 {{ preview.batch_id }}（30 分钟内可发送）</span>
          </div>
          <el-table :data="preview.results" border max-height="320">
            <el-table-column prop="name" label="姓名" width="120" />
            <el-table-column label="邮箱" width="220">
              <template #default="{ row }">
                <span v-if="row.has_email !== false && row.email">{{ row.email }}</span>
                <el-tag v-else type="info" size="small">仅生成，不发邮件</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="role" label="权限" width="80" />
            <el-table-column label="token">
              <template #default="{ row }"><span class="mono">{{ row.token }}</span></template>
            </el-table-column>
            <el-table-column label="操作" width="110">
              <template #default="{ row }"><el-button size="small" @click="copyToken(row.token)">拷贝 token</el-button></template>
            </el-table-column>
          </el-table>

          <h3 style="margin-top: 16px">邮件正文预览</h3>
          <p style="margin: 4px 0"><strong>主题：</strong>{{ preview.results[0].subject }}</p>
          <pre class="mail-preview">{{ preview.results[0].body }}</pre>
        </template>
        <template #footer>
          <el-button @click="closePreview">仅生成不发邮件</el-button>
          <el-button type="primary" :loading="sending" @click="confirmSend">确认发送邮件</el-button>
        </template>
      </el-dialog>
    </el-main>
  </el-container>
</template>

<style>
.auth-screen { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: #f5f7fa; }
.auth-card { width: 420px; }
.auth-card h2 { margin-top: 0; }
.err { color: #f56c6c; font-size: 13px; }
.user-box { padding: 12px 16px; border-top: 1px solid #eee; }
.mono { font-family: Consolas, Monaco, "Courier New", monospace; font-size: 12px; word-break: break-all; }
.mail-preview { background: #f5f7fa; border: 1px solid #e4e7ed; border-radius: 4px; padding: 12px; max-height: 260px; overflow: auto; font-family: Consolas, Monaco, "Courier New", monospace; font-size: 12px; white-space: pre-wrap; }
.dash-card { cursor: pointer; transition: box-shadow .2s, transform .2s; }
.dash-card:hover { box-shadow: 0 4px 14px rgba(0,0,0,.12); transform: translateY(-2px); }
.dash-card :deep(.el-card__body) { height: 96px; box-sizing: border-box; display: flex; flex-direction: column; justify-content: space-between; }
.dash-label { color: #606266; font-size: 14px; }
.dash-num { font-size: 28px; line-height: 1.2; }
.version-foot { padding: 8px 16px; font-size: 11px; color: #b0b3b8; text-align: center; user-select: none; }
.title-version { margin-left: 6px; font-size: 11px; font-weight: 400; color: #909399; }
.ov-card :deep(.el-card__body) { padding: 16px; }
.ov-label { color: #606266; font-size: 14px; }
.ov-num { font-size: 30px; line-height: 1.2; margin-top: 6px; font-weight: 600; }
.cat-card { cursor: pointer; transition: box-shadow .2s, transform .2s; }
.cat-card:hover { box-shadow: 0 6px 18px rgba(64, 158, 255, .18); transform: translateY(-2px); }
</style>
