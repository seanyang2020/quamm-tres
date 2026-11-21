<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

const pages = [
  { key: 'devices', label: '设备中心' },
  { key: 'scheduling', label: '资源调度' },
  { key: 'onboarding', label: '接入申请', admin: true },
  { key: 'credentials', label: '凭据' },
  { key: 'wirelog', label: 'COMRes' },
  { key: 'settings', label: '设置' },
]
const active = ref('devices')
const LEGACY_BOARD_KEYS = new Set(['dashboard', 'board', '看板'])
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
const ALL_DEVICES = '__all__'       // 概况「全部设备」明细的特殊标识，不是真实机型
const UNCLASSIFIED = '__unclassified__'  // model_class 为空的非 retired 设备伪分组
const UNCLASSIFIED_LABEL = '未分类'
const selectedDeviceType = ref(null) // null=概况、'__all__'=全部设备、其它=具体机型

function modelClassKey(d) {
  const mc = (d && d.model_class != null) ? String(d.model_class).trim() : ''
  return mc || UNCLASSIFIED
}
function modelClassLabel(key) {
  return key === UNCLASSIFIED ? UNCLASSIFIED_LABEL : key
}
const detailStatusTab = ref('all')
const detailPage = ref(1)
const detailPageSize = ref(25)
const deviceProbeSort = ref('') // '' | ascending | descending
/** 顶部统计卡筛选：null=未筛；all/online/offline/pending/occupied/available */
const statsFilter = ref(null)
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
const classifiedDevices = computed(() => devices.value.filter((d) => d && d.status !== 'retired'))
const deviceTotals = computed(() => {
  const list = classifiedDevices.value
  const occupied = list.filter((d) => d.status === 'occupied').length
  const available = list.filter((d) => d.status === 'available').length
  const offline = list.filter((d) => d.status === 'offline').length
  const pending = list.filter((d) => d.status === 'pending').length
  return {
    total: list.length,
    occupied,
    available,
    offline,
    pending,
    online: available + occupied,
  }
})
// 机型余量 chips：沿用 /api/dashboard.products（可用/已租）
const productChips = computed(() => {
  const products = (dashboard.value && dashboard.value.products) || []
  return products
    .filter((p) => p && (p.total > 0 || p.available > 0 || p.leased > 0 || p.occupied > 0))
    .map((p) => ({
      type: p.type_code,
      label: `${p.type_code} ${p.available || 0}/${p.leased || 0}`,
    }))
})
const patrolBusy = ref(false)
const deviceOverview = computed(() => {
  const map = new Map()
  for (const d of classifiedDevices.value) {
    const key = modelClassKey(d)
    if (!map.has(key)) map.set(key, [])
    map.get(key).push(d)
  }
  return [...map.entries()].map(([type, list]) => ({
    type,
    label: modelClassLabel(type),
    count: list.length,
    available: list.filter((d) => d.status === 'available').length,
    occupied: list.filter((d) => d.status === 'occupied').length,
    offline: list.filter((d) => d.status === 'offline').length,
    pending: list.filter((d) => d.status === 'pending').length,
  })).sort((a, b) => {
    if (a.type === UNCLASSIFIED) return 1
    if (b.type === UNCLASSIFIED) return -1
    return a.type.localeCompare(b.type)
  })
})
// 当前明细的原始集合（不含状态与搜索过滤），用于 tab 计数
const detailBaseDevices = computed(() => {
  if (selectedDeviceType.value === ALL_DEVICES) return classifiedDevices.value
  return classifiedDevices.value.filter((d) => modelClassKey(d) === selectedDeviceType.value)
})
// 状态 tab 计数基于原始集合，与搜索无关
const detailCounts = computed(() => {
  const list = detailBaseDevices.value
  return {
    all: list.length,
    occupied: list.filter((d) => d.status === 'occupied').length,
    available: list.filter((d) => d.status === 'available').length,
    offline: list.filter((d) => d.status === 'offline').length,
    pending: list.filter((d) => d.status === 'pending').length,
  }
})
// 状态 + 搜索过滤后的完整列表（分页前）；支持按最近探测排序
const detailFilteredDevices = computed(() => {
  let list = detailBaseDevices.value
  if (detailStatusTab.value !== 'all') {
    list = list.filter((d) => d.status === detailStatusTab.value)
  } else if (statsFilter.value === 'online') {
    list = list.filter((d) => d.status === 'available' || d.status === 'occupied')
  }
  const kw = deviceSearch.value.trim().toLowerCase()
  if (kw) {
    list = list.filter((d) =>
      [d.device_id, d.display_name, d.project_name, d.position_code, d.owner, d.adb_address,
       d.status, d.health_level, d.mac, d.register_agent]
        .filter((v) => v !== null && v !== undefined).join(' ').toLowerCase().includes(kw))
  }
  const order = deviceProbeSort.value
  if (order === 'ascending' || order === 'descending') {
    const mul = order === 'ascending' ? 1 : -1
    list = list.slice().sort((a, b) => {
      const ta = parseUtcDate(deviceProbeAt(a))
      const tb = parseUtcDate(deviceProbeAt(b))
      const va = ta ? ta.getTime() : 0
      const vb = tb ? tb.getTime() : 0
      if (va === vb) return 0
      return va < vb ? -mul : mul
    })
  }
  return list
})
function onDeviceSortChange({ prop, order }) {
  deviceProbeSort.value = (prop === 'last_probe_at' && order) ? order : ''
}
// 当前页数据
const detailDevices = computed(() => {
  const start = (detailPage.value - 1) * detailPageSize.value
  return detailFilteredDevices.value.slice(start, start + detailPageSize.value)
})
const detailTitle = computed(() => {
  if (selectedDeviceType.value === ALL_DEVICES) return '全部设备'
  if (selectedDeviceType.value === UNCLASSIFIED) return UNCLASSIFIED_LABEL
  return selectedDeviceType.value
})
// 品类详情页点「添加设备」时预填该品类（全部设备 / 未分类页不预填）
const prefillType = computed(() => {
  const t = selectedDeviceType.value
  if (!t || t === ALL_DEVICES || t === UNCLASSIFIED) return ''
  return t
})
const detailRangeStart = computed(() => detailFilteredDevices.value.length === 0 ? 0 : (detailPage.value - 1) * detailPageSize.value + 1)
const detailRangeEnd = computed(() => Math.min(detailPage.value * detailPageSize.value, detailFilteredDevices.value.length))

// 切换品类 / 状态 / 搜索时回到第 1 页
watch([selectedDeviceType, detailStatusTab, deviceSearch], () => { detailPage.value = 1 })
// 每页条数变化时保留筛选，仅把页码收敛到有效范围
watch([detailPageSize, () => detailFilteredDevices.value.length], () => {
  const maxPage = Math.max(1, Math.ceil(detailFilteredDevices.value.length / detailPageSize.value))
  if (detailPage.value > maxPage) detailPage.value = maxPage
})
const STATUS_LABEL = {
  occupied: '已分配', available: '空闲', offline: '离线', pending: '待检', online: '在线', all: '全部',
}
const emptyDetailText = computed(() => {
  const kw = deviceSearch.value.trim()
  const total = detailCounts.value.all
  if (kw) return `没有匹配「${kw}」的设备（${detailTitle.value} 共 ${total} 台）`
  if (statsFilter.value === 'online') {
    return `${detailTitle.value} · 在线 暂无设备（该机型共 ${total} 台）`
  }
  if (detailStatusTab.value !== 'all') {
    return `${detailTitle.value} · ${STATUS_LABEL[detailStatusTab.value] || detailStatusTab.value} 暂无设备（该机型共 ${total} 台）`
  }
  return `${detailTitle.value} 暂无设备`
})
function showAllInDetail() {
  detailStatusTab.value = 'all'
  statsFilter.value = selectedDeviceType.value === ALL_DEVICES ? 'all' : null
  deviceSearch.value = ''
  detailPage.value = 1
}
function openDeviceDetail(type, status = 'all') {
  selectedDeviceType.value = type
  detailStatusTab.value = status
  deviceSearch.value = ''
  detailPage.value = 1
  if (type === ALL_DEVICES) {
    statsFilter.value = status === 'all' ? 'all' : status
  } else {
    statsFilter.value = null
  }
}
function openDeviceType(type) { openDeviceDetail(type, 'all') }
/** 顶部统计卡：再点同一卡取消筛选，回到概况 */
function toggleStatsFilter(key) {
  if (statsFilter.value === key) {
    statsFilter.value = null
    selectedDeviceType.value = null
    detailStatusTab.value = 'all'
    deviceSearch.value = ''
    detailPage.value = 1
    return
  }
  statsFilter.value = key
  selectedDeviceType.value = ALL_DEVICES
  deviceSearch.value = ''
  detailPage.value = 1
  detailStatusTab.value = (key === 'all' || key === 'online') ? 'all' : key
}
function openOverviewStat(status) { toggleStatsFilter(status) }
function onDetailStatusTabChange(name) {
  if (selectedDeviceType.value !== ALL_DEVICES) {
    statsFilter.value = null
    return
  }
  statsFilter.value = name === 'all' ? 'all' : name
}
function backToOverview() {
  selectedDeviceType.value = null
  statsFilter.value = null
  detailStatusTab.value = 'all'
}

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
  pending_patrol_interval_minutes: 10,
  probe_backend: 'real',
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
// v0.29.2 指定名单「允许 Agent」：按邮箱从有效凭据拉取可选项
const deviceAddAgentInfo = ref({})
const deviceAddAgentSeen = ref(new Set())
const deviceAddAgentAuto = ref(new Set())
// 设备发现模式
const deviceAddMode = ref('ip')
const discoveryNetworks = ref('')
const discovering = ref(false)
const discoveryResults = ref([])

function openDeviceAdd(presetModel) {
  // v0.29.2：打开即刷新机型字典，避免刚派生/新增的机型在下拉里看不到
  // 品类详情页进入时带 presetModel，弹窗里直接预选该品类
  const preset = presetModel || ''
  loadProductTypes()
  deviceAddStep.value = 1
  deviceAddMode.value = 'ip'
  deviceAddProbe.value = null
  deviceAddForm.value = { ip: '', port: 5555, hint_model: preset }
  deviceAddConfirm.value = { position_code: '', owner: '', model_class: preset, note: '', share_scope: 'shared', allowed_emails: [], allowed_agent_labels: [] }
  deviceAddAgentInfo.value = {}
  deviceAddAgentSeen.value = new Set()
  deviceAddAgentAuto.value = new Set()
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
    share_scope: 'shared',
    allowed_emails: [],
    allowed_agent_labels: [],
  }
  deviceAddAgentInfo.value = {}
  deviceAddAgentSeen.value = new Set()
  deviceAddAgentAuto.value = new Set()
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
    // v0.29.2：探测/提示得到的机型未必已在字典里；未注册就留空，让用户从下拉选，
    // 否则提交后会以 UNKNOWN_MODEL 失败（"机型 xxx 不在机型字典中"）。
    const probeModel = r.probe?.model || deviceAddForm.value.hint_model || ''
    const registered = enabledProductTypes.value.some((t) => t.type_code === probeModel)
    deviceAddConfirm.value.model_class = registered ? probeModel : ''
    if (probeModel && !registered) {
      ElMessage.warning(
        '探测到机型 ' + probeModel + ' 未注册：请先在「设置 → 设备类型」新增/派生该机型，或从下拉改选已有机型',
      )
    }
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
// v0.29.2 编辑弹窗的「允许 Agent」候选（按 allowed_emails 拉取）
const deviceEditAgentInfo = ref({})
const deviceEditAgentSeen = ref(new Set())
const deviceEditAgentAuto = ref(new Set())
const cfgAuditBusy = ref(false)
async function showConfigAudit() {
  cfgAuditBusy.value = true
  try {
    const r = await api('/api/config-audit')
    const lines = []
    lines.push('设备总数 ' + r.total + ' 台，当前在线 ' + r.online + ' 台')
    lines.push('配置指纹 ' + r.fingerprinted + ' 台（占全池 ' + r.coverage.vs_total_pct
      + '%，占在线 ' + r.coverage.vs_online_pct + '%）')
    lines.push('老固件 ' + r.legacy + ' 台，未知 ' + r.unknown + ' 台')
    if (r.duplicate_groups && r.duplicate_groups.length) {
      lines.push('')
      lines.push('⚠ 重复登记 ' + r.duplicate_groups.length + ' 组：')
      r.duplicate_groups.forEach(g => lines.push('  ' + g.join('  <->  ')))
    }
    if (r.mismatches && r.mismatches.length) {
      lines.push('')
      lines.push('⚠ 机型/配置不符 ' + r.mismatches.length + ' 台：')
      r.mismatches.forEach(m => lines.push('  ' + m.device_id + ' [' + (m.model_class || '-') + '] '
        + (m.issues || []).join('；')))
    }
    const defs = Object.keys(r.by_defconfig || {})
    if (defs.length) {
      lines.push('')
      lines.push('配置分布：')
      defs.forEach(k => {
        const v = r.by_defconfig[k]
        lines.push('  ' + k + '：' + v.count + ' 台，' + v.shas.length + ' 种 sha'
          + (v.shas.length > 1 ? '（同配置名不同内容）' : ''))
      })
    }
    const gits = Object.keys(r.git_versions || {})
    if (gits.length) {
      lines.push('')
      lines.push('git 版本：' + Object.entries(r.git_versions).map(([k, v]) => k + '×' + v).join('，'))
    }
    await ElMessageBox.alert(lines.join('\n'), '全池配置体检', {
      customStyle: { whiteSpace: 'pre-line', maxWidth: '600px' },
      confirmButtonText: '知道了',
    })
  } catch (e) {
    ElMessage.error('体检失败：' + (e && e.message ? e.message : e))
  } finally {
    cfgAuditBusy.value = false
  }
}

