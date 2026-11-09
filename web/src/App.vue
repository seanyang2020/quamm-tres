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
// v0.23 截图预览 / v0.24 截图生命周期（元数据、重新截图、历史、存储管理）
const screenshotVisible = ref(false)
const screenshotUrl = ref('')
const screenshotMeta = ref(null)
const screenshotBusy = ref('')
const screenshotRotate = ref(0)            // 重新截图所选旋转 0/90/180/270
const screenshotDeviceId = ref('')         // 当前预览/重新截图对应的设备
const retaking = ref(false)
const screenshotHistoryVisible = ref(false)
const screenshotHistoryDevice = ref('')
const screenshotHistory = ref([])
const screenshotHistoryLoading = ref(false)
const screenshotStats = ref(null)
const screenshotStatsLoading = ref(false)
const rotateOptions = [0, 90, 180, 270]
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
  screenshot_max_per_device: 10,
  screenshot_retention_days: 14,
  screenshot_total_max: 500,
  screenshot_cleanup_on_release: true,
  pending_auto_recover_enabled: true,
  pending_knock_eventcode: '2223',
  pending_knock_timeout: 30,
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
const deviceAddConfirm = ref({ position_code: '', owner: '', model_class: '', note: '', share_scope: 'shared', allowed_emails: [], allowed_agent_labels: [] })
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
  deviceAddConfirm.value = { position_code: '', owner: '', model_class: '', note: '', share_scope: 'shared', allowed_emails: [], allowed_agent_labels: [] }
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
        share_scope: deviceAddConfirm.value.share_scope,
        allowed_emails: deviceAddConfirm.value.allowed_emails,
        allowed_agent_labels: deviceAddConfirm.value.allowed_agent_labels,
      }),
    })
    ElMessage.success('设备已保存：' + (r.device_id || r.device?.device_id || ''))
    deviceAddVisible.value = false
    loadDevices()
  } catch (e) { ElMessage.error(e.message) } finally { deviceAddSaving.value = false }
}

// v0.26 设备编辑与共享
const deviceEditVisible = ref(false)
const deviceEditSaving = ref(false)
const deviceEditRow = ref(null)
function canEditDevice(row) {
  return !!me.value && (me.value.role === 'admin' || row.registered_by === me.value.email)
}
function openDeviceEdit(row) {
  if (!contacts.value.length) loadContacts()
  deviceEditRow.value = {
    device_id: row.device_id,
    display_name: row.display_name || '',
    owner: row.owner || '',
    position_code: row.position_code || '',
    model_class: row.model_class || '',
    ip: row.ip || '',
    adb_port: row.adb_port || 5555,
    mac: row.mac || '',
    share_scope: row.share_scope || 'shared',
    allowed_emails: Array.isArray(row.allowed_emails) ? [...row.allowed_emails] : [],
    allowed_agent_labels: Array.isArray(row.allowed_agent_labels) ? [...row.allowed_agent_labels] : [],
    locked: !!row.locked,
    locked_reason: row.locked_reason || '',
  }
  deviceEditVisible.value = true
}
async function submitDeviceEdit() {
  const f = deviceEditRow.value
  const changes = {
    display_name: f.display_name, owner: f.owner, position_code: f.position_code,
    share_scope: f.share_scope, locked: !!f.locked, locked_reason: f.locked_reason || '',
  }
  if (f.share_scope === 'restricted') {
    changes.allowed_emails = f.allowed_emails
    changes.allowed_agent_labels = f.allowed_agent_labels
  }
  if (me.value.role === 'admin') {
    changes.model_class = f.model_class || null
    changes.ip = f.ip
    changes.adb_port = Number(f.adb_port)
    changes.mac = f.mac
  }
  deviceEditSaving.value = true
  try {
    await api('/api/devices/' + encodeURIComponent(f.device_id), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(changes),
    })
    ElMessage.success('设备已更新')
    deviceEditVisible.value = false
    loadDevices()
  } catch (e) { ElMessage.error(e.message) } finally { deviceEditSaving.value = false }
}

const productTypes = ref([])
const ptEditDialog = ref(false)
const ptEditRow = ref({ type_code: '', display_name: '', defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', jenkins_url: '', notes: '', chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null })

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
  chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null,
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
// 把相对 BASE_URL（'./' 或 '/quamm/'）拼成绝对地址：公网隧道 / Caddy 子路径下，
// fetch / <img> / <a download> 都能正确解析，不依赖当前 URL 是否带尾斜杠。
function apiUrl(path) {
  const rel = API_BASE ? API_BASE + path : path
  return new URL(rel, window.location.href).href
}
async function api(path, opts) {
  const r = await fetch(apiUrl(path), { credentials: 'include', ...opts })
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
// UTC ISO -> 本地时区可读时间
function formatLocal(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString()
}
function humanBytes(n) {
  if (n == null) return '-'
  if (n < 1024) return n + ' B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1024 / 1024).toFixed(2) + ' MB'
}
const downloadName = computed(() => (screenshotMeta.value && screenshotMeta.value.file_name) || 'screenshot.jpg')

// v0.24：管理员对设备截图，成功后用 el-image 弹窗预览（含元数据/下载/重新截图/旋转）
async function takeScreenshot(row) {
  screenshotBusy.value = row.device_id
  try {
    const r = await api('/api/devices/' + encodeURIComponent(row.device_id) + '/screenshot', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ quality: 60, rotate: screenshotRotate.value }),
    })
    screenshotDeviceId.value = row.device_id
    screenshotUrl.value = apiUrl('/api/screenshots/' + encodeURIComponent(r.file_name))
    screenshotMeta.value = r
    screenshotVisible.value = true
    ElMessage.success('截图成功：' + r.file_name)
  } catch (e) {
    ElMessage.error(e.message || '截图失败')
  } finally {
    screenshotBusy.value = ''
  }
}