function cfgTagType(verdict) {
  if (verdict === 'ok') return 'success'
  if (verdict === 'duplicate') return 'danger'
  if (verdict === 'mismatch_defconfig' || verdict === 'mismatch_model') return 'warning'
  return 'info'
}

function cfgTooltip(cfg) {
  if (!cfg) return ''
  const parts = []
  if (cfg.defconfig) parts.push('defconfig: ' + cfg.defconfig)
  if (cfg.sha8) parts.push('sha256: ' + cfg.sha8)
  if (cfg.git) parts.push('git: ' + cfg.git)
  if (cfg.built_at) parts.push('构建: ' + cfg.built_at + (cfg.host ? ' @' + cfg.host : ''))
  if (cfg.issues && cfg.issues.length) parts.push('⚠ ' + cfg.issues.join('；'))
  return parts.join('\n')
}

function canEditDevice(row) {
  return !!me.value && (me.value.role === 'admin' || row.registered_by === me.value.email)
}
function openDeviceEdit(row) {
  if (!contacts.value.length) loadContacts()
  deviceEditAgentInfo.value = {}
  deviceEditAgentSeen.value = new Set()
  deviceEditAgentAuto.value = new Set()
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
    last_probe_at: row.last_probe_at || null,
    last_seen_at: row.last_seen_at || null,
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

// ---- v0.29.2 指定名单「允许 Agent」自动联动 ----
function normalizeAllowedEmails(list) {
  const out = []
  for (const item of (list || [])) {
    const email = String(item || '').trim().toLowerCase()
    if (email && !out.includes(email)) out.push(email)
  }
  return out
}

async function fetchAgentInfo(emails) {
  if (!emails.length) return {}
  const r = await api('/api/principals/agent-options', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ emails }),
  })
  const map = {}
  for (const item of (r.items || [])) {
    const key = String(item.email || '').trim().toLowerCase()
    if (key) map[key] = item
  }
  return map
}

function pickAgentOptions(infoMap, emails) {
  const options = new Set()
  for (const email of normalizeAllowedEmails(emails)) {
    for (const label of (infoMap[email]?.agents || [])) {
      const s = String(label || '').trim()
      if (s) options.add(s)
    }
  }
  return [...options].sort()
}

async function refreshAllowedAgents({ emails, infoRef, seenRef, autoRef, getLabels, setLabels, isStale }) {
  const normalized = normalizeAllowedEmails(emails)
  let info = {}
  try {
    info = await fetchAgentInfo(normalized)
  } catch (e) {
    info = {}
  }
  if (isStale && isStale()) return

  // 计算本次“被移除用户”曾提供的 agent，后续从已选值中清掉，避免残留。
  const current = new Set(normalized)
  const removedLabels = new Set()
  for (const [email, item] of Object.entries(infoRef.value || {})) {
    if (current.has(email)) continue
    for (const label of (item?.agents || [])) {
      const s = String(label || '').trim()
      if (s) removedLabels.add(s)
    }
  }

  infoRef.value = info
  const offered = new Set(pickAgentOptions(info, normalized))
  const auto = new Set()
  for (const email of normalized) {
    const agents = (info[email]?.agents || []).map((a) => String(a || '').trim()).filter(Boolean)
    // 只有一个有效 agent 时直接自动填入；多个则作为选项交给用户勾选。
    if (agents.length === 1) auto.add(agents[0])
  }

  const prevAuto = autoRef.value || new Set()
  const merged = []
  const push = (label) => {
    const s = String(label || '').trim()
    if (s && !merged.includes(s)) merged.push(s)
  }
  for (const label of (getLabels() || [])) {
    const s = String(label || '').trim()
    if (!s || prevAuto.has(s)) continue
    // 之前由已移除用户提供、且当前候选里没有的 agent 一并清掉。
    if (removedLabels.has(s) && !offered.has(s)) continue
    push(s)
  }
  for (const label of auto) push(label)

  setLabels(merged)
  autoRef.value = auto
  seenRef.value = offered
}

const deviceAddAgentOptions = computed(() =>
  pickAgentOptions(deviceAddAgentInfo.value, deviceAddConfirm.value.allowed_emails)
)
const deviceEditAgentOptions = computed(() =>
  pickAgentOptions(deviceEditAgentInfo.value, deviceEditRow.value?.allowed_emails || [])
)

let deviceAddAgentSeq = 0
watch(
  [() => deviceAddConfirm.value.allowed_emails, () => deviceAddConfirm.value.share_scope],
  async ([emails, scope]) => {
    if (scope !== 'restricted') { deviceAddAgentSeq += 1; return }
    const seq = ++deviceAddAgentSeq
    await refreshAllowedAgents({
      emails,
      infoRef: deviceAddAgentInfo,
      seenRef: deviceAddAgentSeen,
      autoRef: deviceAddAgentAuto,
      getLabels: () => deviceAddConfirm.value.allowed_agent_labels,
      setLabels: (v) => { deviceAddConfirm.value.allowed_agent_labels = v },
      isStale: () => seq !== deviceAddAgentSeq,
    })
  },
  { deep: true }
)

let deviceEditAgentSeq = 0
watch(
  [() => deviceEditRow.value?.allowed_emails, () => deviceEditRow.value?.share_scope],
  async ([emails, scope]) => {
    if (!deviceEditRow.value || scope !== 'restricted') { deviceEditAgentSeq += 1; return }
    const seq = ++deviceEditAgentSeq
    await refreshAllowedAgents({
      emails,
      infoRef: deviceEditAgentInfo,
      seenRef: deviceEditAgentSeen,
      autoRef: deviceEditAgentAuto,
      getLabels: () => deviceEditRow.value.allowed_agent_labels,
      setLabels: (v) => { if (deviceEditRow.value) deviceEditRow.value.allowed_agent_labels = v },
      isStale: () => seq !== deviceEditAgentSeq,
    })
  },
  { deep: true }
)