async function removeDevice(row) {
  try {
    await ElMessageBox.confirm(
      `确定移除设备「${row.device_id}」（${row.ip || ''}）？将注销并从设备列表隐藏；`
      + '若该设备仍有活动租约，会先标记待下架、租约结束后自动摘除。',
      '移除设备',
      { type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消' },
    )
    await api('/api/devices/' + encodeURIComponent(row.device_id), { method: 'DELETE' })
    ElMessage.success('设备已移除')
    await loadDevices()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(e.message || '移除失败')
  }
}

// 重新截图：按当前所选 rotate 调用 POST，刷新 el-image 与元数据
async function retakeScreenshot() {
  if (!screenshotDeviceId.value) { ElMessage.error('缺少设备 ID，无法重新截图'); return }
  retaking.value = true
  try {
    const r = await api('/api/devices/' + encodeURIComponent(screenshotDeviceId.value) + '/screenshot', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ quality: 60, rotate: screenshotRotate.value }),
    })
    screenshotUrl.value = apiUrl('/api/screenshots/' + encodeURIComponent(r.file_name))
    screenshotMeta.value = r
    ElMessage.success('重新截图成功：' + r.file_name)
    if (screenshotHistoryVisible.value) loadScreenshotHistory(screenshotDeviceId.value)
  } catch (e) {
    ElMessage.error(e.message || '重新截图失败')
  } finally {
    retaking.value = false
  }
}

// 截图历史（设备表入口）
async function openScreenshotHistory(row) {
  screenshotHistoryDevice.value = row.device_id
  screenshotHistoryVisible.value = true
  await loadScreenshotHistory(row.device_id)
}
async function loadScreenshotHistory(deviceId) {
  screenshotHistoryLoading.value = true
  try {
    const r = await api('/api/screenshot-records?device_id=' + encodeURIComponent(deviceId) + '&limit=200')
    screenshotHistory.value = r.items || []
  } catch (e) { ElMessage.error(e.message) } finally { screenshotHistoryLoading.value = false }
}
function previewScreenshotRecord(rec) {
  screenshotDeviceId.value = rec.device_id || ''
  screenshotRotate.value = rec.rotate || 0
  screenshotUrl.value = apiUrl('/api/screenshots/' + encodeURIComponent(rec.file_name))
  screenshotMeta.value = rec
  screenshotVisible.value = true
}
async function deleteScreenshotRecord(rec) {
  try {
    await ElMessageBox.confirm('确定删除这张截图？删除后不可恢复。', '删除截图', { type: 'warning' })
  } catch (e) { return }
  try {
    await api('/api/screenshot-records/' + encodeURIComponent(rec.shot_id), { method: 'DELETE' })
    ElMessage.success('已删除')
    await loadScreenshotHistory(screenshotHistoryDevice.value)
    loadScreenshotStats()
    if (screenshotMeta.value && screenshotMeta.value.file_name === rec.file_name) {
      screenshotVisible.value = false
      screenshotUrl.value = ''
      screenshotMeta.value = null
    }
  } catch (e) { ElMessage.error(e.message) }
}