const productTypes = ref([])
const ptEditDialog = ref(false)
const ptEditRow = ref({ type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1, adb_port: 5555, model_pattern: '', defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', jenkins_url: '', notes: '', chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null, new_type_code: '' })

// v0.16：版本/术语说明移入设置页「关于」；版本信息仍在挂载时加载
const versionInfo = ref(null)
const healthInfo = ref(null)
const ptCreateDialog = ref(false)
const ptCreateForm = ref(null)
const ptAllowIncomplete = ref(false)
const ptCreateSaving = ref(false)
const ptCreateRow = ref({
  type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1,
  adb_port: 5555, model_pattern: '',
  defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', jenkins_url: '', notes: '',
  chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null, copy_from: '',
})
const ptCreateTitle = ref('新增机型')
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
function apiErrorDetail(j, status) {
  const d = j && j.detail
  if (d == null) return String(status)
  if (typeof d === 'string') return d
  if (typeof d === 'object') {
    if (d.message) return d.message
    if (d.detail && typeof d.detail === 'string') return d.detail
    try { return JSON.stringify(d) } catch (e) { return String(status) }
  }
  return String(d)
}

async function api(path, opts) {
  const r = await fetch(apiUrl(path), { credentials: 'include', ...opts })
  if (!r.ok) {
    let detail = String(r.status)
    try {
      const j = await r.json()
      detail = apiErrorDetail(j, r.status)
    } catch (e) { /* ignore */ }
    if (r.status === 401 && me.value && !path.startsWith('/api/auth/')) me.value = null
    const err = new Error(detail)
    err.status = r.status
    err.path = path
    throw err
  }
  return r.json()
}

async function loadDashboard() { dashboard.value = await api('/api/dashboard') }
let devicesReqSeq = 0
async function loadDevices() {
  const seq = ++devicesReqSeq
  try {
    const list = await api('/api/devices')
    if (seq !== devicesReqSeq) return // 已有更新的请求在飞，丢弃旧响应，避免旧数据覆盖新数据
    const arr = Array.isArray(list) ? list : (list && Array.isArray(list.items) ? list.items : [])
    devices.value = arr
    if (!arr.length) ElMessage.warning('设备列表返回为空，请稍后重试或检查登录状态')
  } catch (e) {
    ElMessage.error('设备列表加载失败：' + e.message)
  }
}
// UTC ISO -> 本地时区可读时间。无时区后缀的 naive UTC 先补 Z，避免被当成本地时间。
function parseUtcDate(iso) {
  if (!iso) return null
  let s = String(iso)
  if (!/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z'
  const d = new Date(s)
  return isNaN(d.getTime()) ? null : d
}
function formatLocal(iso) {
  if (!iso) return '-'
  const d = parseUtcDate(iso)
  return d ? d.toLocaleString() : iso
}
function formatLocalHm(iso) {
  if (!iso) return '-'
  const d = parseUtcDate(iso)
  return d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : iso
}
/** 相对时间：3 分钟前 / 1 小时前 / 从未 */
function formatRelative(iso) {
  const d = parseUtcDate(iso)
  if (!d) return '从未'
  const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  if (sec < 60) return '刚刚'
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`
  if (sec < 86400 * 30) return `${Math.floor(sec / 86400)} 天前`
  return formatLocal(iso)
}
function deviceProbeAt(row) {
  return (row && row.last_probe_at) || null
}
/** 探测来源：设备行无独立 source 字段时不伪造，仅在有 last_probe_at 时标注「探测记录」 */
function deviceProbeSource(row) {
  if (!row) return ''
  if (row.last_probe_at) return '探测记录'
  return ''
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
// v0.28：手动探测（单体 / 批量待检·离线）
const probeBusy = ref('')
const probeBatchBusy = ref('')
async function probeOne(row) {
  if (probeBusy.value) return
  probeBusy.value = row.device_id
  try {
    const r = await api(`/api/devices/${row.device_id}/probe`, { method: 'POST' })
    if (r.promoted) ElMessage.success(`探测成功：${r.level}，已转正为可用`)
    else if (r.demoted) ElMessage.warning(`探测失败：${r.level}，已置为不可用（${r.status}）`)
    else if (r.level === 'L3') ElMessage.success(`探测成功：${r.level}，状态 ${r.status}`)
    else ElMessage.warning(r.note || `探测完成：${r.level || '失败'}，仍为 ${r.status}`)
    await loadDevices()
  } catch (e) { ElMessage.error(e.message) } finally { probeBusy.value = '' }
}
async function probeBatch(scope) {
  if (probeBatchBusy.value) return
  // 只探「当前品类 + 当前筛选」下的设备，避免按钮计数与实际探测范围不一致
  const targets = detailBaseDevices.value.filter((d) => d.status === scope)
  if (!targets.length) { ElMessage.info('当前没有可探测的设备'); return }
  const ids = targets.slice(0, 20).map((d) => d.device_id)
  probeBatchBusy.value = scope
  try {
    const r = await api('/api/devices/probe-batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_ids: ids, limit: 20 }),
    })
    const more = targets.length > ids.length ? `（仅前 ${ids.length} 台，共 ${targets.length} 台）` : ''
    const dg = r.demoted ? `，降级 ${r.demoted} 台` : ''
    ElMessage.success(`探测 ${r.scanned} 台，转正 ${r.promoted} 台${dg}，未通过 ${r.failed} 台${more}`)
    await loadDevices()
  } catch (e) { ElMessage.error(e.message) } finally { probeBatchBusy.value = '' }
}
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
  detailStatusTab.value = 'all'
  detailPage.value = 1
  statsFilter.value = null
}
function applyProductChip(typeCode) {
  if (!typeCode) return
  openDeviceDetail(typeCode, 'all')
}
/** 立即巡检：沿用批量探测（pending+offline），再刷新看板数据与设备列表 */
async function triggerPatrolNow() {
  if (patrolBusy.value) return
  patrolBusy.value = true
  try {
    if (me.value && me.value.role === 'admin') {
      const r = await api('/api/devices/probe-batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ statuses: ['pending', 'offline'], limit: 20 }),
      })
      ElMessage.success(`巡检完成：探测 ${r.scanned || 0} 台，转正 ${r.promoted || 0} 台，未通过 ${r.failed || 0} 台`)
    }
    await Promise.all([loadDashboard(), loadDevices()])
  } catch (e) { ElMessage.error(e.message) } finally { patrolBusy.value = false }
}
function resolveLegacyRoute() {
  try {
    const hash = String(window.location.hash || '').replace(/^#\/?/, '').split('?')[0]
    const path = String(window.location.pathname || '').replace(/^\//, '').split('/')[0]
    const key = (hash || path || '').toLowerCase()
    if (LEGACY_BOARD_KEYS.has(key) || LEGACY_BOARD_KEYS.has(hash) || LEGACY_BOARD_KEYS.has(path)) {
      if (window.location.hash && /board|dashboard/i.test(window.location.hash)) {
        history.replaceState(null, '', window.location.pathname + window.location.search)
      }
      if (active.value !== 'devices') {
        switchPage('devices')
      } else {
        active.value = 'devices'
      }
      return true
    }
  } catch (e) { /* ignore */ }
  return false
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
    pending_patrol_interval_minutes: data.pending_patrol_interval_minutes,
    probe_backend: data.probe_backend || 'real',
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

// ---------------- COMRes（原 WireLog tab，同 key；节点视图 + 资源池） ----------------
const wlNodes = ref([])
const wlDetail = ref(null)
const wlDetailId = ref(null)
const wlNodesError = ref('')
const wlDetailError = ref('')
const wlConfigForm = ref({
  heartbeat_interval_s: 60,
  log_level: 'info',
  keepalive_enabled: true,
  keepalive_interval_s: 60,
  consistency_interval_s: 600,
  rotate_local_token: false,
})
const wlGrantForm = ref({ slot: 1, principal: '' })

// /api/comres/resources → { allocated, bound, pool, inactive }
const comresAllocated = ref([])
const comresBound = ref([])
const comresPool = ref([])
const comresInactive = ref([])
const comresError = ref('')
const comresLoading = ref(false)
const comresShowInactiveOnly = ref(false)
const comresPruneBusy = ref(false)

// COMRes 授权抽屉（客户端自助申请审批）
const comresAuthDrawer = ref(false)
const comresAuthLoading = ref(false)
const comresAuthError = ref('')
const comresAuthItems = ref([])
const comresAuthPendingCount = ref(0)
const comresAuthLastApprovedAt = ref(null)
const comresAutoApprove = ref(true)
const comresAuthFilter = ref('all') // all | pending | approved | rejected | revoked
const comresAuthBusyId = ref(null)

const comresAllRows = computed(() => {
  const inactive = comresInactive.value || []
  if (comresShowInactiveOnly.value) {
    return inactive
  }
  return [
    ...(comresAllocated.value || []),
    ...(comresBound.value || []),
    ...(comresPool.value || []),
    ...inactive,
  ]
})
/** 设备中心顶部：串口资源 / 在池端口（来自 /api/comres/resources） */
const comresPortStats = computed(() => {
  const allocated = (comresAllocated.value || []).length
  const bound = (comresBound.value || []).length
  const pool = (comresPool.value || []).length
  const inactive = (comresInactive.value || []).length
  return {
    total: allocated + bound + pool + inactive,
    allocated,
    bound,
    pool,
    inactive,
  }
})

const comresByNode = computed(() => {
  const m = Object.create(null)
  for (const r of comresAllRows.value) {
    const id = r && r.node_id
    if (!id) continue
    if (!m[id]) m[id] = []
    m[id].push(r)
  }
  return m
})

function wlNodeSerialRows(nodeId) {
  return (comresByNode.value && comresByNode.value[nodeId]) || []
}
function wlNodeHasSerial(nodeId) {
  return wlNodeSerialRows(nodeId).length > 0
}
function wlOnlinePortCount(nodeId) {
  return wlNodeSerialRows(nodeId).filter(
    (r) => r && (r.health_state === 'online' || r.health_state === 'ready'),
  ).length
}
function wlNodeAuthStatus(row) {
  return (row && row.auth_status) || 'none'
}
function wlNodeAuthorized(row) {
  const st = wlNodeAuthStatus(row)
  // 存量无申请记录视为可用；待审/拒绝/撤销未授权
  return st === 'approved' || st === 'none'
}
function wlAuthBadge(status) {
  const map = {
    approved: { label: '已授权', type: 'success' },
    pending: { label: '待审批', type: 'warning' },
    rejected: { label: '已拒绝', type: 'danger' },
    revoked: { label: '已撤销', type: 'info' },
    none: { label: '未申请', type: 'info' },
  }
  return map[status] || map.none
}
function wlNodeRowClass({ row }) {
  const parts = []
  if (!wlNodeHasSerial(row.node_id)) parts.push('comres-no-serial')
  if (!wlNodeAuthorized(row)) parts.push('comres-unauthorized')
  return parts.join(' ')
}

/** 端口/资源行是否具备可展示的串口信息（无则灰显，禁止打开） */
function portHasSerial(row) {
  if (!row) return false
  return !!(row.endpoint || row.com || row.com_name || row.tty
    || row.com_key || row.shell_reachable || row.tcp_port)
}
/** 已绑定/可用：可打开浏览器 TCP shell */
function portCanOpen(row) {
  if (!portHasSerial(row)) return false
  if (row.shell_reachable) return true
  if (row.endpoint && row.slot != null) return true
  const hs = row.health_state
  return !!(row.endpoint && (hs === 'online' || hs === 'ready' || !hs || hs === 'unknown'))
}
function findComresForPort(port) {
  const nodeId = wlDetailId.value || (wlDetail.value && wlDetail.value.node_id)
  if (!nodeId || !port) return null
  const rows = wlNodeSerialRows(nodeId)
  return rows.find((r) =>
    (port.slot != null && r.slot === port.slot)
    || (port.endpoint && r.endpoint === port.endpoint)
    || (port.com_name && r.com === port.com_name),
  ) || null
}
function portAllocated(row) {
  const meta = findComresForPort(row)
  if (meta) return !!meta.allocated
  return !!row.allocated
}
function portAllocatedBy(row) {
  const meta = findComresForPort(row)
  if (meta && meta.allocated) return meta.allocated_by || ''
  return row.allocated_by || ''
}
function portOccupancyLabel(row) {
  const meta = findComresForPort(row) || row
  if (meta && meta.allocated) {
    const by = meta.allocated_by || '—'
    const exp = meta.allocated_expire ? formatLocal(meta.allocated_expire) : '—'
    return '占用：' + by + ' · 到期 ' + exp
  }
  if (meta && meta.state === 'bound') return '已绑定未分配'
  return '资源池'
}
function portSn(row) {
  if (row.sn) return row.sn
  if (row.bound_device && row.bound_device.sn) return row.bound_device.sn
  return ''
}
function portMac(row) {
  if (row.mac) return row.mac
  if (row.shell_mac) return row.shell_mac
  if (row.bound_device && row.bound_device.mac) return row.bound_device.mac
  return ''
}
function comresOccupancyLabel(row) {
  if (row && row.allocated) {
    const by = row.allocated_by || '—'
    const exp = row.allocated_expire ? formatLocal(row.allocated_expire) : '—'
    return '占用：' + by + ' · 到期 ' + exp
  }
  if (row && row.state === 'bound') return '已绑定未分配'
  return '资源池'
}
function comresRowClass({ row }) {
  const parts = []
  if (!portHasSerial(row)) parts.push('comres-no-serial')
  if (row && (row.inactive || row.state === 'stale' || row.state === 'superseded'
      || row.state === 'offline' || row.health_state === 'stale'
      || row.health_state === 'superseded')) {
    parts.push('comres-inactive')
  }
  if (row && row.auth_status && row.auth_status !== 'approved' && row.auth_status !== 'none') {
    parts.push('comres-unauthorized')
  }
  return parts.join(' ')
}

function comresInactiveLabel(row) {
  if (!row) return ''
  const reason = row.inactive_reason || row.state || row.health_state || ''
  const map = {
    endpoint_superseded: '端点被取代',
    heartbeat_stale: '心跳超时',
    port_offline: '端口离线',
    node_dormant: '节点休眠',
    stale: '失效',
    superseded: '已取代',
    offline: '离线',
  }
  return map[reason] || reason || '失效'
}

async function loadComresResources() {
  comresLoading.value = true
  comresError.value = ''
  try {
    const r = await api('/api/comres/resources')
    comresAllocated.value = Array.isArray(r && r.allocated) ? r.allocated : []
    comresBound.value = Array.isArray(r && r.bound) ? r.bound : []
    comresPool.value = Array.isArray(r && r.pool) ? r.pool : []
    comresInactive.value = Array.isArray(r && r.inactive) ? r.inactive : []
  } catch (e) {
    comresAllocated.value = []
    comresBound.value = []
    comresPool.value = []
    comresInactive.value = []
    comresError.value = String(e.message || e)
    ElMessage.error('COMRes 资源加载失败：' + comresError.value)
  } finally {
    comresLoading.value = false
  }
}

async function runComresPrune() {
  comresPruneBusy.value = true
  try {
    const audit = await api('/api/comres/maintenance/audit')
    const c = (audit && audit.counts) || {}
    const lines = [
      `重复组：${c.duplicates || 0}`,
      `心跳超时节点：${c.stale || 0}（约 ${c.stale_ports || 0} 端口）`,
      `孤儿/残留节点：${c.orphan_nodes || 0}（约 ${c.orphan_ports || 0} 端口）`,
      `幽灵占用：${c.allocated_zombie || 0}`,
    ]
    const samples = []
    for (const d of (audit.duplicates || []).slice(0, 2)) {
      samples.push(`· 重复 ${d.kind} ${d.com || d.com_key || ''} ×${d.count}`)
    }
    for (const s of (audit.stale || []).slice(0, 2)) {
      samples.push(`· stale ${s.node_id} (${s.display_name || ''})`)
    }
    for (const o of (audit.orphan_nodes || []).slice(0, 2)) {
      samples.push(`· orphan ${o.node_id} → ${o.action}`)
    }
    const body = lines.join('\n')
      + (samples.length ? '\n\n样例：\n' + samples.join('\n') : '')
      + '\n\n确认后将按规则执行 prune（写审计）。'
    await ElMessageBox.confirm(body, '清理无效资源（审计预览）', {
      type: 'warning',
      confirmButtonText: '执行清理',
      cancelButtonText: '取消',
      customClass: 'wl-enroll-box',
    })
    const r = await api('/api/comres/maintenance/prune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: false }),
    })
    ElMessage.success('清理完成：处理 ' + (r.applied_count || 0) + ' 项')
    loadWlNodes()
  } catch (e) {
    if (e === 'cancel' || e?.message === 'cancel') return
    ElMessage.error(String(e.message || e))
  } finally {
    comresPruneBusy.value = false
  }
}

function copyNodeToken() {
  const t = wlDetail.value && wlDetail.value.node_token
  if (!t) {
    ElMessage.warning('无可用 token（历史节点可能未存密文，请让客户端重新申请）')
    return
  }
  copyText(t, '已拷贝 node_token')
}

async function loadComresAuthSummary() {
  try {
    const r = await api('/api/comres/applications?page=1&page_size=1')
    comresAuthPendingCount.value = Number(r && r.pending_count) || 0
    comresAuthLastApprovedAt.value = (r && r.last_approved_at) || null
    if (typeof r.auto_approve === 'boolean') comresAutoApprove.value = r.auto_approve
  } catch (_) {
    /* 概览失败不阻断主视图 */
  }
}

async function loadComresApplications() {
  comresAuthLoading.value = true
  comresAuthError.value = ''
  try {
    const q = new URLSearchParams({ page: '1', page_size: '100' })
    if (comresAuthFilter.value && comresAuthFilter.value !== 'all') {
      q.set('status', comresAuthFilter.value)
    }
    const r = await api('/api/comres/applications?' + q.toString())
    comresAuthItems.value = Array.isArray(r && r.items) ? r.items : []
    comresAuthPendingCount.value = Number(r && r.pending_count) || 0
    comresAuthLastApprovedAt.value = (r && r.last_approved_at) || null
    if (typeof r.auto_approve === 'boolean') comresAutoApprove.value = r.auto_approve
  } catch (e) {
    comresAuthItems.value = []
    comresAuthError.value = String(e.message || e)
  } finally {
    comresAuthLoading.value = false
  }
}

function openComresAuthDrawer() {
  comresAuthDrawer.value = true
  loadComresApplications()
}

async function toggleComresAutoApprove(val) {
  try {
    const r = await api('/api/comres/apply-settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_approve: !!val }),
    })
    comresAutoApprove.value = !!(r && r.auto_approve)
    ElMessage.success(comresAutoApprove.value ? '已开启自动批准' : '已关闭自动批准（需人工审批）')
  } catch (e) {
    comresAutoApprove.value = !val
    ElMessage.error(String(e.message || e))
  }
}

async function comresApprove(row) {
  comresAuthBusyId.value = row.request_id
  try {
    await api('/api/comres/applications/' + row.request_id + '/approve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    ElMessage.success('已批准')
    await loadComresApplications()
    loadWlNodes()
  } catch (e) {
    ElMessage.error(String(e.message || e))
  } finally {
    comresAuthBusyId.value = null
  }
}

async function comresReject(row) {
  try {
    const { value } = await ElMessageBox.prompt('请填写拒绝原因', '拒绝申请', {
      confirmButtonText: '拒绝',
      cancelButtonText: '取消',
      inputPattern: /\S+/,
      inputErrorMessage: '原因不能为空',
    })
    comresAuthBusyId.value = row.request_id
    await api('/api/comres/applications/' + row.request_id + '/reject', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: value }),
    })
    ElMessage.success('已拒绝')
    await loadComresApplications()
    loadWlNodes()
  } catch (e) {
    if (e === 'cancel' || e?.message === 'cancel') return
    ElMessage.error(String(e.message || e))
  } finally {
    comresAuthBusyId.value = null
  }
}

async function comresRevoke(row) {
  try {
    await ElMessageBox.confirm(
      `撤销「${row.display_name || row.client_id}」的授权？节点将退役并从资源池下架。`,
      '撤销授权', { type: 'warning' })
    comresAuthBusyId.value = row.request_id
    await api('/api/comres/applications/' + row.request_id + '/revoke', { method: 'POST' })
    ElMessage.success('已撤销')
    await loadComresApplications()
    loadWlNodes()
  } catch (e) {
    if (e === 'cancel' || e?.message === 'cancel') return
    ElMessage.error(String(e.message || e))
  } finally {
    comresAuthBusyId.value = null
  }
}

function shortClientId(id) {
  if (!id) return '—'
  return String(id).slice(0, 8)
}

async function loadWlNodes() {
  wlNodesError.value = ''
  try {
    wlNodes.value = await api('/api/wirelog/nodes')
  } catch (e) {
    wlNodes.value = []
    wlNodesError.value = String(e.message || e)
    ElMessage.error('节点列表加载失败：' + wlNodesError.value)
  }
  loadComresResources()
  loadComresAuthSummary()
  if (wlDetailId.value) openWlDetail(wlDetailId.value)
}

async function openWlDetail(nodeId) {
  wlDetailError.value = ''
  try {
    wlDetail.value = await api('/api/wirelog/nodes/' + nodeId)
    wlDetailId.value = nodeId
    wlConfigForm.value = { ...wlConfigForm.value, ...(wlDetail.value.config || {}) }
  } catch (e) {
    wlDetailError.value = String(e.message || e)
    ElMessage.error('节点详情加载失败：' + wlDetailError.value)
  }
}

function closeWlDetail() { wlDetail.value = null; wlDetailId.value = null; wlDetailError.value = '' }

// ---------------- COMRes 串口终端（独立全屏 xterm 页 /shell.html，浏览器 TCP shell） ----------------
function openWlShell(row) {
  if (!portCanOpen(row)) {
    ElMessage.warning('无串口资源，无法打开')
    return
  }
  const nodeId = (row && row.node_id)
    || wlDetailId.value
    || (wlDetail.value && wlDetail.value.node_id)
  if (!nodeId) { ElMessage.warning('请先打开节点详情或选择带 node_id 的资源'); return }
  const q = new URLSearchParams({
    node_id: nodeId,
    slot: String(Number((row && row.slot) || 1)),
    name: (row && (row.com || row.com_name))
      || (wlDetail.value && wlDetail.value.display_name)
      || nodeId,
    tty: (row && (row.tty || row.com || row.com_name)) || '',
    baud: String((row && row.baud) || ''),
  })
  window.open('/shell.html?' + q.toString(), '_blank')
}

async function saveWlConfig() {
  const config = { ...wlConfigForm.value }
  try {
    await api('/api/wirelog/nodes/' + wlDetailId.value + '/config', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    })
    ElMessage.success('配置已下发（下次心跳生效）')
    openWlDetail(wlDetailId.value)
  } catch (e) { ElMessage.error(String(e.message || e)) }
}

async function wlGrant() {
  try {
    await api('/api/wirelog/nodes/' + wlDetailId.value + '/write-grant', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slot: wlGrantForm.value.slot,
                             principal: wlGrantForm.value.principal }),
    })
    ElMessage.success('写授权已下发')
    openWlDetail(wlDetailId.value)
  } catch (e) { ElMessage.error(String(e.message || e)) }
}

async function wlRevoke(slot) {
  try {
    await api('/api/wirelog/nodes/' + wlDetailId.value + '/write-grant', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slot, revoke: true }),
    })
    ElMessage.success('已撤销写权限')
    openWlDetail(wlDetailId.value)
  } catch (e) { ElMessage.error(String(e.message || e)) }
}

function wlStatusType(s) {
  return { online: 'success', enrolled: 'info', stale: 'warning',
           offline: 'danger', dormant: 'info', retired: 'info' }[s] || 'info'
}

async function wlRetire(node) {
  try {
    await ElMessageBox.confirm(
      `确定退役日志机「${node.display_name}」？退役后该节点心跳将被拒绝（可恢复需重新发放）。`,
      '退役确认', { type: 'warning' })
    await api('/api/wirelog/nodes/' + node.node_id + '/retire', { method: 'POST' })
    ElMessage.success('已退役')
    if (wlDetailId.value === node.node_id) closeWlDetail()
    loadWlNodes()
  } catch (e) {
    if (e === 'cancel' || e?.message === 'cancel') return
    ElMessage.error(String(e.message || e))
  }
}

async function wlDelete(node) {
  if (!me.value || me.value.role !== 'admin') {
    ElMessage.error('仅管理员可用')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定硬删除日志机「${node.display_name}」？仅 retired 节点可删，将级联清理绑定/审计/申请引用，不可恢复。`,
      '删除确认', { type: 'error', confirmButtonText: '删除' })
    await api('/api/wirelog/nodes/' + node.node_id, { method: 'DELETE' })
    ElMessage.success('已删除')
    if (wlDetailId.value === node.node_id) closeWlDetail()
    loadWlNodes()
  } catch (e) {
    if (e === 'cancel' || e?.message === 'cancel') return
    ElMessage.error(String(e.message || e))
  }
}

// P9：跨视图跳转
function jumpToDevice(deviceId) {
  active.value = 'devices'
  selectedDeviceType.value = ALL_DEVICES
  detailStatusTab.value = 'all'
  deviceSearch.value = deviceId
  loadDevices()
}

function jumpToWlNode(nodeId) {
  active.value = 'wirelog'
  loadWlNodes()
  openWlDetail(nodeId)
}

// P9：设备详情侧串口卡片（弹窗）
const wlBindDlg = reactive({ visible: false, loading: false, device: null, rows: [] })

async function openDeviceWlBindings(row) {
  wlBindDlg.visible = true
  wlBindDlg.loading = true
  wlBindDlg.device = row
  wlBindDlg.rows = []
  try {
    wlBindDlg.rows = await api('/api/wirelog/device/' + row.device_id + '/bindings')
  } catch (e) { ElMessage.error(String(e.message || e)) }
  finally { wlBindDlg.loading = false }
}

function switchPage(key) {
  if (LEGACY_BOARD_KEYS.has(key)) key = 'devices'
  active.value = key
  if (key === 'devices') {
    goDevices()
    loadDevices()
    loadDashboard()
    loadComresResources()
  }
  else if (key === 'scheduling') {
    loadLeases(); loadQueue(); loadAnomalies(); loadNotices(); loadPatrol()
  }
  else if (key === 'onboarding') loadContacts()
  else if (key === 'credentials') loadCredentials()
  else if (key === 'wirelog') { loadWlNodes(); loadComresResources() }
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
  resolveLegacyRoute()
  if (me.value) {
    active.value = 'devices'
    loadDashboard()
    loadVersionInfo()
    loadHealthInfo()
    loadProductTypes()
    loadBuildServers()
    loadDevices()
    loadComresResources()
  }
}
onMounted(() => {
  boot()
  window.addEventListener('hashchange', () => { resolveLegacyRoute() })
})

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
    active.value = 'devices'
    loadDashboard()
    loadDevices()
  } catch (e) { loginError.value = e.message }
}