// 设置-截图存储
async function loadScreenshotStats() {
  try { screenshotStats.value = await api('/api/screenshot-records/stats') } catch (e) { /* ignore */ }
}
async function runScreenshotPrune() {
  screenshotStatsLoading.value = true
  try {
    const r = await api('/api/screenshot-records/prune', { method: 'POST' })
    ElMessage.success('清理完成：保留期删除 ' + r.retention_deleted + ' 张，总数上限删除 ' + r.total_deleted + ' 张，剩余 ' + r.remaining + ' 张')
    loadScreenshotStats()
    if (screenshotHistoryVisible.value) loadScreenshotHistory(screenshotHistoryDevice.value)
  } catch (e) { ElMessage.error(e.message) } finally { screenshotStatsLoading.value = false }
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
    screenshot_max_per_device: data.screenshot_max_per_device,
    screenshot_retention_days: data.screenshot_retention_days,
    screenshot_total_max: data.screenshot_total_max,
    screenshot_cleanup_on_release: !!data.screenshot_cleanup_on_release,
    pending_auto_recover_enabled: !!data.pending_auto_recover_enabled,
    pending_knock_eventcode: data.pending_knock_eventcode,
    pending_knock_timeout: data.pending_knock_timeout,
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

// ---- v0.25：编译服务器（SSH 凭据 Fernet 加密，服务端集中执行）----
const buildServers = ref([])
// 当前账号自己的编译服务器（一账号一台）；管理员可在列表看到全部
const myBuildRow = computed(() => buildServers.value.find(b => b.principal_id === (me.value && me.value.principal_id)) || null)
const bsDialog = ref(false)
const bsSaving = ref(false)
const bsRow = ref({ server_id: null, name: '', host: '', port: 22, username: '', auth_mode: 'password', secret: '', work_root: 'tres-auto' })
const bsProbing = ref('')
const bsPubKey = ref('')
const bsPubDialog = ref(false)
async function loadBuildServers() {
  try { buildServers.value = await api('/api/build-servers') } catch (e) { buildServers.value = [] }
}
function openBsCreate() {
  bsRow.value = { server_id: null, name: '', host: '', port: 22, username: '', auth_mode: 'password', secret: '', work_root: 'tres-auto' }
  bsDialog.value = true
}
function openBsEdit(row) {
  bsRow.value = { server_id: row.server_id, name: row.name, host: row.host, port: row.port, username: row.username, auth_mode: row.auth_mode, secret: '', work_root: row.work_root }
  bsDialog.value = true
}
async function saveBs() {
  const r = bsRow.value
  if (!r.name || !r.host || !r.username) { ElMessage.warning('名称/主机/用户名必填'); return }
  bsSaving.value = true
  try {
    const body = { name: r.name, host: r.host, port: r.port, username: r.username, auth_mode: r.auth_mode, work_root: r.work_root }
    if (r.secret) body.secret = r.secret
    let resp = null
    if (r.server_id) {
      resp = await api('/api/build-servers/' + encodeURIComponent(r.server_id), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    } else {
      resp = await api('/api/build-servers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    }
    if (resp && resp.dedup_hint) {
      ElMessageBox.confirm(resp.dedup_hint + '。仍要保留这条记录吗？', '疑似重复登记', {
        type: 'warning', confirmButtonText: '保留', cancelButtonText: '去删除',
      }).catch(() => {
        if (resp.server_id) api('/api/build-servers/' + encodeURIComponent(resp.server_id), { method: 'DELETE' }).then(loadBuildServers)
      })
    } else {
      ElMessage.success('已保存')
    }
    bsDialog.value = false
    loadBuildServers()
  } catch (e) { ElMessage.error(e.message) } finally { bsSaving.value = false }
}
async function deleteBs(row) {
  try {
    await ElMessageBox.confirm('确认删除你配置的编译服务器「' + row.name + '」？', '确认', { type: 'warning' })
  } catch (e) { return }
  try {
    await api('/api/build-servers/' + encodeURIComponent(row.server_id), { method: 'DELETE' })
    ElMessage.success('已删除'); loadBuildServers()
  } catch (e) { ElMessage.error(e.message) }
}
async function probeBs(row) {
  bsProbing.value = row.server_id
  try {
    const r = await api('/api/build-servers/' + encodeURIComponent(row.server_id) + '/probe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) })
    ElMessage.success('探测完成：' + r.status)
    loadBuildServers()
  } catch (e) { ElMessage.error(e.message) } finally { bsProbing.value = '' }
}
function showBsPub(row) { bsPubKey.value = row.public_key || ''; bsPubDialog.value = true }
function bsTagType(status) { return status === 'available' ? 'success' : (status === 'unavailable' ? 'danger' : 'info') }

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
    loadSettings(); loadProductTypes(); loadContacts(); loadSmtp(); loadAudit(); loadFeedback(); loadBuildServers()
    if (me.value && me.value.role === 'admin') loadScreenshotStats()
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
      const r = await fetch(apiUrl('/api/auth/me'), { credentials: 'include' })
      me.value = r.ok ? await r.json() : null
    } catch (e) { me.value = null }
  }
  booting.value = false
  if (me.value) {
    loadDashboard()
    loadVersionInfo()
    loadHealthInfo()
    loadProductTypes()
    loadBuildServers()
    loadDevices()
  }
}
onMounted(boot)

async function offerBrowserSave(email, password) {
  // SPA 异步登录不会自动触发浏览器保存提示，显式调用 Credential Management API
  // （Chromium 系支持；非安全上下文或不支持时静默跳过）。
  try {
    if (window.PasswordCredential && window.navigator && navigator.credentials) {
      const cred = new window.PasswordCredential({ id: email, password, name: email })
      await navigator.credentials.store(cred)
    }
  } catch (e) { /* 用户拒绝或不支持，忽略 */ }
}

async function doLogin() {
  loginError.value = ''
  const token = loginToken.value.trim()
  try {
    me.value = await api('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    })
    await offerBrowserSave(me.value.email, token)
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
    chip_model: row.chip_model || '', float_abi: row.float_abi || 'auto', has_fpu: !!row.has_fpu,
    build_server_id: row.build_server_id || null,
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
        chip_model: ptEditRow.value.chip_model, float_abi: ptEditRow.value.float_abi,
        has_fpu: ptEditRow.value.has_fpu, build_server_id: ptEditRow.value.build_server_id,
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
    chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null,
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
        chip_model: row.chip_model, float_abi: row.float_abi, has_fpu: row.has_fpu,
        build_server_id: row.build_server_id,
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
      <form autocomplete="on" @submit.prevent="doLogin">
        <el-input v-model="loginToken" type="password" show-password name="token"
          autocomplete="current-password" placeholder="rr_..." />
        <div style="margin-top: 12px"><el-button type="primary" native-type="submit">登录</el-button></div>
      </form>
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
          <el-table :data="detailDevices" border size="small" class="device-table">
            <el-table-column prop="device_id" label="ID" min-width="95" show-overflow-tooltip />
            <el-table-column prop="display_name" label="名称" min-width="90" show-overflow-tooltip />
            <el-table-column prop="position_code" label="位置" min-width="70" show-overflow-tooltip />
            <el-table-column prop="owner" label="责任人" min-width="70" show-overflow-tooltip />
            <el-table-column prop="register_agent" label="Agent" min-width="90" show-overflow-tooltip>
              <template #default="{ row }">{{ row.register_agent || '-' }}</template>
            </el-table-column>
            <el-table-column prop="health_level" label="健康" min-width="45" />
            <el-table-column label="状态" min-width="75">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTag[row.status] || 'info'">{{ row.status }}</el-tag>
                <el-tooltip v-if="row.locked" :content="'已锁定' + (row.locked_reason ? '：' + row.locked_reason : '') + (row.locked_by ? '（' + row.locked_by + '）' : '')" placement="top">
                  <el-tag size="small" type="warning" style="margin-left: 4px">锁定</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="占用者" min-width="120" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.holder_email">{{ row.holder_email }}</span>
                <span v-else style="color: #c0c4cc">-</span>
              </template>
            </el-table-column>
            <el-table-column prop="adb_address" label="ADB" min-width="110" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.adb_address }}</span></template>
            </el-table-column>
            <el-table-column label="共享" min-width="80">
              <template #default="{ row }">
                <el-tooltip v-if="row.share_scope === 'restricted'"
                  :content="(row.allowed_emails || []).concat(row.allowed_agent_labels || []).join('，') || '受限'" placement="top">
                  <el-tag size="small" type="warning">受限</el-tag>
                </el-tooltip>
                <el-tag v-else-if="row.share_scope === 'private'" size="small" type="danger">独享</el-tag>
                <el-tag v-else size="small" type="success">公共</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button v-if="canEditDevice(row)" link type="primary" size="small" @click="openDeviceEdit(row)">编辑</el-button>
                <el-button v-if="me.role === 'admin'" link type="primary" size="small" :loading="screenshotBusy === row.device_id" @click="takeScreenshot(row)">截图</el-button>
                <el-button v-if="me.role === 'admin'" link size="small" @click="openScreenshotHistory(row)">记录</el-button>
                <el-button v-if="canEditDevice(row)" link type="danger" size="small" @click="removeDevice(row)">移除</el-button>
              </template>
            </el-table-column>
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
            <el-form v-if="settings" :model="settingsForm" label-width="160px" class="settings-grid-form">
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
              <el-form-item label="pending 自动敲门恢复">
                <el-switch v-model="settingsForm.pending_auto_recover_enabled" />
              </el-form-item>
              <el-form-item label="敲门事件码">
                <el-input v-model="settingsForm.pending_knock_eventcode" maxlength="6" style="width: 140px" />
              </el-form-item>
              <el-form-item label="敲门等待（秒）">
                <el-input-number v-model="settingsForm.pending_knock_timeout" :min="5" :max="120" />
              </el-form-item>
              <el-form-item class="settings-grid-full">
                <el-button type="primary" :loading="settingsSaving" @click="saveSettings">保存</el-button>
              </el-form-item>
            </el-form>
</el-tab-pane>

          <el-tab-pane label="设备类型" name="product-types">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px">
              <h3 style="margin: 0">设备类型元数据（defconfig / SDK / 编译 / 镜像 / Jenkins / 芯片·浮点 / 备注）</h3>
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
              <el-table-column label="芯片 / 浮点" width="180">
                <template #default="{ row }">{{ row.chip_model || '-' }} · {{ row.float_abi || 'auto' }}{{ row.has_fpu ? ' (FPU)' : '' }}</template>
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

          <!-- v0.24 截图存储管理：概况 + 保留策略 + 立即清理 -->
          <el-tab-pane v-if="me.role === 'admin'" label="截图存储" name="screenshots">
            <h3 style="margin-top: 0">截图存储概况</h3>
            <el-descriptions :column="3" border size="small" style="max-width: 700px; margin-bottom: 14px">
              <el-descriptions-item label="截图总数">{{ screenshotStats ? screenshotStats.count : '-' }} 张</el-descriptions-item>
              <el-descriptions-item label="占用空间">{{ humanBytes(screenshotStats ? screenshotStats.total_bytes : null) }}</el-descriptions-item>
              <el-descriptions-item label="涉及设备">{{ screenshotStats ? screenshotStats.devices : '-' }} 台</el-descriptions-item>
            </el-descriptions>
            <div style="margin-bottom: 16px">
              <el-button @click="loadScreenshotStats">刷新统计</el-button>
              <el-button type="warning" :loading="screenshotStatsLoading" @click="runScreenshotPrune">立即清理</el-button>
              <span style="margin-left: 10px; color: #909399; font-size: 12px">按保留天数与总数上限删除最旧截图，删除写入审计（screenshot_prune）</span>
            </div>
            <h3>保留策略</h3>
            <el-form v-if="settings" :model="settingsForm" label-width="220px" style="max-width: 640px">
              <el-form-item label="每台设备最多保留（张）">
                <el-input-number v-model="settingsForm.screenshot_max_per_device" :min="1" :max="1000" />
              </el-form-item>
              <el-form-item label="全局保留天数（天）">
                <el-input-number v-model="settingsForm.screenshot_retention_days" :min="1" :max="3650" />
              </el-form-item>
              <el-form-item label="全局最多保留（张）">
                <el-input-number v-model="settingsForm.screenshot_total_max" :min="1" :max="100000" />
              </el-form-item>
              <el-form-item label="释放/换用户时清空该设备截图">
                <el-switch v-model="settingsForm.screenshot_cleanup_on_release" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="settingsSaving" @click="saveSettings">保存策略</el-button>
              </el-form-item>
            </el-form>
          </el-tab-pane>
          <el-tab-pane label="编译服务器" name="build-servers">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
              <h3 style="margin:0">我的编译服务器（账号私有，SSH 凭据加密保管，服务端集中执行，不下发 agent）</h3>
              <el-button v-if="!myBuildRow" type="primary" @click="openBsCreate">添加我的编译服务器</el-button>
            </div>
            <p style="color:#909399">只显示当前登录账号自己的编译服务器（token 隔离，管理员也不互看）。每个账号一账号一台。点「探测」：端口→认证→工作目录→git→工具链，全通过才具备 SDK 编译升级能力。密钥模式留空会自动生成 ed25519，公钥需放到编译机。</p>
            <el-table :data="buildServers" border>
              <el-table-column prop="name" label="名称" width="140" />
              <el-table-column label="主机" width="180">
                <template #default="{ row }">{{ row.username }}@{{ row.host }}:{{ row.port }}</template>
              </el-table-column>
              <el-table-column label="认证" width="110">
                <template #default="{ row }"><el-tag size="small">{{ row.auth_mode === 'key' ? '密钥' : row.auth_mode === 'agent' ? '免密' : '密码' }}</el-tag><span v-if="row.secret_mask" style="margin-left:6px;color:#909399">{{ row.secret_mask }}</span></template>
              </el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="{ row }"><el-tag :type="bsTagType(row.status)" size="small">{{ row.status }}</el-tag></template>
              </el-table-column>
              <el-table-column label="探测详情" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="row.probe_detail">
                    <el-tag v-for="st in row.probe_detail.steps" :key="st.step" :type="st.ok ? 'success' : 'danger'" size="small" style="margin-right:4px">{{ st.step }}</el-tag>
                  </span>
                  <span v-else style="color:#c0c4cc">尚未探测</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="280">
                <template #default="{ row }">
                  <el-button size="small" :loading="bsProbing === row.server_id" @click="probeBs(row)">探测</el-button>
                  <el-button v-if="row.public_key" size="small" @click="showBsPub(row)">公钥</el-button>
                  <el-button size="small" @click="openBsEdit(row)">编辑</el-button>
                  <el-button size="small" type="danger" plain @click="deleteBs(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="系统介绍" name="intro">
            <div class="intro-toolbar">
              <a class="intro-open" href="intro.html" target="_blank" rel="noopener">在全页面打开系统介绍 ↗</a>
            </div>
            <iframe src="intro.html" title="系统介绍"
              style="width:100%;height:74vh;border:1px solid var(--el-border-color);border-radius:10px;"></iframe>
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

        <el-dialog v-model="ptEditDialog" :title="'编辑设备类型 · ' + ptEditRow.type_code" width="820px" :close-on-click-modal="false">
          <el-form label-width="96px" class="pt-grid">
            <el-form-item label="defconfig"><el-input v-model="ptEditRow.defconfig" placeholder="project_moto_ld_defconfig" /></el-form-item>
            <el-form-item label="主芯片型号"><el-input v-model="ptEditRow.chip_model" placeholder="如 Cortex-A7（探测可自动回填）" /></el-form-item>
            <el-form-item label="SDK 获取命令" class="pt-full"><el-input v-model="ptEditRow.sdk_repo_cmd" type="textarea" :rows="2" placeholder="repo init -u http://.../manifest; repo sync -j8" /></el-form-item>
            <el-form-item label="编译命令" class="pt-full"><el-input v-model="ptEditRow.build_cmd" type="textarea" :rows="2" placeholder="make distclean && make xxx_defconfig && make xos -j8" /></el-form-item>
            <el-form-item label="镜像路径"><el-input v-model="ptEditRow.image_path" placeholder="out/qm10xd_linux/qmimages/" /></el-form-item>
            <el-form-item label="Jenkins 链接"><el-input v-model="ptEditRow.jenkins_url" placeholder="http://.../Daily/XOS/moto_ld_102d/" /></el-form-item>
            <el-form-item label="浮点 ABI">
              <el-select v-model="ptEditRow.float_abi" style="width: 100%">
                <el-option label="auto（探测自动判定）" value="auto" />
                <el-option label="hard（硬浮点 / vxosdbg）" value="hard" />
                <el-option label="soft（软浮点 / xosdbg）" value="soft" />
              </el-select>
            </el-form-item>
            <el-form-item label="硬件 FPU"><el-switch v-model="ptEditRow.has_fpu" /></el-form-item>
            <el-form-item label="备注" class="pt-full"><el-input v-model="ptEditRow.notes" type="textarea" :rows="2" placeholder="其他备注" /></el-form-item>
          </el-form>
          <template #footer><el-button @click="ptEditDialog = false">取消</el-button><el-button type="primary" @click="savePtEdit">保存</el-button></template>
        </el-dialog>

        <el-dialog v-model="ptCreateDialog" title="新增机型" width="860px" :close-on-click-modal="false">
          <el-form ref="ptCreateForm" :model="ptCreateRow" :rules="ptCreateRules" label-width="96px" class="pt-grid">
            <el-form-item label="type_code" prop="type_code"><el-input v-model="ptCreateRow.type_code" placeholder="如 moto_ld" /></el-form-item>
            <el-form-item label="名称"><el-input v-model="ptCreateRow.display_name" placeholder="展示名，留空则用 type_code" /></el-form-item>
            <el-form-item label="有屏"><el-switch v-model="ptCreateRow.has_screen" /></el-form-item>
            <el-form-item label="有灯"><el-switch v-model="ptCreateRow.has_led" /></el-form-item>
            <el-form-item label="串口数"><el-input-number v-model="ptCreateRow.serial_count" :min="0" /></el-form-item>
            <el-form-item label="主芯片型号"><el-input v-model="ptCreateRow.chip_model" placeholder="如 Cortex-A7（探测可自动回填）" /></el-form-item>
            <el-form-item label="defconfig" prop="defconfig"><el-input v-model="ptCreateRow.defconfig" placeholder="project_moto_ld_defconfig" /></el-form-item>
            <el-form-item label="浮点 ABI">
              <el-select v-model="ptCreateRow.float_abi" style="width: 100%">
                <el-option label="auto（探测自动判定）" value="auto" />
                <el-option label="hard（硬浮点 / vxosdbg）" value="hard" />
                <el-option label="soft（软浮点 / xosdbg）" value="soft" />
              </el-select>
            </el-form-item>
            <el-form-item label="SDK 获取命令" prop="sdk_repo_cmd" class="pt-full"><el-input v-model="ptCreateRow.sdk_repo_cmd" type="textarea" :rows="2" placeholder="repo init -u http://.../manifest; repo sync -j8" /></el-form-item>
            <el-form-item label="编译命令" prop="build_cmd" class="pt-full"><el-input v-model="ptCreateRow.build_cmd" type="textarea" :rows="2" placeholder="make distclean && make xxx_defconfig && make xos -j8" /></el-form-item>
            <el-form-item label="镜像路径" prop="image_path"><el-input v-model="ptCreateRow.image_path" placeholder="out/qm10xd_linux/qmimages/" /></el-form-item>
            <el-form-item label="Jenkins 链接" prop="jenkins_url"><el-input v-model="ptCreateRow.jenkins_url" placeholder="http://.../Daily/XOS/moto_ld_102d/" /></el-form-item>
            <el-form-item label="硬件 FPU"><el-switch v-model="ptCreateRow.has_fpu" /></el-form-item>
            <el-form-item label="允许不完整"><el-checkbox v-model="ptAllowIncomplete">允许不完整（仅测试）</el-checkbox></el-form-item>
            <el-form-item label="备注" class="pt-full"><el-input v-model="ptCreateRow.notes" type="textarea" :rows="2" placeholder="其他备注" /></el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="ptCreateDialog = false">取消</el-button>
            <el-button type="primary" :loading="ptCreateSaving" @click="submitPtCreate">新增</el-button>
          </template>
        </el-dialog>

        <el-dialog v-model="bsDialog" :title="bsRow.server_id ? '编辑编译服务器' : '添加编译服务器'" width="560px" :close-on-click-modal="false">
          <el-form label-width="110px">
            <el-form-item label="名称"><el-input v-model="bsRow.name" placeholder="如 build01" /></el-form-item>
            <el-form-item label="主机"><el-input v-model="bsRow.host" placeholder="IP 或域名" /></el-form-item>
            <el-form-item label="端口"><el-input-number v-model="bsRow.port" :min="1" :max="65535" /></el-form-item>
            <el-form-item label="用户名"><el-input v-model="bsRow.username" /></el-form-item>
            <el-form-item label="认证方式">
              <el-radio-group v-model="bsRow.auth_mode">
                <el-radio value="password">密码（默认）</el-radio>
                <el-radio value="key">密钥（推荐）</el-radio>
                <el-radio value="agent">免密（本机已配置）</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="bsRow.auth_mode === 'agent'" label=" ">
              <span style="color:#909399;font-size:12px;line-height:1.5">复用运行 TRES 服务的本机已配置免密（~/.ssh 默认密钥 / agent / config），无需填写或保存任何凭据。</span>
            </el-form-item>
            <el-form-item v-if="bsRow.auth_mode !== 'agent'" :label="bsRow.auth_mode === 'key' ? '私钥/留空生成' : '密码'">
              <el-input v-model="bsRow.secret" type="textarea" :rows="3" :placeholder="bsRow.auth_mode === 'key' ? '粘贴现有 OpenSSH 私钥；留空则自动生成 ed25519' : 'SSH 登录密码，加密保管'" show-password />
            </el-form-item>
            <el-form-item label="工作根目录"><el-input v-model="bsRow.work_root" placeholder="home 下目录，默认 tres-auto" /></el-form-item>
          </el-form>
          <template #footer><el-button @click="bsDialog = false">取消</el-button><el-button type="primary" :loading="bsSaving" @click="saveBs">保存</el-button></template>
        </el-dialog>

        <el-dialog v-model="bsPubDialog" title="SSH 公钥（放到编译机 authorized_keys）" width="620px">
          <el-input v-model="bsPubKey" type="textarea" :rows="6" readonly />
          <template #footer><el-button type="primary" @click="bsPubDialog = false">知道了</el-button></template>
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
              <el-table-column label="MAC" width="150">
                <template #default="{ row }">{{ row.mac || row.probe?.mac || '-' }}</template>
              </el-table-column>
              <el-table-column label="其他信息" width="100">
                <template #default="{ row }">
                  <el-popover placement="left" :width="260" trigger="click">
                    <template #reference>
                      <el-button size="small" link type="primary">查看详情</el-button>
                    </template>
                    <div v-if="row.error" style="color: #f56c6c; margin-bottom: 6px">探测异常：{{ row.error }}</div>
                    <el-descriptions v-if="row.other_info" :column="1" size="small" border>
                      <el-descriptions-item label="健康等级">{{ row.other_info.level || '-' }}</el-descriptions-item>
                      <el-descriptions-item label="产品名">{{ row.other_info.product_name || '-' }}</el-descriptions-item>
                      <el-descriptions-item label="固件版本">{{ row.other_info.firmware || '-' }}</el-descriptions-item>
                      <el-descriptions-item label="有线MAC">{{ row.other_info.eth_mac || '-' }}</el-descriptions-item>
                      <el-descriptions-item label="无线MAC">{{ row.other_info.wlan_mac || '-' }}</el-descriptions-item>
                      <el-descriptions-item label="存储类型">{{ row.other_info.storage || '-' }}</el-descriptions-item>
                      <el-descriptions-item label="带WiFi">{{ row.other_info.has_wifi ? '是' : '否' }}</el-descriptions-item>
                      <el-descriptions-item label="板级兼容">{{ row.other_info.board || '-' }}</el-descriptions-item>
                    </el-descriptions>
                    <div v-else>暂无更多探测信息</div>
                  </el-popover>
                </template>
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
              <el-alert
                v-if="!deviceAddProbe?.probe?.model"
                type="warning" :closable="false" show-icon style="margin-bottom: 8px"
                title="未自动探测到设备类型，请从已注册机型中选择"
              />
              <el-select v-model="deviceAddConfirm.model_class" placeholder="选择已注册机型" clearable style="width: 100%">
                <el-option v-for="t in enabledProductTypes" :key="t.type_code" :label="t.type_code" :value="t.type_code" />
              </el-select>
            </el-form-item>
            <el-form-item label="共享方式">
              <el-radio-group v-model="deviceAddConfirm.share_scope">
                <el-radio value="shared">公共（任何人可申请）</el-radio>
                <el-radio value="private">个人独享（仅自己）</el-radio>
                <el-radio value="restricted">指定名单</el-radio>
              </el-radio-group>
            </el-form-item>
            <template v-if="deviceAddConfirm.share_scope === 'restricted'">
              <el-form-item label="允许用户">
                <el-select v-model="deviceAddConfirm.allowed_emails" multiple filterable allow-create
                  default-first-option placeholder="选择或输入邮箱，回车添加" style="width: 100%">
                  <el-option v-for="c in contacts" :key="c.email" :label="c.email" :value="c.email" />
                </el-select>
              </el-form-item>
              <el-form-item label="允许 Agent">
                <el-select v-model="deviceAddConfirm.allowed_agent_labels" multiple filterable allow-create
                  default-first-option placeholder="输入 Agent 名，回车添加" style="width: 100%" />
              </el-form-item>
            </template>
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

      <!-- v0.26 设备编辑 -->
      <el-dialog v-model="deviceEditVisible" title="编辑设备" width="720px" :close-on-click-modal="false">
        <el-form v-if="deviceEditRow" label-width="110px" class="settings-grid-form">
          <el-form-item label="名称"><el-input v-model="deviceEditRow.display_name" /></el-form-item>
          <el-form-item label="责任人"><el-input v-model="deviceEditRow.owner" /></el-form-item>
          <el-form-item label="位置"><el-input v-model="deviceEditRow.position_code" /></el-form-item>
          <el-form-item v-if="me.role === 'admin'" label="机型">
            <el-select v-model="deviceEditRow.model_class" clearable style="width: 100%">
              <el-option v-for="t in enabledProductTypes" :key="t.type_code" :label="t.type_code" :value="t.type_code" />
            </el-select>
          </el-form-item>
          <template v-if="me.role === 'admin'">
            <el-form-item label="IP"><el-input v-model="deviceEditRow.ip" /></el-form-item>
            <el-form-item label="ADB 端口"><el-input-number v-model="deviceEditRow.adb_port" :min="1" :max="65535" /></el-form-item>
            <el-form-item label="MAC"><el-input v-model="deviceEditRow.mac" /></el-form-item>
          </template>
          <el-form-item label="锁定设备" class="settings-grid-full">
            <el-switch v-model="deviceEditRow.locked" />
            <span style="margin-left: 8px; color: #909399">锁定后不参与任何分配（不影响已在用的租约）</span>
          </el-form-item>
          <el-form-item v-if="deviceEditRow.locked" label="锁定原因" class="settings-grid-full">
            <el-input v-model="deviceEditRow.locked_reason" placeholder="如 送修中 / 借出 / 不稳需要人工检查" maxlength="128" />
          </el-form-item>
          <el-form-item label="共享方式" class="settings-grid-full">
            <el-radio-group v-model="deviceEditRow.share_scope">
              <el-radio value="shared">公共（任何人可申请）</el-radio>
              <el-radio value="private">个人独享（仅自己）</el-radio>
              <el-radio value="restricted">指定名单</el-radio>
            </el-radio-group>
          </el-form-item>
          <template v-if="deviceEditRow.share_scope === 'restricted'">
            <el-form-item label="允许用户" class="settings-grid-full">
              <el-select v-model="deviceEditRow.allowed_emails" multiple filterable allow-create
                default-first-option placeholder="选择或输入邮箱，回车添加" style="width: 100%">
                <el-option v-for="c in contacts" :key="c.email" :label="c.email" :value="c.email" />
              </el-select>
            </el-form-item>
            <el-form-item label="允许 Agent" class="settings-grid-full">
              <el-select v-model="deviceEditRow.allowed_agent_labels" multiple filterable allow-create
                default-first-option placeholder="输入 Agent 名，回车添加" style="width: 100%" />
            </el-form-item>
          </template>
        </el-form>
        <template #footer>
          <el-button @click="deviceEditVisible = false">取消</el-button>
          <el-button type="primary" :loading="deviceEditSaving" @click="submitDeviceEdit">保存</el-button>
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

      <!-- 全局弹窗：v0.24 设备截图预览（元数据 / 下载 / 重新截图 / 旋转） -->
      <el-dialog v-model="screenshotVisible" title="设备截图" width="600px">
        <template v-if="screenshotMeta">
          <el-descriptions :column="2" border size="small" style="margin-bottom: 12px">
            <el-descriptions-item label="设备">{{ screenshotMeta.device_id || screenshotMeta.ip || '-' }}</el-descriptions-item>
            <el-descriptions-item label="截图时间（本地）">{{ formatLocal(screenshotMeta.captured_at) }}</el-descriptions-item>
            <el-descriptions-item label="质量 quality">{{ screenshotMeta.quality }}</el-descriptions-item>
            <el-descriptions-item label="旋转 rotate">{{ screenshotMeta.rotate }}°</el-descriptions-item>
            <el-descriptions-item label="浮点 ABI">{{ screenshotMeta.float_abi }}</el-descriptions-item>
            <el-descriptions-item label="二进制 bin">{{ screenshotMeta.bin }}</el-descriptions-item>
            <el-descriptions-item label="大小">{{ screenshotMeta.bytes }} bytes（{{ humanBytes(screenshotMeta.bytes) }}）</el-descriptions-item>
            <el-descriptions-item label="来源">{{ screenshotMeta.created_by || '-' }}</el-descriptions-item>
          </el-descriptions>
          <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap">
            <span style="font-size: 13px; color: #606266">旋转</span>
            <el-select v-model="screenshotRotate" style="width: 100px">
              <el-option v-for="r in rotateOptions" :key="r" :label="r + '°'" :value="r" />
            </el-select>
            <el-button type="primary" size="small" :loading="retaking" @click="retakeScreenshot">重新截图</el-button>
            <a v-if="screenshotUrl" :href="screenshotUrl" :download="downloadName" class="shot-download">
              <el-button size="small">下载</el-button>
            </a>
          </div>
          <el-image v-if="screenshotUrl" :key="screenshotUrl" :src="screenshotUrl" fit="contain"
                    class="shot-preview" :preview-src-list="[screenshotUrl]" />
        </template>
        <template #footer><el-button @click="screenshotVisible = false">关闭</el-button></template>
      </el-dialog>

      <!-- 全局弹窗：v0.24 该设备截图历史（缩略图 + 预览 + 单张删除） -->
      <el-dialog v-model="screenshotHistoryVisible" :title="'截图记录 · ' + screenshotHistoryDevice" width="720px">
        <el-table :data="screenshotHistory" border size="small" v-loading="screenshotHistoryLoading" max-height="420">
          <el-table-column label="缩略图" width="120">
            <template #default="{ row }">
              <div style="cursor: pointer" @click="previewScreenshotRecord(row)">
                <el-image :src="apiUrl('/api/screenshots/' + encodeURIComponent(row.file_name))"
                          fit="cover" style="width: 96px; height: 64px" />
              </div>
            </template>
          </el-table-column>
          <el-table-column label="时间（本地）" width="200">
            <template #default="{ row }">{{ formatLocal(row.captured_at) }}</template>
          </el-table-column>
          <el-table-column label="文件" min-width="170" show-overflow-tooltip>
            <template #default="{ row }">{{ row.file_name }}</template>
          </el-table-column>
          <el-table-column label="质量/旋转" width="120">
            <template #default="{ row }">q{{ row.quality }} · {{ row.rotate }}°</template>
          </el-table-column>
          <el-table-column label="大小" width="100">
            <template #default="{ row }">{{ humanBytes(row.bytes) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <el-button size="small" link type="primary" @click="previewScreenshotRecord(row)">预览</el-button>
              <el-button size="small" link type="danger" @click="deleteScreenshotRecord(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!screenshotHistory.length && !screenshotHistoryLoading" description="该设备暂无截图记录" />
        <template #footer><el-button @click="screenshotHistoryVisible = false">关闭</el-button></template>
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
.shot-download { display: inline-block; text-decoration: none; }
.shot-preview { width: 100%; height: 52vh; display: block; }
.device-table { font-size: 12px; }
.device-table :deep(.el-table__cell) { padding: 3px 0; }
.device-table :deep(.cell) { font-size: 12px; line-height: 1.4; padding-left: 6px; padding-right: 6px; }
.device-table :deep(.el-table__header .cell) { font-size: 12px; font-weight: 600; }
.pt-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 16px; }
.pt-grid :deep(.el-form-item) { margin-bottom: 14px; }
.pt-grid .pt-full { grid-column: 1 / -1; }
.shot-preview :deep(.el-image__inner) { width: 100%; height: 100%; object-fit: contain; }
.settings-grid-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 18px; }
.settings-grid-form.settings-grid-form .settings-grid-full { grid-column: 1 / -1; }
.intro-toolbar { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.intro-open { color: var(--el-color-primary); text-decoration: none; font-size: 13px; }
.intro-open:hover { text-decoration: underline; }
</style>