async function doLogout() {
  try { await api('/api/auth/logout', { method: 'POST' }) } catch (e) { /* ignore */ }
  me.value = null
  active.value = 'devices'
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
    type_code: row.type_code, display_name: row.display_name || '',
    has_screen: !!row.has_screen, has_led: !!row.has_led,
    serial_count: row.serial_count === undefined || row.serial_count === null ? 1 : row.serial_count,
    adb_port: row.adb_port || 5555, model_pattern: row.model_pattern || '',
    defconfig: row.defconfig || '', sdk_repo_cmd: row.sdk_repo_cmd || '',
    build_cmd: row.build_cmd || '', image_path: row.image_path || '',
    jenkins_url: row.jenkins_url || '', notes: row.notes || '',
    chip_model: row.chip_model || '', float_abi: row.float_abi || 'auto', has_fpu: !!row.has_fpu,
    build_server_id: row.build_server_id || null,
    new_type_code: row.type_code,
  }
  ptEditDialog.value = true
}
async function savePtEdit() {
  const newCode = (ptEditRow.value.new_type_code || '').trim()
  if (!newCode) { ElMessage.warning('type_code 不能为空'); return }
  try {
    const r = await api('/api/product-types/' + encodeURIComponent(ptEditRow.value.type_code), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        new_type_code: newCode,
        display_name: ptEditRow.value.display_name,
        has_screen: ptEditRow.value.has_screen, has_led: ptEditRow.value.has_led,
        serial_count: ptEditRow.value.serial_count, adb_port: ptEditRow.value.adb_port,
        model_pattern: ptEditRow.value.model_pattern,
        defconfig: ptEditRow.value.defconfig, sdk_repo_cmd: ptEditRow.value.sdk_repo_cmd,
        build_cmd: ptEditRow.value.build_cmd, image_path: ptEditRow.value.image_path,
        jenkins_url: ptEditRow.value.jenkins_url, notes: ptEditRow.value.notes,
        chip_model: ptEditRow.value.chip_model, float_abi: ptEditRow.value.float_abi,
        has_fpu: ptEditRow.value.has_fpu, build_server_id: ptEditRow.value.build_server_id,
      }),
    })
    const movedDev = (r && r.moved_devices) || 0
    const movedJob = (r && r.moved_flash_jobs) || 0
    const movedQueue = (r && r.moved_queue_entries) || 0
    const movedParts = []
    if (movedDev) { movedParts.push(movedDev + ' 台设备') }
    if (movedQueue) { movedParts.push(movedQueue + ' 条排队') }
    if (movedJob) { movedParts.push(movedJob + ' 条刷机记录') }
    ElMessage.success(
      movedParts.length
        ? ('已保存（' + r.complete_label + '），type_code 已改为 ' + r.type_code + '，同步调整 ' + movedParts.join('、'))
        : ('已保存（' + r.complete_label + '）')
    )
    ptEditDialog.value = false
    loadProductTypes(); loadSettings()
    if (movedDev) { loadDevices() }
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
    adb_port: 5555, model_pattern: '',
    defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', jenkins_url: '', notes: '',
    chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null, copy_from: '',
  }
  ptAllowIncomplete.value = false
  ptCreateTitle.value = '新增机型'
  ptCreateForm.value?.clearValidate?.()
}
function openPtCreate() {
  resetPtCreate()
  ptCreateDialog.value = true
}
// v0.28.7：派生机型 —— 以某机型为模板克隆一行（工程配置/芯片浮点/编译服务器全部带过来），
// 只换 type_code 与名称；不搬运任何设备，原设备仍留在模板机型上。
// 两个入口：机型列表行「派生」、每台设备操作列「派生」（该设备当前机型为模板）。
async function openPtDerive(row, fromDevice) {
  const code = fromDevice ? (row.model_class || '') : (row.type_code || '')
  if (!code) { ElMessage.warning('该设备还没有机型，无法派生机型'); return }
  // v0.29.3：直接向服务端要模板的完整配置（含镜像/编译/芯片/编译服务器），
  // 不再依赖页面内存里的列表快照，保证派生弹窗一打开就带全，只需改差异项。
  let tpl = null
  try {
    tpl = await api('/api/product-types/' + encodeURIComponent(code))
  } catch (e) {
    tpl = productTypes.value.find((p) => p.type_code === code) || null
  }
  if (!tpl) { ElMessage.warning('机型 ' + code + ' 不在当前列表，请刷新后重试'); return }
  resetPtCreate()
  ptCreateRow.value = {
    type_code: code + '_copy',
    display_name: (tpl.display_name || code) + '-副本',
    has_screen: !!tpl.has_screen, has_led: !!tpl.has_led,
    serial_count: tpl.serial_count === undefined || tpl.serial_count === null ? 1 : tpl.serial_count,
    adb_port: tpl.adb_port || 5555, model_pattern: tpl.model_pattern || '',
    defconfig: tpl.defconfig || '', sdk_repo_cmd: tpl.sdk_repo_cmd || '',
    build_cmd: tpl.build_cmd || '', image_path: tpl.image_path || '',
    jenkins_url: tpl.jenkins_url || '', notes: tpl.notes || '',
    chip_model: tpl.chip_model || '', float_abi: tpl.float_abi || 'auto',
    has_fpu: !!tpl.has_fpu, build_server_id: tpl.build_server_id || null,
    copy_from: code,
  }
  ptCreateTitle.value = '派生机型（模板 ' + code + '）'
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
        adb_port: row.adb_port, model_pattern: row.model_pattern,
        copy_from: row.copy_from || null,
        defconfig: row.defconfig, sdk_repo_cmd: row.sdk_repo_cmd, build_cmd: row.build_cmd,
        image_path: row.image_path, jenkins_url: row.jenkins_url, notes: row.notes,
        chip_model: row.chip_model, float_abi: row.float_abi, has_fpu: row.has_fpu,
        build_server_id: row.build_server_id,
      }),
    })
    // 派生：后端已在一次请求里整行复制（含工程字段），不用再 PUT；普通新增仍走两步。
    if (!row.copy_from) {
      await api('/api/product-types/' + encodeURIComponent(code), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          defconfig: row.defconfig, sdk_repo_cmd: row.sdk_repo_cmd, build_cmd: row.build_cmd,
          image_path: row.image_path, jenkins_url: row.jenkins_url, notes: row.notes,
          chip_model: row.chip_model, float_abi: row.float_abi, has_fpu: row.has_fpu,
          build_server_id: row.build_server_id,
        }),
      })
    }
    ElMessage.success(row.copy_from
      ? ('已派生机型 ' + code + '（模板 ' + row.copy_from + '）')
      : ('已新增机型 ' + code))
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
      <!-- 设备中心（默认页；旧看板路由已重定向至此） -->
      <div v-if="active === 'devices'">
        <div class="device-status-bar">
          <span>
            上次全量巡检 {{ dashboard && dashboard.last_patrol ? formatLocal(dashboard.last_patrol.ended_at) : '—' }}
            · 最近快轮询 {{ dashboard && dashboard.last_recover_patrol ? formatLocalHm(dashboard.last_recover_patrol.ended_at) : '—' }}
          </span>
          <el-button v-if="me.role === 'admin'" size="small" link type="primary" :loading="patrolBusy" @click="triggerPatrolNow">立即巡检</el-button>
          <el-button v-else size="small" link type="primary" :loading="patrolBusy" @click="triggerPatrolNow">刷新状态</el-button>
        </div>
        <!-- 统计卡：≥1200→4列 / ≥900→3列 / &lt;700→2列；7 张末行居中避免孤卡 -->
        <div class="device-stats-grid">
          <el-card shadow="hover" class="ov-card" :class="{ 'is-active': statsFilter === 'all' }" @click="toggleStatsFilter('all')">
            <div class="ov-label">设备总量</div>
            <div class="ov-num">{{ deviceTotals.total }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="hover" class="ov-card" :class="{ 'is-active': statsFilter === 'online' }" @click="toggleStatsFilter('online')">
            <div class="ov-label">在线</div>
            <div class="ov-num ov-online">{{ deviceTotals.online }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="hover" class="ov-card" :class="{ 'is-active': statsFilter === 'offline' }" @click="toggleStatsFilter('offline')">
            <div class="ov-label">离线</div>
            <div class="ov-num ov-offline">{{ deviceTotals.offline }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="hover" class="ov-card" :class="{ 'is-active': statsFilter === 'pending' }" @click="toggleStatsFilter('pending')">
            <div class="ov-label">待检</div>
            <div class="ov-num ov-pending">{{ deviceTotals.pending }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="hover" class="ov-card" :class="{ 'is-active': statsFilter === 'occupied' }" @click="toggleStatsFilter('occupied')">
            <div class="ov-label">已分配</div>
            <div class="ov-num ov-allocated">{{ deviceTotals.occupied }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="hover" class="ov-card" :class="{ 'is-active': statsFilter === 'available' }" @click="toggleStatsFilter('available')">
            <div class="ov-label">空闲</div>
            <div class="ov-num ov-idle">{{ deviceTotals.available }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="hover" class="ov-card ov-card-comres" @click="switchPage('wirelog')">
            <div class="ov-label">串口资源</div>
            <div class="ov-num">{{ comresPortStats.total }}</div>
            <div class="ov-sub">在池 {{ comresPortStats.pool }}</div>
          </el-card>
        </div>
        <div v-if="productChips.length" class="product-chips">
          <button
            v-for="c in productChips"
            :key="c.type"
            type="button"
            class="product-chip"
            :class="{ 'is-active': selectedDeviceType === c.type }"
            @click="applyProductChip(c.type)"
          >{{ c.label }}</button>
        </div>
        <!-- 概况 -->
        <div v-if="!selectedDeviceType">
          <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-top: 8px">
            <div style="display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap">
              <h2 style="margin: 0">设备中心</h2>
              <span style="color: #909399; font-size: 12px">登记新设备：探测 → 确认注册（两步）</span>
            </div>
            <el-button v-if="me.role === 'admin'" type="primary" @click="openDeviceAdd('')">＋ 添加设备</el-button>
            <el-button v-else type="primary" plain disabled style="cursor: not-allowed">＋ 添加设备（需管理员）</el-button>
          </div>

          <p style="color: #909399; margin: 12px 0 8px">共 {{ deviceOverview.length }} 个品类，点击卡片或上方机型 chip 查看明细。</p>
          <el-row :gutter="12">
            <el-col v-for="g in deviceOverview" :key="g.type" :span="8" :xs="24" :sm="12" :md="8" style="margin-bottom: 12px">
              <el-card shadow="hover" class="cat-card" @click="openDeviceType(g.type)">
                <div style="display: flex; align-items: center; justify-content: space-between">
                  <span style="font-weight: 700; font-size: 15px">{{ g.label || g.type }}</span>
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
          <el-empty v-if="!deviceOverview.length" description="还没有设备，先登记一台吧">
            <el-button v-if="me.role === 'admin'" type="primary" @click="openDeviceAdd('')">＋ 添加设备</el-button>
            <span v-else style="color: #909399; font-size: 12px">添加设备需要管理员权限，请联系管理员登记</span>
          </el-empty>
        </div>

        <!-- 品类详情 -->
        <div v-else>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <el-button size="small" @click="backToOverview">返回概况</el-button>
            <h2 style="margin: 0">{{ detailTitle }}</h2>
            <el-tag size="small" type="info">共 {{ detailCounts.all }} 台</el-tag>
            <el-button size="small" plain :loading="cfgAuditBusy" @click="showConfigAudit">配置体检</el-button>
            <el-button v-if="me.role === 'admin'" type="primary" style="margin-left: auto"
              @click="openDeviceAdd(prefillType)">＋ 添加 {{ detailTitle }} 设备</el-button>
            <el-button v-else type="primary" plain disabled style="margin-left: auto; cursor: not-allowed">＋ 添加设备（需管理员）</el-button>
          </div>
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin: 10px 0">
            <el-tabs v-model="detailStatusTab" style="flex: 1; min-width: 280px" @tab-change="onDetailStatusTabChange">
              <el-tab-pane :label="'全部(' + detailCounts.all + ')'" name="all" />
              <el-tab-pane :label="'占用(' + detailCounts.occupied + ')'" name="occupied" />
              <el-tab-pane :label="'可用(' + detailCounts.available + ')'" name="available" />
              <el-tab-pane :label="'离线(' + detailCounts.offline + ')'" name="offline" />
              <el-tab-pane :label="'待检(' + detailCounts.pending + ')'" name="pending" />
            </el-tabs>
            <div style="display: flex; align-items: center; gap: 8px">
              <el-button v-if="me.role === 'admin'" size="small" :loading="probeBatchBusy === 'pending'"
                :disabled="!detailCounts.pending" @click="probeBatch('pending')">探测待检({{ detailCounts.pending }})</el-button>
              <el-button v-if="me.role === 'admin'" size="small" :loading="probeBatchBusy === 'offline'"
                :disabled="!detailCounts.offline" @click="probeBatch('offline')">探测离线({{ detailCounts.offline }})</el-button>
              <el-input v-model="deviceSearch" placeholder="搜索 ID / 名称 / 位置 / 责任人 / ADB" clearable style="width: 280px" />
              <span v-if="deviceSearch.trim()" class="search-match">匹配 {{ detailFilteredDevices.length }} 台</span>
            </div>
          </div>
          <div class="device-table-wrap">
          <el-table v-if="detailFilteredDevices.length" :data="detailDevices" border size="small" class="device-table" height="min(70vh, 640px)" style="min-width: 1545px" @sort-change="onDeviceSortChange">
            <el-table-column label="操作" width="260">
              <template #default="{ row }">
                <el-button v-if="canEditDevice(row)" link type="primary" size="small"
                  :loading="probeBusy === row.device_id" @click="probeOne(row)">探测</el-button>
                <el-button v-if="canEditDevice(row)" link type="primary" size="small" @click="openDeviceEdit(row)">编辑</el-button>
                <el-button v-if="me.role === 'admin'" link type="primary" size="small" :loading="screenshotBusy === row.device_id" @click="takeScreenshot(row)">截图</el-button>
                <el-button v-if="me.role === 'admin'" link size="small" @click="openScreenshotHistory(row)">记录</el-button>
                <el-button link type="primary" size="small" @click="openDeviceWlBindings(row)">串口</el-button>
                <el-button v-if="canEditDevice(row)" link type="danger" size="small" @click="removeDevice(row)">移除</el-button>
              </template>
            </el-table-column>
            <el-table-column prop="device_id" label="ID" width="115" show-overflow-tooltip />
            <el-table-column prop="display_name" label="名称" width="130" show-overflow-tooltip />
            <el-table-column prop="position_code" label="位置" width="85" show-overflow-tooltip />
            <el-table-column prop="owner" label="责任人" width="85" show-overflow-tooltip />
            <el-table-column prop="register_agent" label="Agent" width="115" show-overflow-tooltip>
              <template #default="{ row }">{{ row.register_agent || '-' }}</template>
            </el-table-column>
            <el-table-column prop="health_level" label="健康" width="55" />
            <el-table-column label="配置" width="120">
              <template #default="{ row }">
                <el-tooltip v-if="row.config && row.config.state === 'fingerprinted'"
                  :content="cfgTooltip(row.config)" placement="top">
                  <el-tag size="small" :type="cfgTagType(row.config.verdict)">
                    {{ row.config.defconfig_short || '?' }}
                  </el-tag>
                </el-tooltip>
                <el-tooltip v-else-if="row.config && row.config.state === 'legacy'"
                  content="能连但无 build-info（老固件）" placement="top">
                  <el-tag size="small" type="info" effect="plain">老固件</el-tag>
                </el-tooltip>
                <span v-else style="color: #c0c4cc">-</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTag[row.status] || 'info'">{{ row.status }}</el-tag>
                <el-tooltip v-if="row.locked" :content="'已锁定' + (row.locked_reason ? '：' + row.locked_reason : '') + (row.locked_by ? '（' + row.locked_by + '）' : '')" placement="top">
                  <el-tag size="small" type="warning" style="margin-left: 4px">锁定</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="占用者" width="135" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.holder_email">{{ row.holder_email }}</span>
                <span v-else style="color: #c0c4cc">-</span>
              </template>
            </el-table-column>
            <el-table-column prop="adb_address" label="ADB" width="135" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.adb_address }}</span></template>
            </el-table-column>
            <el-table-column label="共享" width="90">
              <template #default="{ row }">
                <el-tooltip v-if="row.share_scope === 'restricted'"
                  :content="(row.allowed_emails || []).concat(row.allowed_agent_labels || []).join('，') || '受限'" placement="top">
                  <el-tag size="small" type="warning">受限</el-tag>
                </el-tooltip>
                <el-tag v-else-if="row.share_scope === 'private'" size="small" type="danger">独享</el-tag>
                <el-tag v-else size="small" type="success">公共</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近探测" prop="last_probe_at" width="110" sortable="custom">
              <template #default="{ row }">
                <el-tooltip :content="deviceProbeAt(row) ? formatLocal(deviceProbeAt(row)) : '从未探测'" placement="top">
                  <span>{{ formatRelative(deviceProbeAt(row)) }}</span>
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
          </div>
          <el-empty v-if="!detailFilteredDevices.length" :description="emptyDetailText">
              <el-button
                v-if="detailStatusTab !== 'all' || deviceSearch.trim()"
                size="small"
                type="primary"
                plain
                @click="showAllInDetail"
              >查看全部 {{ detailCounts.all }} 台</el-button>
              <el-button
                v-if="me.role === 'admin' && !detailCounts.all"
                type="primary"
                style="margin-left: 8px"
                @click="openDeviceAdd(prefillType)"
              >{{ prefillType ? '＋ 添加 ' + prefillType + ' 设备' : '＋ 添加设备' }}</el-button>
            </el-empty>
          <div v-if="detailFilteredDevices.length" class="detail-pager">
            <span class="detail-range">共 {{ detailFilteredDevices.length }} 台，第 {{ detailRangeStart }}-{{ detailRangeEnd }} 台</span>
            <el-pagination
              v-model:current-page="detailPage"
              v-model:page-size="detailPageSize"
              :page-sizes="[25, 50, 100]"
              :total="detailFilteredDevices.length"
              layout="sizes, prev, pager, next, jumper"
              background
            />
          </div>
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
          <el-table-column label="到期" width="200"><template #default="{ row }">{{ formatLocal(row.expires_at) }}</template></el-table-column>
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
          <el-table-column label="入队时间" width="200"><template #default="{ row }">{{ formatLocal(row.joined_at) }}</template></el-table-column>
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
          <el-table-column label="最后在线" width="200"><template #default="{ row }">{{ formatLocal(row.last_seen_at) }}</template></el-table-column>
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
          <el-table-column label="登记时间" width="200"><template #default="{ row }">{{ formatLocal(row.created_at) }}</template></el-table-column>
        </el-table>
        <h3>通知流水</h3>
        <el-table :data="notices" border>
          <el-table-column label="时间" width="200"><template #default="{ row }">{{ formatLocal(row.created_at) }}</template></el-table-column>
          <el-table-column prop="event_type" label="类型" width="160" />
          <el-table-column prop="title" label="标题" />
          <el-table-column prop="severity" label="级别" width="90" />
        </el-table>
        <h3>最近巡检</h3>
        <el-table :data="patrolRuns" border>
          <el-table-column label="时间" width="200"><template #default="{ row }">{{ formatLocal(row.ended_at) }}</template></el-table-column>
          <el-table-column label="模式" width="90">
            <template #default="{ row }">{{ row.mode === 'recover' ? '快轮询' : '全量' }}</template>
          </el-table-column>
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
              <el-table-column label="到期时间" width="200"><template #default="{ row }">{{ formatLocal(row.expires_at) }}</template></el-table-column>
              <el-table-column label="创建时间" width="200"><template #default="{ row }">{{ formatLocal(row.created_at) }}</template></el-table-column>
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
              <el-table-column label="到期时间" width="200"><template #default="{ row }">{{ formatLocal(row.expires_at) }}</template></el-table-column>
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

      <!-- COMRes：节点视图 + 资源池（原 WireLog tab，key 仍为 wirelog） -->
      <div v-else-if="active === 'wirelog'">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:12px;flex-wrap:wrap">
          <h2 style="margin:0">COMRes</h2>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <el-tag
              size="small"
              :type="comresAuthPendingCount > 0 ? 'warning' : 'info'"
              style="cursor:pointer"
              @click="openComresAuthDrawer"
            >待审批 {{ comresAuthPendingCount }} 条</el-tag>
            <span v-if="comresAuthLastApprovedAt" class="comres-muted" style="font-size:12px">
              上次批准 {{ formatLocal(comresAuthLastApprovedAt) }}
            </span>
            <el-button size="small" type="primary" plain @click="openComresAuthDrawer">授权</el-button>
          </div>
        </div>
        <el-alert v-if="wlNodesError" type="error" :closable="false" show-icon
                  :title="'节点列表：' + wlNodesError" style="margin-bottom:10px" />
        <el-alert v-if="comresError" type="error" :closable="false" show-icon
                  :title="'资源池：' + comresError" style="margin-bottom:10px" />
        <el-alert v-if="wlDetailError" type="error" :closable="false" show-icon
                  :title="'节点详情：' + wlDetailError" style="margin-bottom:10px" />

        <!-- 节点视图 -->
        <el-card v-if="!wlDetail" style="margin-bottom:16px">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>节点视图（/api/wirelog/nodes）</span>
              <el-button size="small" @click="loadWlNodes">刷新</el-button>
            </div>
          </template>
          <el-table :data="wlNodes" stripe
                    :row-class-name="wlNodeRowClass"
                    @row-click="r => openWlDetail(r.node_id)">
            <el-table-column prop="node_id" label="node_id" min-width="180" />
            <el-table-column prop="display_name" label="名称" min-width="140" />
            <el-table-column prop="ip" label="IP" min-width="130" />
            <el-table-column label="授权" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="wlAuthBadge(wlNodeAuthStatus(row)).type">
                  {{ wlAuthBadge(wlNodeAuthStatus(row)).label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="wlStatusType(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="在线端口数" width="110">
              <template #default="{ row }">{{ wlOnlinePortCount(row.node_id) }}</template>
            </el-table-column>
            <el-table-column label="串口资源" min-width="160">
              <template #default="{ row }">
                <template v-if="!wlNodeAuthorized(row)">
                  <span class="comres-muted">未授权，点右上角授权</span>
                </template>
                <template v-else-if="!wlNodeHasSerial(row.node_id)">
                  <span class="comres-muted">无串口资源</span>
                </template>
                <span v-else>{{ wlNodeSerialRows(row.node_id).length }} 路</span>
              </template>
            </el-table-column>
            <el-table-column prop="model_class" label="机型" width="110" />
            <el-table-column prop="sw_version" label="版本" width="90" />
            <el-table-column label="最近心跳" min-width="190"><template #default="{ row }">{{ formatLocal(row.last_heartbeat_at) }}</template></el-table-column>
            <el-table-column label="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status !== 'retired'" size="small"
                           type="warning" plain
                           @click.stop="wlRetire(row)">退役</el-button>
                <el-button v-if="row.status === 'retired' && me.role === 'admin'" size="small"
                           type="danger"
                           @click.stop="wlDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <p class="comres-muted" style="font-size:12px;margin:12px 0 0">
            新节点请由客户端自助申请（deskserialhub）；token 在节点详情中查看/复制。
          </p>
        </el-card>

        <el-card v-else style="margin-bottom:16px">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>节点详情：{{ wlDetail.node_id }}</span>
              <el-button size="small" @click="closeWlDetail">返回列表</el-button>
            </div>
          </template>

          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="名称">{{ wlDetail.display_name }}</el-descriptions-item>
            <el-descriptions-item label="状态">
              <el-tag size="small" :type="wlStatusType(wlDetail.status)">{{ wlDetail.status }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="IP">{{ wlDetail.ip }}</el-descriptions-item>
            <el-descriptions-item label="版本">{{ wlDetail.sw_version }}</el-descriptions-item>
            <el-descriptions-item label="node_token">
              <span class="comres-muted">{{ wlDetail.node_token_masked || (wlDetail.token_prefix ? wlDetail.token_prefix + '…' : '—') }}</span>
              <el-button size="small" link type="primary" style="margin-left:8px"
                         :disabled="!wlDetail.node_token"
                         @click="copyNodeToken">复制</el-button>
            </el-descriptions-item>
            <el-descriptions-item label="最近心跳">{{ wlDetail.last_heartbeat_at }}</el-descriptions-item>
            <el-descriptions-item label="在线端口数">{{ wlOnlinePortCount(wlDetail.node_id) }}</el-descriptions-item>
          </el-descriptions>
          <el-alert type="info" :closable="false" show-icon style="margin:12px 0 0"
                    title="节点凭据由客户端自助申请签发；详情可复制 node_token。宿主自行 register/heartbeat。" />

          <h3>端口明细</h3>
          <el-alert v-if="!(wlDetail.ports && wlDetail.ports.length)"
                    type="info" :closable="false" show-icon
                    title="无串口资源" class="comres-muted" style="margin-bottom:8px" />
          <el-table :data="wlDetail.ports || []" border size="small"
                    :row-class-name="({ row }) => portHasSerial(row) ? '' : 'comres-no-serial'">
            <el-table-column label="com" min-width="100">
              <template #default="{ row }">
                <span :class="{ 'comres-muted': !portHasSerial(row) }">{{ row.com_name || row.com || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="slot" label="slot" width="60" />
            <el-table-column label="endpoint" min-width="160" show-overflow-tooltip>
              <template #default="{ row }">
                <span :class="{ 'comres-muted': !portHasSerial(row) }">{{ row.endpoint || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="health_state" width="110">
              <template #default="{ row }">{{ row.health_state || '—' }}</template>
            </el-table-column>
            <el-table-column label="allocated" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="portAllocated(row) ? 'warning' : 'info'">
                  {{ portAllocated(row) ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="allocated_by" min-width="120" show-overflow-tooltip>
              <template #default="{ row }">{{ portAllocatedBy(row) || '—' }}</template>
            </el-table-column>
            <el-table-column label="占用/池" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="!portHasSerial(row)" class="comres-muted">无串口资源</span>
                <span v-else>{{ portOccupancyLabel(row) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="bound device" min-width="160">
              <template #default="{ row }">
                <el-button v-if="row.bound_device_id" link type="primary"
                           @click="jumpToDevice(row.bound_device_id)">
                  {{ row.bound_device ? (row.bound_device.name + ' (' + row.bound_device.status + ')') : row.bound_device_id }}
                </el-button>
                <span v-else class="comres-muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="sn" min-width="120" show-overflow-tooltip>
              <template #default="{ row }">{{ portSn(row) || '—' }}</template>
            </el-table-column>
            <el-table-column label="mac" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">{{ portMac(row) || '—' }}</template>
            </el-table-column>
            <el-table-column prop="tty" label="tty" min-width="100" />
            <el-table-column prop="baud" label="波特" width="80" />
            <el-table-column label="写授权" min-width="110">
              <template #default="{ row }">
                <span v-if="row.write_granted_to">{{ row.write_granted_to }}</span>
                <span v-else class="comres-muted">只读</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="180" fixed="right">
              <template #default="{ row }">
                <template v-if="portCanOpen(row)">
                  <el-button size="small" type="primary" link @click="openWlShell(row)">打开串口</el-button>
                </template>
                <span v-else class="comres-muted">无串口资源</span>
                <el-button size="small" link type="danger"
                  @click="wlRevoke(row.slot)" :disabled="!row.write_granted_to">撤销写</el-button>
              </template>
            </el-table-column>
          </el-table>

          <el-row :gutter="16" style="margin-top:16px">
            <el-col :span="12">
              <h3>下发配置（下次心跳生效）</h3>
              <el-form label-width="140" size="small">
                <el-form-item label="心跳周期（秒）">
                  <el-select v-model="wlConfigForm.heartbeat_interval_s" style="width:160px">
                    <el-option :value="30" label="30 秒" />
                    <el-option :value="60" label="60 秒" />
                    <el-option :value="120" label="120 秒" />
                  </el-select>
                </el-form-item>
                <el-form-item label="日志级别">
                  <el-select v-model="wlConfigForm.log_level" style="width:160px">
                    <el-option value="debug" label="debug" />
                    <el-option value="info" label="info" />
                    <el-option value="warn" label="warn" />
                    <el-option value="error" label="error" />
                  </el-select>
                </el-form-item>
                <el-form-item label="启用保活探测">
                  <el-switch v-model="wlConfigForm.keepalive_enabled" />
                </el-form-item>
                <el-form-item label="保活周期（秒）">
                  <el-select v-model="wlConfigForm.keepalive_interval_s" style="width:160px">
                    <el-option :value="30" label="30 秒" />
                    <el-option :value="60" label="60 秒" />
                    <el-option :value="120" label="120 秒" />
                  </el-select>
                </el-form-item>
                <el-form-item label="一致性检测周期（秒）">
                  <el-select v-model="wlConfigForm.consistency_interval_s" style="width:160px">
                    <el-option :value="300" label="300 秒" />
                    <el-option :value="600" label="600 秒" />
                    <el-option :value="900" label="900 秒" />
                  </el-select>
                </el-form-item>
                <el-form-item label="轮换本地串口 token">
                  <el-switch v-model="wlConfigForm.rotate_local_token" />
                </el-form-item>
              </el-form>
              <el-button size="small" type="primary"
                         @click="saveWlConfig">保存并下发</el-button>
            </el-col>
            <el-col :span="12">
              <h3>写授权</h3>
              <el-form label-width="70">
                <el-form-item label="路 slot">
                  <el-input-number v-model="wlGrantForm.slot" :min="1" />
                </el-form-item>
                <el-form-item label="授权对象">
                  <el-input v-model="wlGrantForm.principal"
                            placeholder="principal / lease 标识" />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" @click="wlGrant">授予写权限</el-button>
                </el-form-item>
              </el-form>
            </el-col>
          </el-row>
        </el-card>

        <!-- 资源池视图：/api/comres/resources → allocated / bound / pool / inactive -->
        <el-card>
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
              <span>资源池视图（/api/comres/resources）
                <el-tag size="small" type="warning" style="margin-left:8px">已分配 {{ comresAllocated.length }}</el-tag>
                <el-tag size="small" type="success" style="margin-left:4px">已绑定 {{ comresBound.length }}</el-tag>
                <el-tag size="small" type="info" style="margin-left:4px">池 {{ comresPool.length }}</el-tag>
                <el-tag size="small" type="info" effect="plain" style="margin-left:4px">失效 {{ comresInactive.length }}</el-tag>
              </span>
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                <el-switch v-model="comresShowInactiveOnly" size="small"
                           inline-prompt active-text="重复/失效" inactive-text="全部" />
                <el-button size="small" type="warning" plain :loading="comresPruneBusy"
                           @click="runComresPrune">清理无效资源</el-button>
                <el-button size="small" :loading="comresLoading" @click="loadComresResources">刷新资源</el-button>
              </div>
            </div>
          </template>
          <el-table :data="comresAllRows" border size="small" stripe v-loading="comresLoading"
                    :row-class-name="comresRowClass" max-height="480">
            <el-table-column prop="node_id" label="node_id" min-width="150" show-overflow-tooltip />
            <el-table-column prop="node_ip" label="IP" width="120" />
            <el-table-column label="com" min-width="100">
              <template #default="{ row }">
                <span :class="{ 'comres-muted': !portHasSerial(row) || row.inactive }">{{ row.com || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="slot" label="slot" width="60" />
            <el-table-column label="endpoint" min-width="160" show-overflow-tooltip>
              <template #default="{ row }">
                <span :class="{ 'comres-muted': !portHasSerial(row) || row.inactive }">{{ row.endpoint || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="health_state" label="health_state" width="110" />
            <el-table-column label="原因" min-width="110">
              <template #default="{ row }">
                <span v-if="row.inactive" class="comres-muted">{{ comresInactiveLabel(row) }}</span>
                <span v-else class="comres-muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="allocated" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.allocated ? 'warning' : 'info'">
                  {{ row.allocated ? '是' : '否' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="allocated_by" min-width="120" show-overflow-tooltip>
              <template #default="{ row }">{{ row.allocated_by || '—' }}</template>
            </el-table-column>
            <el-table-column label="占用/池" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.inactive" class="comres-muted">{{ comresInactiveLabel(row) }}</span>
                <span v-else-if="!portHasSerial(row)" class="comres-muted">无串口资源</span>
                <span v-else>{{ comresOccupancyLabel(row) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="allocated_expire" min-width="170">
              <template #default="{ row }">
                {{ row.allocated && row.allocated_expire ? formatLocal(row.allocated_expire) : '—' }}
              </template>
            </el-table-column>
            <el-table-column label="bound device" min-width="140">
              <template #default="{ row }">
                <el-button v-if="row.bound_device_id || row.device_id" link type="primary"
                           @click="jumpToDevice(row.bound_device_id || row.device_id)">
                  {{ row.bound_device_id || row.device_id }}
                </el-button>
                <span v-else class="comres-muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="sn" min-width="100">
              <template #default="{ row }">{{ row.sn || '—' }}</template>
            </el-table-column>
            <el-table-column label="mac" min-width="120">
              <template #default="{ row }">{{ row.mac || '—' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="120" fixed="right">
              <template #default="{ row }">
                <el-button v-if="portCanOpen(row)" size="small" type="primary" link
                           @click="openWlShell(row)">打开串口</el-button>
                <span v-else class="comres-muted">无串口资源</span>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!comresAllRows.length && !comresLoading && !comresError"
                    description="暂无 COMRes 资源" />
          <p class="comres-muted" style="font-size:12px;margin:8px 0 0">
            可分配池仅含已授权节点端口；待审批 / 已拒绝 / 已撤销节点的端口不入池，避免误领。
          </p>
        </el-card>

        <el-drawer v-model="comresAuthDrawer" title="COMRes 授权" size="520px" direction="rtl">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
            <div>
              <div style="font-weight:600;margin-bottom:4px">自动批准</div>
              <div class="comres-muted" style="font-size:12px">关闭后客户端申请进入待审，需人工批准</div>
            </div>
            <el-switch :model-value="comresAutoApprove" @change="toggleComresAutoApprove" />
          </div>
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
            <el-radio-group v-model="comresAuthFilter" size="small" @change="loadComresApplications">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="pending">待审</el-radio-button>
              <el-radio-button value="approved">已授权</el-radio-button>
              <el-radio-button value="rejected">已拒绝</el-radio-button>
              <el-radio-button value="revoked">已撤销</el-radio-button>
            </el-radio-group>
            <el-button size="small" :loading="comresAuthLoading" @click="loadComresApplications">刷新</el-button>
          </div>
          <el-alert v-if="comresAuthError" type="error" :closable="false" show-icon
                    :title="comresAuthError" style="margin-bottom:10px" />
          <div v-loading="comresAuthLoading">
            <el-empty v-if="!comresAuthItems.length && !comresAuthLoading && !comresAuthError"
                      description="暂无申请记录" />
            <div v-for="row in comresAuthItems" :key="row.request_id" class="comres-auth-card">
              <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
                <div>
                  <div style="font-weight:600">{{ row.display_name || '未命名' }}</div>
                  <div class="comres-muted" style="font-size:12px;margin-top:2px">
                    client {{ shortClientId(row.client_id) }}
                    · IP {{ row.source_ip || '—' }}
                    · {{ row.model_class || '机型未知' }}
                  </div>
                  <div class="comres-muted" style="font-size:12px;margin-top:2px">
                    首次 {{ formatLocal(row.first_seen_at) }} · 申请 {{ row.request_count }} 次
                  </div>
                </div>
                <el-tag size="small" :type="wlAuthBadge(row.status).type">
                  {{ wlAuthBadge(row.status).label }}
                </el-tag>
              </div>
              <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
                <template v-if="row.status === 'pending'">
                  <el-button size="small" type="primary"
                             :loading="comresAuthBusyId === row.request_id"
                             @click="comresApprove(row)">批准</el-button>
                  <el-button size="small" type="danger" plain
                             :loading="comresAuthBusyId === row.request_id"
                             @click="comresReject(row)">拒绝</el-button>
                </template>
                <el-button v-if="row.status === 'approved'" size="small" type="warning" plain
                           :loading="comresAuthBusyId === row.request_id"
                           @click="comresRevoke(row)">撤销</el-button>
                <span v-if="row.status === 'rejected'" class="comres-muted" style="font-size:12px">
                  原因：{{ row.reject_reason || '—' }}
                </span>
              </div>
            </div>
          </div>
        </el-drawer>
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
              <el-form-item label="探测后端">
                <el-select v-model="settingsForm.probe_backend" style="width: 160px">
                  <el-option label="real（真实 ADB 探测）" value="real" />
                  <el-option label="mock（模拟设备，仅测试）" value="mock" />
                </el-select>
                <span style="margin-left: 8px; color: #909399">mock 只用于模拟设备；真实设备用 mock 会一直判 L0</span>
              </el-form-item>
              <el-form-item label="巡检间隔（分钟）">
                <el-input-number v-model="settingsForm.patrol_interval_minutes" :min="1" :max="1440" />
              </el-form-item>
              <el-form-item label="待检设备探测间隔（分钟）">
                <el-input-number v-model="settingsForm.pending_patrol_interval_minutes" :min="1" :max="1440" />
                <span style="margin-left: 8px; color: #909399">仅探测 pending 设备；申请无可用设备时还会立即补探一次</span>
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
              <el-table-column label="操作" width="330" fixed="right">
                <template #default="{ row }">
                  <template v-if="me.role === 'admin'">
                    <el-button size="small" @click="openPtEdit(row)">编辑</el-button>
                    <el-button size="small" @click="openPtDerive(row, false)">派生</el-button>
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
              <el-table-column label="创建时间" width="200"><template #default="{ row }">{{ formatLocal(row.created_at) }}</template></el-table-column>
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
              <el-table-column label="时间" width="200"><template #default="{ row }">{{ formatLocal(row.created_at) }}</template></el-table-column>
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
              <el-table-column label="时间" width="170"><template #default="{ row }">{{ formatLocal(row.created_at) }}</template></el-table-column>
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
            <el-form-item label="type_code">
              <el-input v-model="ptEditRow.new_type_code" placeholder="机型主键，可直接修改（会同步已有设备的机型）" />
            </el-form-item>
            <el-form-item label="名称"><el-input v-model="ptEditRow.display_name" placeholder="展示名（给人看），留空则用 type_code" /></el-form-item>
            <el-form-item label="defconfig"><el-input v-model="ptEditRow.defconfig" placeholder="project_moto_ld_defconfig" /></el-form-item>
            <el-form-item label="主芯片型号"><el-input v-model="ptEditRow.chip_model" placeholder="如 Cortex-A7（探测可自动回填）" /></el-form-item>
            <el-form-item label="有屏"><el-switch v-model="ptEditRow.has_screen" /></el-form-item>
            <el-form-item label="有灯"><el-switch v-model="ptEditRow.has_led" /></el-form-item>
            <el-form-item label="串口数"><el-input-number v-model="ptEditRow.serial_count" :min="0" /></el-form-item>
            <el-form-item label="ADB 端口"><el-input-number v-model="ptEditRow.adb_port" :min="1" :max="65535" /></el-form-item>
            <el-form-item label="机型匹配正则"><el-input v-model="ptEditRow.model_pattern" placeholder="留空则不按型号自动归档" /></el-form-item>
            <el-form-item label="编译服务器">
              <el-select v-model="ptEditRow.build_server_id" clearable placeholder="不绑定" style="width: 100%">
                <el-option v-for="b in buildServers" :key="b.server_id" :label="b.name + '（' + b.host + '）'" :value="b.server_id" />
              </el-select>
            </el-form-item>
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
              <el-input v-model="deviceAddConfirm.position_code" placeholder="物理位置，如 302-R2-05（同一位置可放多台设备）" />
              <div class="form-hint">同一位置可放多台设备；填自己的名字也行，不会自动加序号。「个人独享」只决定谁能申请，不影响位置。</div>
            </el-form-item>
            <el-form-item label="负责人" required>
              <el-input v-model="deviceAddConfirm.owner" placeholder="维护责任人" />
            </el-form-item>
            <el-form-item label="机型">
              <el-alert
                v-if="!deviceAddConfirm.model_class"
                type="warning" :closable="false" show-icon style="margin-bottom: 8px"
                :title="deviceAddProbe?.probe?.model
                  ? ('探测到机型 ' + deviceAddProbe.probe.model + ' 未注册，请先新增/派生，或从下拉改选已有机型')
                  : '未自动探测到设备类型，请从已注册机型中选择'"
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
                  default-first-option
                  :placeholder="deviceAddAgentOptions.length ? '按所选用户的有效凭据勾选，或手动输入' : '所选用户暂无有效凭据，可手动输入 Agent 名'"
                  style="width: 100%">
                  <el-option v-for="a in deviceAddAgentOptions" :key="a" :label="a" :value="a" />
                </el-select>
                <div class="form-hint">根据所选用户的有效 token 自动列出；只有一个 Agent 时已自动填入。</div>
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
          <el-form-item label="上次探测" class="settings-grid-full">
            <span style="color: #606266">
              {{ deviceEditRow.last_probe_at ? formatLocal(deviceEditRow.last_probe_at) : '从未' }}
              <span v-if="deviceProbeSource(deviceEditRow)" style="color: #909399; margin-left: 6px">
                （来源：{{ deviceProbeSource(deviceEditRow) }}）
              </span>
            </span>
          </el-form-item>
          <el-form-item label="名称"><el-input v-model="deviceEditRow.display_name" /></el-form-item>
          <el-form-item label="责任人"><el-input v-model="deviceEditRow.owner" /></el-form-item>
          <el-form-item label="位置">
            <el-input v-model="deviceEditRow.position_code" />
            <div class="form-hint">同一位置可放多台设备。</div>
          </el-form-item>
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
                default-first-option
                :placeholder="deviceEditAgentOptions.length ? '按所选用户的有效凭据勾选，或手动输入' : '所选用户暂无有效凭据，可手动输入 Agent 名'"
                style="width: 100%">
                <el-option v-for="a in deviceEditAgentOptions" :key="a" :label="a" :value="a" />
              </el-select>
              <div class="form-hint">根据所选用户的有效 token 自动列出；只有一个 Agent 时已自动填入。</div>
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

      <!-- P9 全局弹窗：设备的 WireLog 串口接入情况 -->
      <el-dialog v-model="wlBindDlg.visible"
                 :title="'串口接入 · ' + (wlBindDlg.device ? (wlBindDlg.device.display_name || wlBindDlg.device.device_id) : '')"
                 width="860px">
        <el-table :data="wlBindDlg.rows" border size="small" v-loading="wlBindDlg.loading" max-height="440">
          <el-table-column label="日志机/路" min-width="180">
            <template #default="{ row }">
              <el-button link type="primary" @click="jumpToWlNode(row.node_id)">
                {{ row.node_display || row.node_id }} · 路{{ row.slot }}
              </el-button>
            </template>
          </el-table-column>
          <el-table-column prop="tty" label="tty" width="120" show-overflow-tooltip />
          <el-table-column label="串口就绪" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.shell_reachable ? 'success' : 'info'">
                {{ row.shell_reachable ? '可达' : '不可达' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="shell_mac" label="实测 MAC" width="150" />
          <el-table-column prop="shell_ip" label="实测 IP" width="130" />
          <el-table-column prop="write_granted_to" label="写授权" min-width="120" show-overflow-tooltip>
            <template #default="{ row }">{{ row.write_granted_to || '只读' }}</template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!wlBindDlg.rows.length && !wlBindDlg.loading" description="该设备当前未绑定任何 WireLog 串口" />
        <template #footer><el-button @click="wlBindDlg.visible = false">关闭</el-button></template>
      </el-dialog>
    <el-dialog v-model="ptCreateDialog" :title="ptCreateTitle" width="860px" :close-on-click-modal="false">
          <el-alert v-if="ptCreateRow.copy_from" type="info" :closable="false" show-icon style="margin-bottom:10px"
            :title="'已从模板 ' + ptCreateRow.copy_from + ' 完整复制配置（工程命令 / 镜像路径 / 芯片浮点 / 编译服务器 / 固件策略），只需修改有差异的项'" />
          <el-form ref="ptCreateForm" :model="ptCreateRow" :rules="ptCreateRules" label-width="96px" class="pt-grid">
            <el-form-item label="type_code" prop="type_code"><el-input v-model="ptCreateRow.type_code" placeholder="如 moto_ld" /></el-form-item>
            <el-form-item label="名称"><el-input v-model="ptCreateRow.display_name" placeholder="展示名，留空则用 type_code" /></el-form-item>
            <el-form-item label="有屏"><el-switch v-model="ptCreateRow.has_screen" /></el-form-item>
            <el-form-item label="有灯"><el-switch v-model="ptCreateRow.has_led" /></el-form-item>
            <el-form-item label="串口数"><el-input-number v-model="ptCreateRow.serial_count" :min="0" /></el-form-item>
            <el-form-item label="ADB 端口"><el-input-number v-model="ptCreateRow.adb_port" :min="1" :max="65535" /></el-form-item>
            <el-form-item label="机型匹配正则"><el-input v-model="ptCreateRow.model_pattern" placeholder="留空则不按型号自动归档" /></el-form-item>
            <el-form-item label="编译服务器">
              <el-select v-model="ptCreateRow.build_server_id" clearable placeholder="不绑定" style="width: 100%">
                <el-option v-for="b in buildServers" :key="b.server_id" :label="b.name + '（' + b.host + '）'" :value="b.server_id" />
              </el-select>
            </el-form-item>
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
            <el-button type="primary" :loading="ptCreateSaving" @click="submitPtCreate">{{ ptCreateRow.copy_from ? '派生' : '新增' }}</el-button>
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
.version-foot { padding: 8px 16px; font-size: 11px; color: #b0b3b8; text-align: center; user-select: none; }
.title-version { margin-left: 6px; font-size: 11px; font-weight: 400; color: #909399; }
.device-status-bar {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  background: #f5f7fa; border: 1px solid #ebeef5; border-radius: 6px;
  padding: 6px 12px; margin-bottom: 8px; color: #909399; font-size: 12px; line-height: 1.5;
}
/* 统计卡栅格：<700→2列 / ≥900→3列 / ≥1200→4列；justify center 让末行 4+3 / 3+3+1 不孤左 */
.device-stats-grid {
  --ov-gap: 12px;
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  align-items: stretch;
  gap: var(--ov-gap);
  margin: 0 0 10px;
}
.device-stats-grid > .ov-card {
  /* 默认 <700：一行 2 张 */
  flex: 0 0 calc((100% - var(--ov-gap)) / 2);
  max-width: calc((100% - var(--ov-gap)) / 2);
  box-sizing: border-box;
}
@media (min-width: 900px) {
  .device-stats-grid > .ov-card {
    flex-basis: calc((100% - 2 * var(--ov-gap)) / 3);
    max-width: calc((100% - 2 * var(--ov-gap)) / 3);
  }
}
@media (min-width: 1200px) {
  .device-stats-grid > .ov-card {
    flex-basis: calc((100% - 3 * var(--ov-gap)) / 4);
    max-width: calc((100% - 3 * var(--ov-gap)) / 4);
  }
}
.ov-card {
  --el-card-bg-color: #1f2937;
  --el-card-border-color: #374151;
  cursor: pointer; transition: box-shadow .2s, border-color .2s, transform .2s;
  border-radius: 8px; background: #1f2937; border: 2px solid #374151; color: #e5e7eb;
  height: 100%; min-height: 100px;
}
.ov-card :deep(.el-card__body) {
  padding: 14px 16px; background: transparent; height: 100%; box-sizing: border-box;
  display: flex; flex-direction: column; justify-content: flex-start;
}
.ov-card:hover { box-shadow: 0 6px 18px rgba(0, 0, 0, .28); border-color: #60a5fa; transform: translateY(-2px); }
.ov-card.is-active {
  border-color: #409eff;
  box-shadow: 0 0 0 1px #409eff, 0 4px 14px rgba(64, 158, 255, .35);
}
.ov-label { color: #9ca3af; font-size: 13px; line-height: 1.3; }
.ov-num {
  font-size: 28px; line-height: 1.15; margin-top: 8px; font-weight: 700; color: #f3f4f6;
  font-variant-numeric: tabular-nums; letter-spacing: -0.02em;
}
.ov-sub { margin-top: auto; padding-top: 4px; font-size: 12px; color: #9ca3af; line-height: 1.3; min-height: 1.3em; }
.ov-sub-spacer { visibility: hidden; }
.ov-online { color: #67c23a; }
.ov-offline { color: #f89898; }
.ov-pending { color: #e6a23c; }
.ov-allocated { color: #409eff; }
.ov-idle { color: #a3e635; }
.product-chips {
  display: flex; flex-wrap: nowrap; gap: 8px; overflow-x: auto; padding: 2px 0 8px;
  -webkit-overflow-scrolling: touch;
}
.product-chip {
  flex: 0 0 auto; border: 1px solid #dcdfe6; background: #fff; color: #606266;
  border-radius: 999px; padding: 4px 12px; font-size: 12px; cursor: pointer; line-height: 1.4;
}
.product-chip:hover { border-color: #409eff; color: #409eff; }
.product-chip.is-active { border-color: #409eff; background: #ecf5ff; color: #409eff; }
.cat-card { cursor: pointer; transition: box-shadow .2s, border-color .2s, transform .2s; }
.cat-card:hover { box-shadow: 0 6px 18px rgba(64, 158, 255, .18); border-color: #409eff; transform: translateY(-2px); }
.search-match { color: #409eff; font-size: 13px; white-space: nowrap; }
.detail-pager { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 12px; }
.detail-range { color: #909399; font-size: 13px; }
.shot-download { display: inline-block; text-decoration: none; }
.shot-preview { width: 100%; height: 52vh; display: block; }
.device-table { font-size: 12px; }
/* v0.28.1：状态标签在表格重建时可能卡在 transition 起始态（opacity 0 / scaleX 0），
   导致状态列看起来空白。这里兜底强制可见，动画只影响观感、不影响判读。 */
.el-tag {
  opacity: 1 !important;
  transform: none !important;
}

.device-table-wrap { width: 100%; overflow-x: auto; }
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
.form-hint { font-size: 12px; color: #909399; line-height: 1.5; margin-top: 4px; }
/* COMRes：无串口资源 / 失效资源灰显 */
.comres-muted { color: #c0c4cc; }
.comres-no-serial { color: #c0c4cc; }
.comres-no-serial .cell { color: #c0c4cc; }
.comres-unauthorized { color: #c0c4cc; }
.comres-unauthorized .cell { color: #c0c4cc; }
.comres-inactive { color: #c0c4cc; }
.comres-inactive .cell { color: #c0c4cc; }
.comres-auth-card {
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 10px;
  background: var(--el-fill-color-blank, #fff);
}

</style>
