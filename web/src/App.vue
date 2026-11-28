<script setup>
import { ref, reactive, computed, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  SLOT_MAX as PR_SLOT_MAX,
  collectModelClasses,
  aggregatePowerBoundCounts,
  filterBindTargets,
} from './powerresBindTargets.js'

const pages = [
  { key: 'devices', label: '设备中心' },
  { key: 'scheduling', label: '资源调度' },
  { key: 'onboarding', label: '接入申请', admin: true },
  { key: 'credentials', label: '凭据' },
  { key: 'ctrlres', label: 'CtrlRes' },
  { key: 'settings', label: '设置' },
]
const active = ref('devices')
const LEGACY_BOARD_KEYS = new Set(['dashboard', 'board', '看板'])
/** PT3b：设备独立详情页 #/device/{id} */
const devicePageId = ref('')
const devicePage = ref(null)          // GET /api/devices/{id}
const devicePageLoading = ref(false)
const devicePageError = ref('')       // 404 / 网络等
const deviceCaps = ref({ enabled: true, items: [], revision: 0 })
const deviceCapsLoading = ref(false)
const deviceSerial = ref(null)        // GET /api/devices/{id}/serial
const deviceSerialLoading = ref(false)
const devicePowerBindings = ref([])   // 本设备电源绑定行
const devicePowerLoading = ref(false)
const devicePowerAlert = ref('')      // 解绑成功等提示
/** PT4a：详情页节点归属 / 可绑定通道 */
const devicePowerNodeId = ref('')           // 推断或手选的 node_id
const devicePowerNodeSource = ref('')       // serial | binding | manual | ''
const devicePowerNodePicking = ref(false)   // 显示节点下拉（强制选 / 重新选择）
const devicePowerInventory = ref([])        // 当前节点 inventory
const devicePowerInvLoading = ref(false)
const devicePowerBindBusy = ref('')         // 绑定中行键 power_dev::channel
const devicePowerCandOpen = ref(['candidates']) // 可绑定区块默认展开
const devicePowerConfigOpen = ref([]) // 电源配置默认折叠（仅 admin）
const deviceActionBusy = reactive({}) // actionKey|bindingKey → bool
const deviceActionFail = reactive({}) // key → { phase, code, message }
const deviceHoldMs = ref(3000)
/** PT10a：ADB 卡内调试区（切设备清空） */
const adbPanelOpen = ref(false)
const adbCmd = ref('')
const adbOut = ref(null) // { result, rc, stdout, stderr, duration_ms, truncated }
const adbBusy = ref(false)
const adbErr = ref('')
const ADB_QUICK_CMDS = [
  { label: 'id', cmd: 'id' },
  { label: 'model', cmd: 'getprop ro.product.model' },
  { label: 'battery', cmd: 'dumpsys battery' },
]
/** PT6a：设备中心列表「电源」popover 缓存（按设备只拉一次 capabilities） */
const listPowerCapsCache = reactive({})   // device_id → { items, loading, error, loaded }
const listPowerHoldMs = ref(3000)
const settingsTab = ref('system')
const schedulingTab = ref('leases')
/** CtrlRes 卡片：comres | powerres；默认 COMRes */
const ctrlresCard = ref('comres')
/** COMRes 子页签：nodes | pool | settings；默认节点视图 */
const comresTab = ref('nodes')
const COMRES_TABS = [
  { key: 'nodes', label: '节点视图' },
  { key: 'pool', label: '资源池视图' },
  { key: 'bundles', label: '设备资源包' },
  { key: 'settings', label: '设置' },
]

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
const deviceAddConfirm = ref({ position_code: '', owner: '', model_class: '', display_name: '', note: '', share_scope: 'shared', allowed_emails: [], allowed_agent_labels: [] })
// v0.29.2 指定名单「允许 Agent」：按邮箱从有效凭据拉取可选项
const deviceAddAgentInfo = ref({})
const deviceAddAgentSeen = ref(new Set())
const deviceAddAgentAuto = ref(new Set())
// 设备发现模式
const deviceAddMode = ref('ip')
const discoveryNetworks = ref('')
const discovering = ref(false)
const discoveryResults = ref([])
// UART（串口）接入：从未绑定的串口资源直接建一台「串口为主通道」的设备
const serialAddResources = ref([])
const serialAddLoading = ref(false)
const serialAddPick = ref(null)
const serialProbingKey = ref('')

function openDeviceAdd(presetModel) {
  // v0.29.2：打开即刷新机型字典，避免刚派生/新增的机型在下拉里看不到
  // 品类详情页进入时带 presetModel，弹窗里直接预选该品类
  const preset = presetModel || ''
  loadProductTypes()
  deviceAddStep.value = 1
  deviceAddMode.value = 'ip'
  deviceAddProbe.value = null
  deviceAddForm.value = { ip: '', port: 5555, hint_model: preset }
  deviceAddConfirm.value = { position_code: '', owner: '', model_class: preset, display_name: '', note: '', share_scope: 'shared', allowed_emails: [], allowed_agent_labels: [] }
  deviceAddAgentInfo.value = {}
  deviceAddAgentSeen.value = new Set()
  deviceAddAgentAuto.value = new Set()
  discoveryNetworks.value = ''
  discoveryResults.value = []
  serialAddResources.value = []
  serialAddPick.value = null
  deviceAddVisible.value = true
}

async function loadSerialAddResources() {
  serialAddLoading.value = true
  try {
    const r = await api('/api/wirelog/serial-resources')
    serialAddResources.value = (r.resources || []).filter((x) => !x.bound_device_id)
  } catch (e) { ElMessage.error('读取串口资源失败：' + String(e.message || e)) }
  finally { serialAddLoading.value = false }
}

function serialIdentity(row) {
  const p = row.probe || {}
  const ident = p.identity || {}
  return {
    mac: (ident.mac || row.shell_mac || '').toLowerCase(),
    sn: ident.sn || '',
    ip: ident.ip || row.shell_ip || '',
    model: (p.system || {}).project || (p.system || {}).model || '',
    verdict: p.verdict || '',
  }
}

function serialReady(row) {
  return row.node_status === 'online'
    && (!!row.shell_reachable || ['online', 'probed'].includes(String(row.health_state || '').toLowerCase()))
}

function serialQualified(row) {
  return serialLevel(row) === 'L3'
}

function serialLevel(row) {
  const p = row.probe || {}
  const id = (p.identity || {})
  if (p.verdict === 'manual_probe' && (p.probe || {}).level === 'L3'
      && (id.mac || id.sn || id.ip)) {
    return 'L3'
  }
  return serialReady(row) ? 'L2' : 'L0'
}

async function probeSerialResource(row) {
  const key = row.node_id + ':' + row.slot
  if (serialProbingKey.value) return
  serialProbingKey.value = key
  try {
    const r = await api('/api/wirelog/nodes/' + row.node_id + '/slots/' + row.slot
      + '/probe-serial', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    if (r.level === 'L3') {
      ElMessage.success('探测成功 L3：MAC ' + (r.identity?.mac || '—')
        + ' / SN ' + (r.identity?.sn || '—') + ' / IP ' + (r.identity?.ip || '—'))
    } else if (r.level === 'L2') {
      ElMessage.warning('串口有响应，但没取到 MAC/SN/IP（L2）；不允许添加，请检查设备或补身份')
    } else {
      ElMessage.error('探测失败：没有收到串口响应（L0）')
    }
    await loadSerialAddResources()
  } catch (e) {
    ElMessage.error('手动探测失败：' + String(e.message || e))
  } finally { serialProbingKey.value = '' }
}

function onSerialRowClick(row) {
  if (!serialQualified(row)) {
    ElMessage.info('该串口未达 L3，先点「探测」补齐指纹')
    return
  }
  pickSerialResource(row)
}

function pickSerialResource(row) {
  if (!serialQualified(row)) {
    ElMessage.warning('该串口未达 L3（需可达且取到 MAC/SN/IP），不能添加')
    return
  }
  const id = serialIdentity(row)
  serialAddPick.value = row
  deviceAddProbe.value = {
    serial: true,
    probe: {
      level: 'L3',
      mac: id.mac, sn: id.sn, model: id.model,
      endpoint: row.endpoint, com: row.com_name || row.tty_name,
      node: row.node_name || row.node_id, slot: row.slot,
      reachable: true,
    },
    identity_match: { result: 'new' },
  }
  const model = enabledProductTypes.value.some((t) => t.type_code === id.model) ? id.model : ''
  deviceAddConfirm.value = {
    position_code: '', owner: '', model_class: model, display_name: '',
    note: (row.com_name || row.tty_name || ('COM' + row.slot)) + '@' + (row.node_name || row.node_id),
    share_scope: 'shared', allowed_emails: [], allowed_agent_labels: [],
  }
  deviceAddAgentInfo.value = {}
  deviceAddAgentSeen.value = new Set()
  deviceAddAgentAuto.value = new Set()
  deviceAddStep.value = 2
}

function onDeviceAddModeChange(mode) {
  if (mode === 'serial') loadSerialAddResources()
}

async function runDiscovery() {  discovering.value = true
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
    display_name: '',
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

async function submitSerialDevice() {
  if (!serialAddPick.value) { ElMessage.error('请选择一路串口'); return }
  if (!deviceAddConfirm.value.position_code.trim()) { ElMessage.error('请填写位置'); return }
  if (!deviceAddConfirm.value.owner.trim()) { ElMessage.error('请填写负责人'); return }
  deviceAddSaving.value = true
  try {
    const id = serialIdentity(serialAddPick.value)
    const r = await api('/api/wirelog/nodes/' + serialAddPick.value.node_id
      + '/slots/' + serialAddPick.value.slot + '/create-device', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        owner: deviceAddConfirm.value.owner,
        position_code: deviceAddConfirm.value.position_code,
        display_name: deviceAddConfirm.value.display_name || null,
        model_class: deviceAddConfirm.value.model_class || null,
        mac: id.mac || null, sn: id.sn || null, ip: id.ip || null,
        share_scope: deviceAddConfirm.value.share_scope || 'shared',
        allowed_emails: deviceAddConfirm.value.allowed_emails || [],
        allowed_agent_labels: deviceAddConfirm.value.allowed_agent_labels || [],
      }),
    })
    ElMessage.success(r.result === 'existing'
      ? '该串口已归到现有设备 ' + r.device_id
      : '设备已从串口创建：' + r.device_id)
    deviceAddVisible.value = false
    loadDevices()
    loadComresBundles().catch(() => {})
  } catch (e) { ElMessage.error(e.message) } finally { deviceAddSaving.value = false }
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
        display_name: deviceAddConfirm.value.display_name || null,
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
const ptEditRow = ref({
  type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1, adb_port: 5555,
  model_pattern: '', defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', notes: '',
  chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null, new_type_code: '',
  flash_method: 'db', feature_filter: [], db_project_name: '', db_features: [], db_probing: false,
})

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
  defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', notes: '',
  chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null, copy_from: '',
  flash_method: 'db', feature_filter: [], db_project_name: '', db_features: [], db_probing: false,
})
const ptCreateTitle = ref('新增机型')
const ptCreateRules = computed(() => {
  const req = { required: true, message: '该字段默认必填（可勾选底部「允许不完整」放开）', trigger: 'blur' }
  return ptAllowIncomplete.value
    ? { type_code: [{ required: true, message: 'type_code 必填', trigger: 'blur' }] }
    : {
        type_code: [{ required: true, message: 'type_code 必填', trigger: 'blur' }],
        defconfig: [req], sdk_repo_cmd: [req], build_cmd: [req],
        image_path: [req],
      }
})
function parseFeatureFilter(raw) {
  if (Array.isArray(raw)) return raw.map((x) => String(x).trim()).filter(Boolean)
  if (raw == null || raw === '') return []
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) return parsed.map((x) => String(x).trim()).filter(Boolean)
    } catch (e) { /* 失败回退空数组 */ }
    return []
  }
  return []
}
function ptFlashPayload(row) {
  const dbName = (row.db_project_name || '').trim()
  const ff = Array.isArray(row.feature_filter) ? row.feature_filter.filter(Boolean) : []
  return {
    flash_method: row.flash_method || 'db',
    db_project_name: dbName || null,
    feature_filter: ff.length ? JSON.stringify(ff) : null,
  }
}
async function probeDbFeatures(targetRow) {
  const spec = (targetRow.db_project_name || '').trim()
  if (!spec) { ElMessage.warning('请先填写 DB 路径'); return }
  targetRow.db_probing = true
  try {
    const r = await api('/api/product-types/db-features?spec=' + encodeURIComponent(spec))
    if (r && r.error) {
      ElMessage.error(r.error)
      targetRow.db_features = []
      return
    }
    targetRow.db_features = (r && Array.isArray(r.features)) ? r.features : []
    ElMessage.success('探测到 ' + targetRow.db_features.length + ' 个候选特征')
  } catch (e) {
    ElMessage.error(e.message)
    targetRow.db_features = []
  } finally {
    targetRow.db_probing = false
  }
}

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
    if (d.message) return d.code ? (d.code + ': ' + d.message) : d.message
    if (d.detail && typeof d.detail === 'string') return d.detail
    try { return JSON.stringify(d) } catch (e) { return String(status) }
  }
  return String(d)
}

async function api(path, opts) {
  const r = await fetch(apiUrl(path), { credentials: 'include', ...opts })
  if (!r.ok) {
    let detail = String(r.status)
    let body = null
    try {
      const j = await r.json()
      body = j
      detail = apiErrorDetail(j, r.status)
    } catch (e) { /* ignore */ }
    if (r.status === 401 && me.value && !path.startsWith('/api/auth/')) me.value = null
    const err = new Error(detail)
    err.status = r.status
    err.path = path
    const d = body && body.detail
    if (d && typeof d === 'object') {
      err.code = d.code || null
      err.detail = d.detail || null
      err.apiMessage = d.message || null
    }
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
/** COMRes 心跳年龄：38 秒前 / 4 分钟前 / 1 小时前 */
function formatHeartbeatAge(row) {
  let sec = row && row.heartbeat_age_s
  if (sec == null && row && row.last_heartbeat_at) {
    const d = parseUtcDate(row.last_heartbeat_at)
    if (d) sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  }
  if (sec == null || Number.isNaN(sec)) return '—'
  sec = Math.max(0, Math.floor(sec))
  if (sec < 60) return `${sec} 秒前`
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`
  return `${Math.floor(sec / 86400)} 天前`
}
function wlHeartbeatTimedOut(row) {
  if (!row) return false
  if (row.status === 'stale' || row.status === 'offline') return true
  if (row.heartbeat_age_s != null && row.heartbeat_timeout_s != null) {
    return Number(row.heartbeat_age_s) > Number(row.heartbeat_timeout_s)
  }
  return false
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
function syncCtrlresHash() {
  if (active.value !== 'ctrlres') return
  const want = ctrlresCard.value === 'powerres' ? '#/ctrlres?card=powerres' : '#/ctrlres'
  if (String(window.location.hash || '') !== want) {
    history.replaceState(null, '', window.location.pathname + window.location.search + want)
  }
}

function loadComresForCurrentTab() {
  if (comresTab.value === 'settings') {
    loadPackages()
    loadComresAuthSummary()
  } else if (comresTab.value === 'pool') {
    loadComresResources()
  } else if (comresTab.value === 'bundles') {
    loadComresBundles()
  } else {
    loadWlNodes()
    loadComresResources()
  }
}

function enterComRes() {
  ctrlresCard.value = 'comres'
  loadComresForCurrentTab()
  syncCtrlresHash()
}

function onCtrlresCardChange(name) {
  const card = name === 'powerres' ? 'powerres' : 'comres'
  ctrlresCard.value = card
  syncCtrlresHash()
  if (card === 'powerres') enterPowerRes()
  else enterComRes()
}

/** 解析 hash：board 遗留 → 设备中心；wirelog/powerres/ctrlres → CtrlRes 卡片；device/{id} → 详情 */
function resolveLegacyRoute() {
  try {
    const hashRaw = String(window.location.hash || '')
    const withoutHash = hashRaw.replace(/^#\/?/, '')
    const qIdx = withoutHash.indexOf('?')
    const pathPart = qIdx >= 0 ? withoutHash.slice(0, qIdx) : withoutHash
    const hashPath = pathPart.toLowerCase()
    const qs = new URLSearchParams(qIdx >= 0 ? withoutHash.slice(qIdx + 1) : '')
    const path = String(window.location.pathname || '').replace(/^\//, '').split('/')[0]
    const key = (hashPath || path || '').toLowerCase()

    // PT3b：#/device/{id}（保留原始大小写）
    const deviceMatch = pathPart.match(/^device\/(.+)$/i)
    if (deviceMatch) {
      let id = deviceMatch[1] || ''
      try { id = decodeURIComponent(id) } catch (e) { /* keep raw */ }
      id = String(id || '').trim()
      if (id) {
        enterDevicePage(id)
        return true
      }
    }

    if (LEGACY_BOARD_KEYS.has(key) || LEGACY_BOARD_KEYS.has(hashPath) || LEGACY_BOARD_KEYS.has(path)) {
      if (window.location.hash && /board|dashboard/i.test(window.location.hash)) {
        history.replaceState(null, '', window.location.pathname + window.location.search)
      }
      devicePageId.value = ''
      if (active.value !== 'devices') {
        switchPage('devices')
      } else {
        active.value = 'devices'
      }
      return true
    }

    // CtrlRes 深链 + 旧 hash 兼容：#/wirelog → COMRes；#/powerres → PowerRes；#/ctrlres?card=
    if (hashPath === 'wirelog' || hashPath === 'powerres' || hashPath === 'ctrlres') {
      let card = 'comres'
      if (hashPath === 'powerres') card = 'powerres'
      else if (hashPath === 'ctrlres' && qs.get('card') === 'powerres') card = 'powerres'
      ctrlresCard.value = card
      devicePageId.value = ''
      if (active.value !== 'ctrlres') {
        switchPage('ctrlres')
      } else {
        syncCtrlresHash()
        if (card === 'powerres') enterPowerRes()
        else enterComRes()
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

async function deleteFeedback(row) {
  try {
    await ElMessageBox.confirm(
      '确认删除这条无效反馈？删除后不可恢复。',
      '删除 Agent 意见',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch (e) { return }
  try {
    await api('/api/feedback/' + row.feedback_id, { method: 'DELETE' })
    ElMessage.success('已删除')
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
})
// /api/comres/resources → { allocated, bound, pool, inactive }
const comresAllocated = ref([])
const comresBound = ref([])
const comresPool = ref([])
const comresInactive = ref([])
const comresReachSummary = ref({ total: 0, ok: 0, fail: 0 })
const comresError = ref('')
const comresLoading = ref(false)
const comresReachBusy = ref(false)
const comresShowInactiveOnly = ref(false)
const comresPruneBusy = ref(false)

// 设备资源包（v0.42）：ADB + 串口成套
const bundleData = ref(null)      // { devices, unattached_serial, summary }
const bundleError = ref('')
const bundleLoading = ref(false)
const bundleOnlySerial = ref(true)  // 默认只显示有串口的设备

// 串口恢复
const recoverDialog = ref(false)
const recoverDevice = ref(null)
const recoverChannel = ref(null)
const recoverLevel = ref('readonly')
const recoverBusy = ref(false)
const recoverResult = ref(null)

// t981：设备指纹抽屉
const fpDrawer = ref(false)
const fpDrawerTitle = ref('设备指纹')
const fpDrawerData = ref(null) // { fingerprint, fp_verdict, node_id, slot, com }
const fpDrawerLoading = ref(false)

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

// 客户端安装包（t976）
const packages = ref([])
const packagesLoading = ref(false)
const packagesError = ref('')
const packageUploadBusy = ref(false)
const packageForm = reactive({
  name: 'elsb',
  version: '',
  platform: 'Windows',
  channel: 'prod',
  notes: '',
  file: null,
})

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
function wlNodeAuthSource(row) {
  return (row && row.auth_source) || 'none'
}
function wlAuthSourceLabel(source) {
  if (source === 'token') return '预置 token'
  if (source === 'apply') return '自助申请'
  return ''
}
function wlNodeAuthorized(row) {
  const st = wlNodeAuthStatus(row)
  // 已批准；存量无申请记录（none）仍视为可用（与池口径一致）
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
/** TCP COM 主键：以槽位/端口号命名，不从 tty 名出发 */
function portTcpComLabel(row) {
  if (!row) return '—'
  if (row.slot != null) return 'TCP COM ' + row.slot
  if (row.tcp_port != null) return 'TCP COM ' + row.tcp_port
  return 'TCP COM'
}
function portTcpHost(row) {
  if (!row) return ''
  if (row.node_ip) return row.node_ip
  const ep = row.endpoint || ''
  const m = ep.match(/^tcp:\/\/([^:]+)/i)
  return m ? m[1] : ''
}
function portTcpPortNum(row) {
  if (!row) return null
  if (row.tcp_port != null) return row.tcp_port
  const ep = row.endpoint || ''
  const m = ep.match(/^tcp:\/\/[^:]+:(\d+)/i)
  return m ? Number(m[1]) : null
}
function portEndpointText(row) {
  const host = portTcpHost(row)
  const port = portTcpPortNum(row)
  if (host && port != null) return 'tcp://' + host + ':' + port
  return (row && (row.endpoint || row.com_key)) || '—'
}
/** 来源节点名：资源行里优先查节点台账，其次用当前详情 */
function comresNodeById(nodeId) {
  const list = (wlNodes && wlNodes.value) || []
  const hit = list.find((n) => n && n.node_id === nodeId)
  if (hit) return hit
  if (wlDetail.value && wlDetail.value.node_id === nodeId) return wlDetail.value
  return null
}
function portSourceName(row) {
  if (!row) return '—'
  const n = comresNodeById(row.node_id)
  return (n && (n.display_name || n.node_id)) || row.node_id || '—'
}
/** 浏览器可直接打开的串口控制台（serialhub 自带前端） */
function portConsoleUrl(row) {
  const host = portTcpHost(row)
  if (!host) return ''
  const hp = (row && row.node_http_port) || 8787
  return 'http://' + host + ':' + hp + '/'
}
async function openPortConsole(row) {
  const nodeId = (row && row.node_id) || wlDetailId.value
  if (!nodeId) {
    // 没有 node_id 时退回旧的裸地址（至少能打开，token 需手工填）
    const url = portConsoleUrl(row)
    if (!url) { ElMessage.warning('该端口未上报节点地址，无法打开控制台'); return }
    window.open(url, '_blank')
    return
  }
  // 先开空白页，避免 await 之后被浏览器当弹窗拦截
  const win = window.open('about:blank', '_blank')
  try {
    const q = new URLSearchParams({ node_id: nodeId })
    if (row && row.slot != null) q.set('slot', row.slot)
    const r = await api('/api/comres/console-url?' + q.toString())
    if (!r || !r.url) throw new Error('节点未返回控制台地址')
    win.location.href = r.url
  } catch (e) {
    if (win) win.close()
    ElMessage.error('打开控制台失败：' + String(e.message || e))
  }
}
function copyPortEndpoint(row) {
  const text = portEndpointText(row)
  if (!text || text === '—') return
  copyText(text, '已拷贝接入地址')
}
function gotoPortSource(row) {
  if (!row || !row.node_id) return
  if (wlDetailId.value === row.node_id) return
  openWlDetail(row.node_id)
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

/** 可达列展示：未测 / 可连 / 无路由|拒连|超时|错误码 */
function comresReachLabel(row) {
  if (!row || row.reach_ok == null || row.reach_checked_at == null) {
    return { text: '未测', ok: null, tip: '尚未做 TRES→endpoint 探测' }
  }
  if (row.reach_ok === true) {
    return { text: '✓ 可连', ok: true, tip: (row.endpoint || '') + ' ok' }
  }
  const err = row.reach_error || 'error'
  const map = {
    no_route: '✗ 无路由',
    refused: '✗ 拒连',
    timeout: '✗ 超时',
    dns: '✗ DNS',
  }
  const text = map[err] || ('✗ ' + err)
  const tip = (row.endpoint || '') + ' · ' + err
  return { text, ok: false, tip }
}

function portHasFingerprint(row) {
  return !!(row && row.fingerprint && typeof row.fingerprint === 'object')
}

function fpVerdictMeta(verdict) {
  const v = (verdict || '').toLowerCase()
  if (v === 'ok') return { text: 'ok', type: 'success' }
  if (v === 'partial') return { text: 'partial', type: 'warning' }
  if (v === 'failed') return { text: 'failed', type: 'danger' }
  return { text: verdict || '—', type: 'info' }
}

function fpPrimaryIface(fp) {
  if (!fp || typeof fp !== 'object') return null
  const ident = fp.identity || {}
  return ident.mac_iface || ident.ip_iface || null
}

function fpNetRows(fp) {
  if (!fp || !Array.isArray(fp.net)) return []
  const primary = fpPrimaryIface(fp)
  return fp.net.map((n) => ({
    ...n,
    primary: !!(primary && n && n.iface === primary),
  }))
}

async function openFingerprintDrawer(row) {
  if (!row) return
  const nodeId = row.node_id
  const slot = row.slot
  const titleBits = [
    slot != null ? 'TCP COM ' + slot : (row.com || row.com_name || ''),
    row.node_id,
  ].filter(Boolean)
  fpDrawerTitle.value = '设备指纹 · ' + titleBits.join(' · ')
  fpDrawerData.value = {
    node_id: nodeId,
    slot,
    com: row.com || row.com_name,
    fingerprint: row.fingerprint || null,
    fp_verdict: row.fp_verdict || (row.fingerprint && row.fingerprint.verdict) || null,
  }
  fpDrawer.value = true
  // 有 node/slot 时再拉一次单查，保证原样 JSON
  if (nodeId != null && slot != null && !portHasFingerprint(row)) {
    return
  }
  if (nodeId == null || slot == null) return
  fpDrawerLoading.value = true
  try {
    const r = await api(`/api/comres/ports/${encodeURIComponent(nodeId)}/${encodeURIComponent(slot)}/fingerprint`)
    if (r && r.fingerprint) {
      fpDrawerData.value = {
        ...fpDrawerData.value,
        fingerprint: r.fingerprint,
        fp_verdict: r.fp_verdict || (r.fingerprint && r.fingerprint.verdict) || null,
      }
    }
  } catch (e) {
    // 抽屉已用行内数据，单查失败不阻断
  } finally {
    fpDrawerLoading.value = false
  }
}

async function loadComresResources(retry = 2) {
  comresLoading.value = true
  comresError.value = ''
  try {
    const r = await api('/api/comres/resources')
    comresAllocated.value = Array.isArray(r && r.allocated) ? r.allocated : []
    comresBound.value = Array.isArray(r && r.bound) ? r.bound : []
    comresPool.value = Array.isArray(r && r.pool) ? r.pool : []
    comresInactive.value = Array.isArray(r && r.inactive) ? r.inactive : []
    const rs = (r && r.reach_summary) || {}
    comresReachSummary.value = {
      total: Number(rs.total) || 0,
      ok: Number(rs.ok) || 0,
      fail: Number(rs.fail) || 0,
    }
  } catch (e) {
    // 首屏偶发失败（会话/服务刚重启）不要留下"串口资源 0"的假象：退避重试一次
    if (retry > 0) {
      comresLoading.value = false
      setTimeout(() => loadComresResources(retry - 1), 1500)
      return
    }
    comresAllocated.value = []
    comresBound.value = []
    comresPool.value = []
    comresInactive.value = []
    comresReachSummary.value = { total: 0, ok: 0, fail: 0 }
    comresError.value = String(e.message || e)
    ElMessage.error('COMRes 资源加载失败：' + comresError.value)
  } finally {
    comresLoading.value = false
  }
}

async function loadComresBundles() {
  bundleLoading.value = true
  bundleError.value = ''
  try {
    bundleData.value = await api('/api/comres/bundles')
  } catch (e) {
    bundleData.value = null
    bundleError.value = String(e.message || e)
    ElMessage.error('设备资源包加载失败：' + bundleError.value)
  } finally {
    bundleLoading.value = false
  }
}

const bundleRows = computed(() => {
  const rows = (bundleData.value && bundleData.value.devices) || []
  const list = bundleOnlySerial.value ? rows.filter((x) => x.channels.serial.length) : rows
  return [...list].sort((a, b) => {
    const d = (b.channels.serial.length ? 1 : 0) - (a.channels.serial.length ? 1 : 0)
    if (d) return d
    return (a.identity.display_name || a.device_id).localeCompare(b.identity.display_name || b.device_id, 'zh')
  })
})

function bundleSerialEndpoint(ch) {
  return ch.endpoint || (ch.tcp_port ? `tcp://<节点IP>:${ch.tcp_port}` : '—')
}

function openRecover(device, ch) {
  recoverDevice.value = device
  recoverChannel.value = ch
  recoverLevel.value = 'readonly'
  recoverResult.value = null
  recoverDialog.value = true
}

async function runRecover() {
  if (!recoverDevice.value || !recoverChannel.value) return
  if (recoverLevel.value !== 'readonly') {
    try {
      await ElMessageBox.confirm(
        recoverLevel.value === 'reboot'
          ? '确定要整机重启？设备会断开数十秒。'
          : '确定重启 adbd？ADB 会短暂中断。',
        '串口恢复确认', { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' },
      )
    } catch (e) { return }
  }
  recoverBusy.value = true
  recoverResult.value = null
  try {
    recoverResult.value = await api('/api/comres/recover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        device_id: recoverDevice.value.device_id,
        level: recoverLevel.value,
        slot: recoverChannel.value.source.slot,
      }),
    })
    ElMessage.success(recoverResult.value.ok ? '串口恢复完成' : '已下发，但未确认完成，请看输出')
  } catch (e) {
    ElMessage.error('串口恢复失败：' + String(e.message || e))
  } finally {
    recoverBusy.value = false
  }
}

async function runComresReachability() {
  comresReachBusy.value = true
  try {
    const r = await api('/api/comres/reachability', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    const failN = Array.isArray(r && r.failed) ? r.failed.length : 0
    ElMessage.success(
      `可达探测完成：检查 ${r && r.checked != null ? r.checked : 0}，`
      + `成功 ${r && r.ok != null ? r.ok : 0}，失败 ${failN}`,
    )
    await loadComresResources()
  } catch (e) {
    ElMessage.error('可达探测失败：' + String(e.message || e))
  } finally {
    comresReachBusy.value = false
  }
}

function formatBytes(n) {
  const v = Number(n) || 0
  if (v < 1024) return v + ' B'
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB'
  return (v / (1024 * 1024)).toFixed(2) + ' MB'
}

function sha12(s) {
  return (s || '').slice(0, 12)
}

async function loadPackages() {
  packagesLoading.value = true
  packagesError.value = ''
  try {
    const r = await api('/api/packages')
    packages.value = Array.isArray(r && r.items) ? r.items : []
  } catch (e) {
    packages.value = []
    packagesError.value = String(e.message || e)
    ElMessage.error('安装包列表加载失败：' + packagesError.value)
  } finally {
    packagesLoading.value = false
  }
}

function onPackageFileChange(uploadFile) {
  packageForm.file = uploadFile && uploadFile.raw ? uploadFile.raw : null
  if (packageForm.file && !packageForm.version) {
    const m = String(packageForm.file.name || '').match(/(\d+\.\d+\.\d+)/)
    if (m) packageForm.version = m[1]
  }
}

async function uploadPackage() {
  if (!packageForm.file) {
    ElMessage.warning('请先选择安装包文件')
    return
  }
  if (!packageForm.version || !packageForm.name) {
    ElMessage.warning('请填写名称与版本号')
    return
  }
  packageUploadBusy.value = true
  try {
    const fd = new FormData()
    fd.append('file', packageForm.file)
    fd.append('name', packageForm.name)
    fd.append('version', packageForm.version)
    fd.append('platform', packageForm.platform)
    fd.append('channel', packageForm.channel)
    if (packageForm.notes) fd.append('notes', packageForm.notes)
    await api('/api/packages', { method: 'POST', body: fd })
    ElMessage.success('上传成功')
    packageForm.version = ''
    packageForm.notes = ''
    packageForm.file = null
    await loadPackages()
  } catch (e) {
    ElMessage.error('上传失败：' + String(e.message || e))
  } finally {
    packageUploadBusy.value = false
  }
}

async function deletePackage(row) {
  try {
    await ElMessageBox.confirm(
      `删除安装包 ${row.name} ${row.version}（${row.platform}）？`,
      '确认删除',
      { type: 'warning' },
    )
  } catch (e) { return }
  try {
    await api('/api/packages/' + row.id, { method: 'DELETE' })
    ElMessage.success('已删除')
    await loadPackages()
  } catch (e) {
    ElMessage.error('删除失败：' + String(e.message || e))
  }
}

function packageDownloadHref(row) {
  return apiUrl(row.download_url || ('/api/packages/' + row.id + '/download'))
}

function switchComresTab(key) {
  if (comresTab.value === key) return
  comresTab.value = key
  if (key === 'nodes') {
    loadWlNodes()
    loadComresResources()
  } else if (key === 'pool') {
    loadComresResources()
  } else if (key === 'bundles') {
    loadComresBundles()
  } else if (key === 'settings') {
    loadPackages()
    loadComresAuthSummary()
  }
}

async function copySha256(row) {
  try {
    await navigator.clipboard.writeText(row.sha256 || '')
    ElMessage.success('已复制完整 sha256')
  } catch (e) {
    ElMessage.error('复制失败')
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

// ---------------- PowerRes（电源通道绑定到被测设备） ----------------
const prNodeId = ref('')
const prNodes = ref([]) // overview：仅有 power 资源或绑定的节点
const prItems = ref([])
const prLoading = ref(false)
const prError = ref('')
const prUnsupported = ref(false) // 旧 TRES 无路由：404/405 降级
const PR_UNSUPPORTED_HINT = '当前 TRES 版本不支持 PowerRes（需 0.54+），请升级后再使用'
const PR_NO_NODES_HINT = '该 TRES 没有节点上报电源资源'
const prBindDlg = reactive({
  visible: false,
  busy: false,
  power_dev: '',
  name: '',
  com: '',
  channel: null,
  endpoint: '',
  device_id: '',
  label: '',
  slot: '',
  modelFilter: '',
  keyword: '',
  bindingsLoading: false,
  location: '',
  driver: '',
})
/** 当前节点 powerres bindings 按 device_id 聚合的已绑数 */
const prBindPowerCounts = ref({})
/** 通道级「重启测试」对话框（进程内单例：同一时刻只允许一条） */
const prRebootDlg = reactive({
  visible: false,
  row: null,
  power_dev: '',
  channel: null,
  node_id: '',
  hold_ms: 3000,
  interval_s: 30,
  repeat: 20,
  status: null,
  starting: false,
  stopping: false,
})
let prRebootPollTimer = null
function stopPrRebootPoll() {
  if (prRebootPollTimer) {
    clearInterval(prRebootPollTimer)
    prRebootPollTimer = null
  }
}
function startPrRebootPoll() {
  stopPrRebootPoll()
  prRebootPollTimer = setInterval(async () => {
    try {
      const st = await api('/api/powerres/reboot-test/status')
      prRebootDlg.status = st
      if (!st || !st.running) stopPrRebootPoll()
    } catch (e) {
      stopPrRebootPoll()
    }
  }, 2000)
}
const prRebootEstimatedS = computed(() => {
  const n = Number(prRebootDlg.repeat) || 0
  const itv = Number(prRebootDlg.interval_s) || 0
  const hold = (Number(prRebootDlg.hold_ms) || 0) / 1000
  return Math.round(n * (hold + 2 + itv))
})
const prRebootEstimatedText = computed(() => {
  const t = prRebootEstimatedS.value
  if (!t) return '—'
  const m = Math.floor(t / 60)
  const s = t % 60
  return m > 0 ? (m + ' 分 ' + s + ' 秒') : (s + ' 秒')
})
const prRebootStatusText = computed(() => {
  const st = prRebootDlg.status
  if (!st) return ''
  const round = st.round || 0
  const total = st.total_rounds || 0
  const elapsed = st.elapsed_s || 0
  let tail = '等待首轮结果'
  if (st.last_result === 'ok') tail = '最近一次 OK'
  else if (st.last_result === 'failed') tail = '失败：' + (st.last_error || '未知原因')
  else if (!st.running && total) tail = st.cancelled ? '已停止' : '已结束'
  return '第 ' + round + '/' + total + ' 次 · 已用 ' + elapsed + ' 秒 · ' + tail
})
const prAddDlg = reactive({
  visible: false,
  busy: false,
  node_id: '',
  power_dev: '',
  name: '',
  com: '',
  channel: '',
  endpoint: '',
  location: '',
  driver: '',
})

const prBindModelOptions = computed(() => collectModelClasses(devices.value || []))

const prDeviceOptions = computed(() => {
  const list = devices.value || []
  const slotUsed = Object.create(null)
  for (const d of list) {
    if (!d || !d.device_id) continue
    const n = Number(d.serial_ports)
    slotUsed[d.device_id] = Number.isFinite(n) && n >= 0 ? n : 0
  }
  return filterBindTargets(list, {
    model: prBindDlg.modelFilter,
    keyword: prBindDlg.keyword,
    slotUsed,
    powerBound: prBindPowerCounts.value,
    slotMax: PR_SLOT_MAX,
  })
})

const prStats = computed(() => {
  const items = prItems.value || []
  const devs = new Set()
  let bound = 0
  for (const it of items) {
    if (it && it.power_dev) devs.add(it.power_dev)
    if (prRowBound(it)) bound += 1
  }
  return { devices: devs.size, channels: items.length, bound }
})

function prIsUnsupportedStatus(status) {
  return status === 404 || status === 405
}

function prNormalizeInventory(raw) {
  // 后端 list_inventory 直接返回数组；兼容 {items|inventory: [...]} 兜底
  if (Array.isArray(raw)) return raw
  if (raw && Array.isArray(raw.items)) return raw.items
  if (raw && Array.isArray(raw.inventory)) return raw.inventory
  return []
}

function prRowBound(row) {
  if (!row) return false
  if (row.bound && typeof row.bound === 'object') return true
  if (row.bound === true) return true
  return !!(row.bound_device_id || (row.bound && row.bound.device_id))
}

function prBoundDeviceId(row) {
  if (!row) return ''
  if (row.bound && typeof row.bound === 'object') return row.bound.device_id || ''
  return row.bound_device_id || ''
}

function prBoundSlot(row) {
  if (!row) return null
  if (row.bound && typeof row.bound === 'object') {
    return row.bound.slot != null ? row.bound.slot : null
  }
  return row.bound_slot != null ? row.bound_slot : null
}

function prDeviceName(deviceId) {
  if (!deviceId) return ''
  const d = (devices.value || []).find((x) => x && x.device_id === deviceId)
  return (d && (d.display_name || d.device_id)) || deviceId
}

function prBoundTargetText(row) {
  if (!prRowBound(row)) return '-'
  const did = prBoundDeviceId(row)
  const slot = prBoundSlot(row)
  const name = prDeviceName(did) || did || '?'
  return slot != null ? (name + '(' + slot + ')') : name
}

// ---- PT5a：权限收紧 + location/driver 展示 -------------------------------

function prIsAdmin() {
  return !!(me.value && me.value.role === 'admin')
}

/** 绑定目标设备的占用者 email（对照 me.email） */
function prBoundDeviceHolder(row) {
  const did = prBoundDeviceId(row)
  if (!did) return ''
  const d = (devices.value || []).find((x) => x && x.device_id === did)
  return (d && (d.holder_email || d.owner)) || ''
}

/** 非 admin 且绑定目标设备不归自己 → 电源动作按钮禁用（后端 check_access 兜底） */
function prActionLeaseBlocked(row) {
  if (!prRowBound(row)) return false
  if (prIsAdmin()) return false
  return prBoundDeviceHolder(row) !== (me.value && me.value.email)
}

/** 来源：driver + com 组合，如 bull-gn315 · COM29 */
function prSourceText(row) {
  if (!row) return ''
  const parts = []
  if (row.driver) parts.push(String(row.driver))
  if (row.com) parts.push(String(row.com))
  return parts.join(' · ')
}

function prLocationText(row) {
  return (row && row.location) || '—'
}

/** 设备详情页电源卡：非 admin 且当前设备不归自己 → 电源动作禁用 */
function devicePowerLeaseBlocked() {
  if (prIsAdmin()) return false
  const d = devicePage.value
  const holder = (d && (d.holder_email || d.owner)) || ''
  return holder !== (me.value && me.value.email)
}

/** 设备中心列表电源 popover：非 admin 且该行设备不归自己 → 动作禁用 */
function listPowerLeaseBlocked(row) {
  if (prIsAdmin()) return false
  const holder = (row && (row.holder_email || row.owner)) || ''
  return holder !== (me.value && me.value.email)
}

function listPowerN(row) {
  const s = row && row.serial
  if (!s || typeof s !== 'object') return null
  const n = s.power_n
  return (typeof n === 'number' && !Number.isNaN(n)) ? n : 0
}

function listPowerCache(deviceId) {
  const id = String(deviceId || '')
  if (!id) return { items: [], loading: false, error: '', loaded: false }
  if (!listPowerCapsCache[id]) {
    listPowerCapsCache[id] = { items: [], loading: false, error: '', loaded: false }
  }
  return listPowerCapsCache[id]
}

async function ensureListPowerCaps(deviceId) {
  const id = String(deviceId || '').trim()
  if (!id) return
  const c = listPowerCache(id)
  if (c.loaded || c.loading) return
  c.loading = true
  c.error = ''
  try {
    const raw = await api('/api/devices/' + encodeURIComponent(id) + '/capabilities')
    const items = (raw && Array.isArray(raw.items))
      ? raw.items.filter((it) => it && it.kind === 'power')
      : []
    c.items = items
    c.loaded = true
  } catch (e) {
    c.error = String((e && e.message) || e)
    c.items = []
    c.loaded = true
  } finally {
    c.loading = false
  }
}

function onListPowerPopoverShow(row) {
  if (!row || !row.device_id) return
  const n = listPowerN(row)
  if (n == null || n === 0) return
  ensureListPowerCaps(row.device_id)
}

function listPowerItemTitle(it) {
  const name = (it && (it.display_name || it.label || it.name)) || '—'
  let slot = null
  if (it && it.binding && it.binding.slot != null) slot = it.binding.slot
  else {
    const m = String((it && it.id) || '').match(/slot(\d+)/i)
    if (m) slot = m[1]
  }
  const ep = capEndpointText(it) || '—'
  return name + ' · slot' + (slot != null ? slot : '?') + ' · ' + ep
}

async function doListCapPower(row, item, actionKey) {
  const id = row && row.device_id
  if (!id) return
  const scope = 'list:' + id + ':' + ((item && item.id) || 'cap')
  if (actionKey === 'power_cycle') {
    try {
      await ElMessageBox.confirm(
        `确定对 ${id} 执行定时重启？\n断电保持间隔：${listPowerHoldMs.value || 3000} ms`,
        '重启确认',
        { type: 'warning', confirmButtonText: '重启', cancelButtonText: '取消' },
      )
    } catch (e) { return }
  }
  await runDeviceSerialAction(id, actionKey, { scope, hold_ms: listPowerHoldMs.value })
}

/** 行键：power_dev + channel（动作 loading / hold_ms / 失败相位） */
function prRowKey(row) {
  if (!row) return ''
  return String(row.power_dev || '') + '::' + String(row.channel)
}

const prHoldMs = reactive({})       // rowKey → number
const prActionBusy = reactive({})   // rowKey → bool
const prActionFail = reactive({})   // rowKey → { phase, code, message } | null

function prHoldFollowsHub(row) {
  if (!row) return false
  const v = row.restart_hold_ms
  return v != null && v !== '' && Number.isFinite(Number(v))
}

function prGetHoldMs(row) {
  // 已注册通道：跟随 hub 的 restart_hold_ms
  if (prHoldFollowsHub(row)) return Number(row.restart_hold_ms)
  const k = prRowKey(row)
  const v = prHoldMs[k]
  return (typeof v === 'number' && !Number.isNaN(v)) ? v : 3000
}

function prSetHoldMs(row, val) {
  if (prHoldFollowsHub(row)) return
  const k = prRowKey(row)
  let n = Number(val)
  if (!Number.isFinite(n)) n = 3000
  if (n < 500) n = 500
  if (n > 30000) n = 30000
  prHoldMs[k] = n
}

/** 开关绑定值：仅 on/off 可切换；未知态返回 false 且禁用 */
function prSwitchOn(row) {
  return String((row && row.state) || '').toLowerCase() === 'on'
}

function prSwitchKnown(row) {
  const st = String((row && row.state) || '').toLowerCase()
  return st === 'on' || st === 'off'
}

function prIsActionBusy(row) {
  return !!prActionBusy[prRowKey(row)]
}

function prFailPhase(row) {
  const f = prActionFail[prRowKey(row)]
  return f && f.phase ? f.phase : ''
}

function openPrDevicePage(deviceId) {
  if (!deviceId) return
  const want = '#/device/' + encodeURIComponent(deviceId)
  if (String(window.location.hash || '') !== want) {
    window.location.hash = want
  } else {
    enterDevicePage(deviceId)
  }
}
/** @deprecated 兼容旧名；请用 openPrDevicePage */
function openPrDevicePlaceholder(deviceId) {
  openPrDevicePage(deviceId)
}

function syncDeviceHash(deviceId) {
  const want = deviceId ? ('#/device/' + encodeURIComponent(deviceId)) : ''
  if (!want) return
  if (String(window.location.hash || '') !== want) {
    history.replaceState(null, '', window.location.pathname + window.location.search + want)
  }
}

function backToDeviceCenter() {
  devicePageId.value = ''
  devicePage.value = null
  devicePageError.value = ''
  const want = '#/dashboard'
  if (String(window.location.hash || '') !== want) {
    window.location.hash = want
  } else {
    switchPage('devices')
  }
}

function goBindPowerRes() {
  ctrlresCard.value = 'powerres'
  const want = '#/ctrlres?card=powerres'
  if (String(window.location.hash || '') !== want) {
    window.location.hash = want
  } else {
    switchPage('ctrlres')
  }
}

function resetAdbDebugState() {
  adbPanelOpen.value = false
  adbCmd.value = ''
  adbOut.value = null
  adbBusy.value = false
  adbErr.value = ''
}

async function enterDevicePage(deviceId) {
  const id = String(deviceId || '').trim()
  if (!id) return
  active.value = 'device'
  devicePageId.value = id
  resetAdbDebugState()
  syncDeviceHash(id)
  await refreshDevicePage()
}

async function refreshDevicePage() {
  const id = devicePageId.value
  if (!id) return
  devicePageLoading.value = true
  devicePageError.value = ''
  devicePowerAlert.value = ''
  try {
    devicePage.value = await api('/api/devices/' + encodeURIComponent(id))
  } catch (e) {
    devicePage.value = null
    devicePageError.value = (e && e.status === 404)
      ? '设备不存在或已移除'
      : String((e && e.message) || e)
    deviceCaps.value = { enabled: true, items: [], revision: 0 }
    deviceSerial.value = null
    devicePowerBindings.value = []
    devicePowerInventory.value = []
    devicePowerNodeId.value = ''
    devicePowerNodeSource.value = ''
    devicePowerNodePicking.value = false
    devicePageLoading.value = false
    return
  } finally {
    devicePageLoading.value = false
  }
  await Promise.all([
    loadDeviceCaps(),
    loadDeviceSerial(),
    loadDevicePowerBindings(),
  ])
  await resolveDevicePowerNodeAndInventory({ keepManual: false })
}

async function loadDeviceCaps() {
  const id = devicePageId.value
  if (!id) return
  deviceCapsLoading.value = true
  try {
    const raw = await api('/api/devices/' + encodeURIComponent(id) + '/capabilities')
    if (raw && raw.enabled === false) {
      deviceCaps.value = { enabled: false, items: [], revision: 0 }
    } else {
      deviceCaps.value = {
        enabled: true,
        items: Array.isArray(raw && raw.items) ? raw.items : [],
        revision: (raw && raw.revision) || 0,
      }
    }
  } catch (e) {
    deviceCaps.value = { enabled: true, items: [], revision: 0 }
    if (e && e.status !== 404) ElMessage.error('能力：' + String(e.message || e))
  } finally {
    deviceCapsLoading.value = false
  }
}

async function loadDeviceSerial() {
  const id = devicePageId.value
  if (!id) return
  deviceSerialLoading.value = true
  try {
    deviceSerial.value = await api('/api/devices/' + encodeURIComponent(id) + '/serial')
  } catch (e) {
    deviceSerial.value = null
    if (e && e.status !== 404) ElMessage.error('串口：' + String(e.message || e))
  } finally {
    deviceSerialLoading.value = false
  }
}

async function loadDevicePowerBindings() {
  const id = devicePageId.value
  if (!id) return
  devicePowerLoading.value = true
  try {
    let nodes = prNodes.value || []
    if (!nodes.length) {
      try {
        const raw = await api('/api/powerres/overview')
        nodes = (raw && Array.isArray(raw.nodes)) ? raw.nodes : []
        prNodes.value = nodes
      } catch (e) {
        nodes = []
      }
    }
    const out = []
    await Promise.all(nodes.map(async (n) => {
      try {
        const rows = await api('/api/powerres/bindings?node_id=' + encodeURIComponent(n.node_id))
        for (const b of (Array.isArray(rows) ? rows : [])) {
          if (b && b.device_id === id) {
            out.push({
              ...b,
              node_name: n.display_name || n.node_id,
            })
          }
        }
      } catch (e) { /* 单节点失败忽略 */ }
    }))
    out.sort((a, b) => String(a.power_dev || '').localeCompare(String(b.power_dev || ''))
      || (Number(a.channel) - Number(b.channel)))
    devicePowerBindings.value = out
  } catch (e) {
    devicePowerBindings.value = []
  } finally {
    devicePowerLoading.value = false
  }
}

/**
 * 从串口 caps 推断 node_id：ports[].node_id 或 enrich 后的 ports[].binding.node_id；
 * 取非空出现次数最多者（并列取先出现的）。
 */
function inferNodeIdFromSerialPorts(serialPayload) {
  const ports = serialPayload && Array.isArray(serialPayload.ports) ? serialPayload.ports : []
  const counts = Object.create(null)
  const order = []
  for (const p of ports) {
    if (!p) continue
    let nid = p.node_id
    if (!nid && p.binding && p.binding.node_id) nid = p.binding.node_id
    nid = nid != null ? String(nid).trim() : ''
    if (!nid) continue
    if (!Object.prototype.hasOwnProperty.call(counts, nid)) {
      counts[nid] = 0
      order.push(nid)
    }
    counts[nid] += 1
  }
  if (!order.length) return ''
  let best = order[0]
  let bestN = counts[best]
  for (let i = 1; i < order.length; i++) {
    const id = order[i]
    if (counts[id] > bestN) {
      best = id
      bestN = counts[id]
    }
  }
  return best
}

function devicePowerNodeLabel(nodeId) {
  const nid = String(nodeId || '').trim()
  if (!nid) return ''
  const nodes = prNodes.value || []
  const hit = nodes.find((n) => n && n.node_id === nid)
  if (hit) return hit.display_name || hit.node_id
  const b = (devicePowerBindings.value || []).find((x) => x && x.node_id === nid)
  if (b && b.node_name) return b.node_name
  return nid
}

async function ensurePowerResOverviewNodes() {
  let nodes = prNodes.value || []
  if (nodes.length) return nodes
  try {
    const raw = await api('/api/powerres/overview')
    nodes = (raw && Array.isArray(raw.nodes)) ? raw.nodes : []
    prNodes.value = nodes
  } catch (e) {
    nodes = []
  }
  return nodes
}

/**
 * 节点归属：① 串口 ports 众数 node_id → ② 本机 PowerRes binding → ③ 强制手选。
 * keepManual：刷新时若用户已手选则保留。
 */
async function resolveDevicePowerNodeAndInventory(opts) {
  const keepManual = !!(opts && opts.keepManual)
  await ensurePowerResOverviewNodes()

  if (keepManual && devicePowerNodeSource.value === 'manual' && devicePowerNodeId.value) {
    devicePowerNodePicking.value = false
    await loadDevicePowerInventory()
    return
  }

  const fromSerial = inferNodeIdFromSerialPorts(deviceSerial.value)
  if (fromSerial) {
    devicePowerNodeId.value = fromSerial
    devicePowerNodeSource.value = 'serial'
    devicePowerNodePicking.value = false
    await loadDevicePowerInventory()
    return
  }

  const binds = devicePowerBindings.value || []
  if (binds.length && binds[0].node_id) {
    devicePowerNodeId.value = String(binds[0].node_id)
    devicePowerNodeSource.value = 'binding'
    devicePowerNodePicking.value = false
    await loadDevicePowerInventory()
    return
  }

  // 无推断 → 强制手选
  devicePowerNodeId.value = ''
  devicePowerNodeSource.value = ''
  devicePowerNodePicking.value = true
  devicePowerInventory.value = []
}

async function loadDevicePowerInventory() {
  const nid = String(devicePowerNodeId.value || '').trim()
  if (!nid) {
    devicePowerInventory.value = []
    return
  }
  devicePowerInvLoading.value = true
  try {
    const raw = await api('/api/powerres/inventory?node_id=' + encodeURIComponent(nid))
    devicePowerInventory.value = prNormalizeInventory(raw)
  } catch (e) {
    devicePowerInventory.value = []
    if (e && e.status !== 404) {
      ElMessage.error('电源通道：' + String((e && e.message) || e))
    }
  } finally {
    devicePowerInvLoading.value = false
  }
}

async function refreshDevicePowerCard() {
  await loadDevicePowerBindings()
  await resolveDevicePowerNodeAndInventory({ keepManual: true })
}

function onDevicePowerNodePick(nid) {
  const id = String(nid || '').trim()
  devicePowerNodeId.value = id
  devicePowerNodeSource.value = id ? 'manual' : ''
  devicePowerNodePicking.value = !id
  if (id) loadDevicePowerInventory()
  else devicePowerInventory.value = []
}

function startDevicePowerNodeReselect() {
  devicePowerNodePicking.value = true
  ensurePowerResOverviewNodes()
}

const deviceCapsByKind = computed(() => {
  const items = (deviceCaps.value && deviceCaps.value.items) || []
  const order = ['adb', 'serial.io', 'power', 'mode']
  const groups = {}
  for (const k of order) groups[k] = []
  for (const it of items) {
    const k = (it && it.kind) || 'other'
    if (!groups[k]) groups[k] = []
    groups[k].push(it)
  }
  return order.filter((k) => (groups[k] || []).length).map((k) => ({
    kind: k,
    label: ({ adb: 'ADB', 'serial.io': '串口', power: '电源', mode: '模式' })[k] || k,
    items: groups[k],
  }))
})

/** 设备页能力区扁平顺序：ADB → 串口 → 电源（mode 排最后） */
const deviceCapsFlat = computed(() => {
  const items = (deviceCaps.value && deviceCaps.value.items) || []
  const order = { adb: 0, 'serial.io': 1, power: 2, mode: 3 }
  return items.slice().sort((a, b) => {
    const ka = order[(a && a.kind)] != null ? order[a.kind] : 9
    const kb = order[(b && b.kind)] != null ? order[b.kind] : 9
    return ka - kb
  })
})

const deviceSerialPorts = computed(() => {
  const ports = deviceSerial.value && deviceSerial.value.ports
  return Array.isArray(ports) ? ports : []
})

/** 非 admin：电源绑定只读摘要 */
const devicePowerBindSummary = computed(() => {
  const rows = devicePowerBindings.value || []
  if (!rows.length) return '未绑定电源通道'
  const parts = rows.map((r) => {
    const name = (r.power_dev || '?') + ' CH' + r.channel
    const loc = r.location ? ('（' + r.location + '）') : ''
    return name + loc
  })
  return '已绑定 ' + rows.length + ' 路：' + parts.join('；')
})

/** 头栏/电源卡展示用节点名（优先当前选中节点） */
const devicePageNodeName = computed(() => {
  if (devicePowerNodeId.value) return devicePowerNodeLabel(devicePowerNodeId.value)
  const b = (devicePowerBindings.value || [])[0]
  if (b && b.node_name) return b.node_name
  const items = (deviceCaps.value && deviceCaps.value.items) || []
  for (const it of items) {
    if (it && it.binding && it.binding.node_id) {
      return devicePowerNodeLabel(it.binding.node_id) || it.binding.node_id
    }
  }
  return ''
})

/** 可绑定通道：未绑本机（未绑优先，其次已绑他机） */
const devicePowerCandidates = computed(() => {
  const id = devicePageId.value
  const rows = (devicePowerInventory.value || []).filter((row) => {
    const bid = prBoundDeviceId(row)
    return !bid || bid !== id
  })
  rows.sort((a, b) => {
    const aBound = !!prBoundDeviceId(a)
    const bBound = !!prBoundDeviceId(b)
    if (aBound !== bBound) return aBound ? 1 : -1
    const pd = String(a.power_dev || '').localeCompare(String(b.power_dev || ''))
    if (pd) return pd
    return Number(a.channel) - Number(b.channel)
  })
  return rows
})

/** 本机无绑定且节点下无任何 inventory → 空态 */
const devicePowerFullEmpty = computed(() => {
  if (!devicePowerNodeId.value) return false
  if ((devicePowerBindings.value || []).length) return false
  return !(devicePowerInventory.value || []).length
})

function devicePageIp(dev) {
  if (!dev) return '—'
  if (dev.ip) return dev.ip
  const adb = String(dev.adb_address || '')
  const m = adb.match(/^([^:]+):/)
  return m ? m[1] : '—'
}

function capStatusTagType(item) {
  const reasons = (item && item.status_reasons) || []
  if (reasons.includes('NO_BINDING')) return 'warning'
  const st = item && item.status
  if (st === 'available' || st === 'ready') return 'success'
  if (st === 'degraded') return 'warning'
  return 'info'
}

function capStatusLabel(item) {
  if (!item) return '—'
  if (item.status_label) return item.status_label
  const reasons = item.status_reasons || []
  if (reasons.includes('NO_BINDING')) return 'NO_BINDING'
  if (reasons.includes('DISABLED')) return 'disabled'
  return item.status || reasons[0] || '—'
}

function capEndpointText(item) {
  const t = item && item.transport
  const b = item && item.binding
  return (t && t.endpoint) || (b && b.endpoint) || (t && t.port_name) || (b && b.com) || ''
}

function capKindLabel(kind) {
  return ({ adb: 'ADB', 'serial.io': '串口', power: '电源', mode: '模式' })[kind] || kind || '—'
}

function capSourceLabel(item) {
  const src = item && item.source
  if (src === 'observed') return '端口目录观测'
  if (src === 'powerres') return 'PowerRes'
  if (src === 'declared') return '声明端口'
  if (src === 'auto-bound') return '自动绑定'
  return src || '声明端口'
}

function capConstraintText(item) {
  const c = (item && item.constraints) || {}
  const parts = []
  if (c.read_only) parts.push('只读')
  if (c.shared_endpoint || c.shared) parts.push('共享')
  const src = capSourceLabel(item)
  if (src) parts.push(src)
  return parts.join(' / ') || '—'
}

function capConstraintOnly(item) {
  const c = (item && item.constraints) || {}
  const parts = []
  if (c.read_only) parts.push('只读')
  if (c.shared_endpoint || c.shared) parts.push('共享')
  return parts.join(' / ')
}

function capHealthText(item) {
  const h = item && item.health
  if (!h) return ''
  return h.state || ''
}

function capMatchSerialPort(item) {
  const ports = deviceSerialPorts.value || []
  if (!ports.length || !item) return null
  const ep = String(capEndpointText(item) || '').toLowerCase()
  const slot = item.binding && item.binding.slot
  const com = (item.binding && (item.binding.com || item.binding.com_key)) || ''
  return ports.find((p) => {
    if (slot != null && p.slot === slot) return true
    if (ep && String(p.endpoint || p.port_name || '').toLowerCase() === ep) return true
    if (com && String(p.port_name || p.com || '').toLowerCase() === String(com).toLowerCase()) return true
    return false
  }) || null
}

function capHolderText(item) {
  const busy = deviceSerial.value && deviceSerial.value.busy
  if (!busy) return ''
  const port = capMatchSerialPort(item)
  const slot = (item && item.binding && item.binding.slot != null)
    ? item.binding.slot
    : (port && port.slot)
  if (slot == null || busy.slot !== slot) return ''
  return busy.holder || busy.held_by || 'busy'
}

function capActionsEnabled(item) {
  const st = item && item.status
  return st === 'available' || st === 'degraded' || st === 'ready'
}

function capPowerMeta(item) {
  const b = (item && item.binding) || {}
  const ep = capEndpointText(item)
  const binds = devicePowerBindings.value || []
  let row = binds.find((x) => ep && String(x.endpoint || '') === String(ep)) || null
  if (!row && (b.power_dev != null || b.channel != null)) {
    row = binds.find((x) =>
      String(x.power_dev || '') === String(b.power_dev || '')
      && Number(x.channel) === Number(b.channel),
    ) || null
  }
  const inv = devicePowerInventory.value || []
  let invRow = null
  if (ep) invRow = inv.find((x) => String(x.endpoint || '') === String(ep)) || null
  if (!invRow && (b.power_dev != null || (row && row.power_dev != null))) {
    const pd = (row && row.power_dev) || b.power_dev
    const ch = (row && row.channel) != null ? row.channel : b.channel
    invRow = inv.find((x) =>
      String(x.power_dev || '') === String(pd || '')
      && Number(x.channel) === Number(ch),
    ) || null
  }
  const powerDev = (row && row.power_dev) || b.power_dev || (invRow && invRow.power_dev) || ''
  const channel = (row && row.channel != null) ? row.channel
    : (b.channel != null ? b.channel : (invRow && invRow.channel))
  return {
    power_dev: powerDev,
    channel,
    slot: (row && row.slot != null) ? row.slot : (b.slot != null ? b.slot : null),
    location: (row && row.location) || (invRow && invRow.location) || '',
    source: prSourceText(row || invRow || { driver: null, com: b.com }) || (b.source || ''),
    enabled: row ? row.enabled !== false : (invRow && invRow.enabled != null ? invRow.enabled !== false : null),
    label: (row && row.label) || item.label || '',
    restart_hold_ms: (item && item.restart_hold_ms != null) ? item.restart_hold_ms
      : (row && row.restart_hold_ms != null) ? row.restart_hold_ms
        : (invRow && invRow.restart_hold_ms != null) ? invRow.restart_hold_ms : null,
  }
}

function capRestartHoldMs(item) {
  const meta = capPowerMeta(item)
  if (meta.restart_hold_ms == null || meta.restart_hold_ms === '') return null
  const n = Number(meta.restart_hold_ms)
  return Number.isFinite(n) ? n : null
}

function capRestartHoldText(item) {
  const n = capRestartHoldMs(item)
  return n == null ? '—' : (n + ' ms·跟随端侧')
}

function capSerialConsoleTarget(item) {
  const b = (item && item.binding) || {}
  const ep = capEndpointText(item)
  if (b.node_id && b.slot != null) {
    return {
      mode: 'wl',
      row: {
        node_id: b.node_id,
        slot: b.slot,
        com: b.com || b.com_key,
        com_name: b.com || b.com_key,
        tty: b.com || b.com_key || '',
        endpoint: b.endpoint || ep,
        shell_reachable: true,
      },
    }
  }
  if (b.node_id && ep) {
    return {
      mode: 'port',
      row: {
        node_id: b.node_id,
        slot: b.slot,
        endpoint: ep,
        com: b.com || b.com_key,
      },
    }
  }
  const port = capMatchSerialPort(item)
  if (port && port.node_id && port.slot != null) {
    return {
      mode: 'wl',
      row: {
        node_id: port.node_id,
        slot: port.slot,
        com: port.port_name || port.com,
        endpoint: port.endpoint || ep,
        shell_reachable: true,
      },
    }
  }
  if (port && port.node_id && (port.endpoint || ep)) {
    return {
      mode: 'port',
      row: {
        node_id: port.node_id,
        slot: port.slot,
        endpoint: port.endpoint || ep,
        com: port.port_name || port.com,
      },
    }
  }
  return null
}

function openCapSerialConsole(item) {
  const target = capSerialConsoleTarget(item)
  if (!target) {
    ElMessage.warning('该端口未绑定节点')
    return
  }
  if (target.mode === 'wl') openWlShell(target.row)
  else openPortConsole(target.row)
}

function capKindActions(item) {
  return Array.isArray(item && item.actions) ? item.actions.filter((a) => a && a.enabled !== false) : []
}

function isPowerActionKey(key) {
  return key === 'power_on' || key === 'power_off' || key === 'power_cycle'
}

function deviceActionKey(scope, key) {
  return String(scope || '') + '|' + String(key || '')
}

function deviceIsActionBusy(scope, key) {
  return !!deviceActionBusy[deviceActionKey(scope, key)] || !!deviceActionBusy[deviceActionKey(scope, '*')]
}

function deviceFailPhase(scope, key) {
  const f = deviceActionFail[deviceActionKey(scope, key)] || deviceActionFail[deviceActionKey(scope, '*')]
  return f && f.phase ? f.phase : ''
}

async function runDeviceSerialAction(deviceId, actionKey, opts) {
  const scope = (opts && opts.scope) || actionKey
  const busyKey = deviceActionKey(scope, '*')
  if (deviceActionBusy[busyKey]) return
  const holdMs = (opts && opts.hold_ms != null) ? Number(opts.hold_ms) : Number(deviceHoldMs.value) || 3000
  const body = {
    action_key: actionKey,
    wait_ms: 3000,
    dry_run: false,
  }
  // power_off / power_cycle 强制 confirm；power_on 不带（断电残窗可立刻补偿）
  if (actionKey === 'power_off' || actionKey === 'power_cycle' || actionKey === 'mode_edl') {
    body.confirm = true
  }
  if (actionKey === 'power_cycle') body.hold_ms = holdMs
  deviceActionBusy[busyKey] = true
  deviceActionFail[busyKey] = null
  try {
    const out = await api('/api/devices/' + encodeURIComponent(deviceId) + '/serial/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (out && out.result === 'fail') {
      const phase = out.phase || ''
      const code = out.error_code || out.code || ''
      const msg = out.error || out.message || '动作失败'
      deviceActionFail[busyKey] = { phase, code, message: msg }
      let tip = (code ? code + ': ' : '') + msg
      if (phase === 'power_off') tip = '关机指令失败，设备可能仍通电。' + tip
      if (phase === 'power_on') tip = '重启中途失败：设备可能处于断电；请立即点「补偿开」。' + tip
      ElMessage.error(tip)
      return
    }
    ElMessage.success(
      actionKey === 'power_on' ? '已开'
        : actionKey === 'power_off' ? '已关'
          : actionKey === 'power_cycle' ? ('已重启（间隔 ' + holdMs + ' ms）')
            : ((opts && opts.okText) || '动作成功'),
    )
    try { await loadDeviceSerial() } catch (e) { /* ignore */ }
    try { await loadDeviceCaps() } catch (e) { /* ignore */ }
  } catch (e) {
    if (e === 'cancel' || (e && e.message === 'cancel')) return
    const code = (e && e.code) || ''
    let tip = String((e && e.message) || e)
    if (code === 'SERIAL_BUSY') {
      const holder = e.detail && (e.detail.holder || e.detail.held_by)
      tip = holder ? ('通道被 ' + holder + ' 占用') : ('SERIAL_BUSY：' + tip)
    } else if (code === 'POWER_DISABLED') {
      tip = '该通道已停用'
    } else if (code === 'CONFIRM_REQUIRED') {
      tip = '需要确认'
    }
    deviceActionFail[busyKey] = { phase: '', code, message: tip }
    ElMessage.error(tip)
  } finally {
    deviceActionBusy[busyKey] = false
  }
}

async function doDeviceCapPower(item, actionKey) {
  const id = devicePageId.value
  if (!id) return
  const scope = (item && item.id) || 'cap'
  const holdMs = capRestartHoldMs(item)
  const holdForAction = holdMs != null ? holdMs : (Number(deviceHoldMs.value) || 3000)
  if (actionKey === 'power_cycle') {
    try {
      await ElMessageBox.confirm(
        `确定对 ${id} 执行定时重启？\n断电保持间隔：${holdForAction} ms` + (holdMs != null ? '（跟随端侧）' : ''),
        '重启确认',
        { type: 'warning', confirmButtonText: '重启', cancelButtonText: '取消' },
      )
    } catch (e) { return }
  }
  await runDeviceSerialAction(id, actionKey, { scope, hold_ms: holdForAction })
}

async function doDeviceCapAction(item, action) {
  const id = devicePageId.value
  if (!id || !action) return
  const key = action.action_key
  if (isPowerActionKey(key)) {
    await doDeviceCapPower(item, key)
    return
  }
  if (key === 'adb_shell' || (action && action.exec_mode === 'adb_exec') || (item && item.kind === 'adb')) {
    adbPanelOpen.value = true
    return
  }
  await runDeviceSerialAction(id, key, {
    scope: (item && item.id) || key,
    okText: action.display_name || key,
  })
}

function setAdbQuickCmd(cmd) {
  adbCmd.value = cmd
}

async function runDeviceAdbExec(opts) {
  const id = devicePageId.value
  const command = String((opts && opts.command != null) ? opts.command : adbCmd.value || '').trim()
  if (!id || !command) {
    ElMessage.warning('请输入 ADB 命令')
    return
  }
  if (adbBusy.value) return
  adbBusy.value = true
  adbErr.value = ''
  try {
    const body = { command }
    if (opts && opts.confirm) body.confirm = true
    if (opts && opts.timeout_ms != null) body.timeout_ms = opts.timeout_ms
    const out = await api('/api/devices/' + encodeURIComponent(id) + '/adb/exec', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    adbOut.value = out || null
    if (out && out.result === 'fail') {
      ElMessage.error((out.error_code || out.code || 'ADB') + ': ' + (out.error || out.message || '执行失败'))
    }
  } catch (e) {
    if (e === 'cancel' || (e && e.message === 'cancel')) return
    const code = (e && e.code) || ''
    if (code === 'ADB_CONFIRM_REQUIRED') {
      try {
        await ElMessageBox.confirm(
          '该命令可能有破坏性，确认在设备上执行？\n' + command,
          'ADB 二次确认',
          { type: 'warning', confirmButtonText: '确认执行', cancelButtonText: '取消' },
        )
      } catch (ce) { return }
      adbBusy.value = false
      await runDeviceAdbExec({ command, confirm: true, timeout_ms: opts && opts.timeout_ms })
      return
    }
    if (code === 'ADB_NOT_ALLOWED' || (e && e.status === 403)) {
      adbErr.value = '需 admin 或设备租约'
      ElMessage.error('需 admin 或设备租约')
      return
    }
    adbErr.value = String((e && e.message) || e)
    ElMessage.error(adbErr.value)
  } finally {
    adbBusy.value = false
  }
}

function onAdbCmdEnter() {
  runDeviceAdbExec({ command: adbCmd.value })
}

async function doDevicePowerBindingAction(row, actionKey) {
  const id = devicePageId.value || (row && row.device_id)
  if (!id || !row) return
  const scope = 'pwr:' + (row.node_id || '') + ':' + (row.power_dev || '') + ':' + (row.channel || '')
  if (actionKey === 'power_cycle') {
    try {
      await ElMessageBox.confirm(
        `确定对 ${row.power_dev} CH${row.channel} 执行定时重启？\n断电保持间隔：${deviceHoldMs.value || 3000} ms`,
        '重启确认',
        { type: 'warning', confirmButtonText: '重启', cancelButtonText: '取消' },
      )
    } catch (e) { return }
  }
  await runDeviceSerialAction(id, actionKey, { scope, hold_ms: deviceHoldMs.value })
}

async function doDevicePowerUnbind(row) {
  if (!row || !row.node_id) return
  try {
    await ElMessageBox.confirm(
      `确定解绑 ${row.power_dev} CH${row.channel}？`,
      '解绑确认',
      { type: 'warning' },
    )
  } catch (e) { return }
  try {
    await api('/api/powerres/unbind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        node_id: row.node_id,
        power_dev: row.power_dev,
        channel: row.channel,
      }),
    })
    devicePowerAlert.value = `已解绑 ${row.power_dev} CH${row.channel}`
    ElMessage.success('已解绑')
    await loadDeviceCaps()
    await loadDeviceSerial()
    await refreshDevicePowerCard()
  } catch (e) {
    if (e === 'cancel' || (e && e.message === 'cancel')) return
    ElMessage.error(String((e && e.message) || e))
  }
}

function devicePowerCandRowKey(row) {
  if (!row) return ''
  return String(row.power_dev || '') + '::' + String(row.channel)
}

async function doDevicePowerBind(row) {
  const deviceId = devicePageId.value
  const nodeId = String(devicePowerNodeId.value || '').trim()
  if (!deviceId) return
  if (!nodeId) {
    ElMessage.warning('请先选择设备所在节点')
    devicePowerNodePicking.value = true
    return
  }
  if (!row || !row.power_dev || row.channel == null) return
  const endpoint = row.endpoint != null ? String(row.endpoint).trim() : ''
  if (!endpoint) {
    ElMessage.warning('该通道端点缺失，请在 PowerRes 卡片补登记')
    return
  }
  const otherId = prBoundDeviceId(row)
  const otherName = otherId ? (prDeviceName(otherId) || otherId) : ''
  const ch = row.channel
  try {
    if (otherId && otherId !== deviceId) {
      await ElMessageBox.confirm(
        `确定将 ${row.power_dev} CH${ch} 绑定到本设备？\n` +
          `该通道当前绑定的设备（${otherName}）将失去远程控电能力。`,
        '抢绑确认',
        { type: 'warning', confirmButtonText: '仍要绑定', cancelButtonText: '取消' },
      )
    } else {
      await ElMessageBox.confirm(
        `确定将 ${row.power_dev} CH${ch} 绑定到本设备？`,
        '绑定确认',
        { type: 'info', confirmButtonText: '绑定', cancelButtonText: '取消' },
      )
    }
  } catch (e) { return }

  const busyKey = devicePowerCandRowKey(row)
  devicePowerBindBusy.value = busyKey
  try {
    await api('/api/powerres/bind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        node_id: nodeId,
        power_dev: row.power_dev,
        channel: row.channel,
        endpoint,
        device_id: deviceId,
        // slot 省略：后端 _allocate_slot
      }),
    })
    ElMessage.success('已绑定 CH' + ch)
    devicePowerAlert.value = `已绑定 ${row.power_dev} CH${ch}`
    await loadDeviceCaps()
    await loadDeviceSerial()
    await refreshDevicePowerCard()
  } catch (e) {
    if (e === 'cancel' || (e && e.message === 'cancel')) return
    const code = (e && e.code) || ''
    if (code === 'POWERRES_NO_SLOT') {
      ElMessage.error('POWERRES_NO_SLOT: 该设备已达 16 个串口上限')
    } else {
      const msg = String((e && e.message) || e)
      ElMessage.error(code ? (code + ': ' + msg) : msg)
    }
  } finally {
    devicePowerBindBusy.value = ''
  }
}

function openDevicePageFromList(row) {
  if (!row || !row.device_id) return
  openPrDevicePage(row.device_id)
}

function prBindingEnabled(row) {
  if (!row) return true
  if (row.enabled === false) return false
  if (row.bound && typeof row.bound === 'object' && row.bound.enabled === false) return false
  return true
}

/** PT7a：管理员通道级动作（未绑定也可直控；已绑定受 enabled 闸） */
async function runPrChannelAction(row, actionKey) {
  if (!row || !prNodeId.value) return
  if (!prBindingEnabled(row)) {
    ElMessage.warning('该通道已停用')
    return
  }
  const k = prRowKey(row)
  if (prActionBusy[k]) return
  const holdMs = prGetHoldMs(row)
  const body = {
    node_id: prNodeId.value,
    power_dev: row.power_dev,
    channel: row.channel,
    action: actionKey,
  }
  if (actionKey === 'power_cycle') body.hold_ms = holdMs
  prActionBusy[k] = true
  prActionFail[k] = null
  try {
    const out = await api('/api/powerres/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (out && (out.result === 'fail' || out.result === 'failed')) {
      const phase = out.phase || ''
      const code = out.error_code || out.code || ''
      const msg = out.error || out.message || '动作失败'
      prActionFail[k] = { phase, code, message: msg }
      let tip = (code ? code + ': ' : '') + msg
      if (phase === 'power_off') tip = '关机指令失败，设备可能仍通电。' + tip
      if (phase === 'power_on') tip = '重启中途失败：设备可能处于断电；请立即点「补偿开」。' + tip
      ElMessage.error(tip)
      return
    }
    if (actionKey === 'power_query') {
      const st = (out && out.state) || '—'
      ElMessage.success('当前状态：' + st)
    } else {
      ElMessage.success(
        actionKey === 'power_on' ? '已开'
          : actionKey === 'power_off' ? '已关'
            : actionKey === 'power_cycle' ? ('已重启（间隔 ' + holdMs + ' ms）')
              : '动作成功',
      )
    }
    await loadPowerResInventory({ skipLoading: true })
  } catch (e) {
    if (e === 'cancel' || (e && e.message === 'cancel')) return
    const code = (e && e.code) || ''
    let tip = String((e && e.message) || e)
    if (code === 'POWERRES_BUSY' || code === 'SERIAL_BUSY') {
      const holder = e.detail && (e.detail.holder || e.detail.held_by)
      tip = holder
        ? ('通道被 ' + holder + ' 占用')
        : ((code === 'POWERRES_BUSY' ? 'POWERRES_BUSY：' : 'SERIAL_BUSY：') + tip)
    } else if (code === 'POWERRES_NO_ENDPOINT') {
      tip = '端点缺失，请检查 hub 上报'
    } else if (code === 'POWER_DISABLED') {
      tip = '该通道已停用'
    } else if (code === 'CONFIRM_REQUIRED') {
      tip = '需要确认'
    }
    prActionFail[k] = { phase: '', code, message: tip }
    ElMessage.error(tip)
  } finally {
    prActionBusy[k] = false
  }
}

async function runPrSerialAction(row, actionKey, opts) {
  const deviceId = prBoundDeviceId(row)
  if (!deviceId) {
    ElMessage.warning('请先绑定目标设备')
    return
  }
  if (!prBindingEnabled(row)) {
    ElMessage.warning('该通道已停用')
    return
  }
  const k = prRowKey(row)
  if (prActionBusy[k]) return
  const holdMs = prGetHoldMs(row)
  const body = {
    action_key: actionKey,
    wait_ms: 3000,
    dry_run: false,
  }
  if (actionKey === 'power_off' || actionKey === 'power_cycle') {
    body.confirm = true
  }
  if (actionKey === 'power_cycle') body.hold_ms = holdMs
  prActionBusy[k] = true
  prActionFail[k] = null
  try {
    const out = await api('/api/devices/' + encodeURIComponent(deviceId) + '/serial/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (out && out.result === 'fail') {
      const phase = out.phase || ''
      const code = out.error_code || out.code || ''
      const msg = out.error || out.message || '动作失败'
      prActionFail[k] = { phase, code, message: msg }
      let tip = (code ? code + ': ' : '') + msg
      if (phase === 'power_off') tip = '关机指令失败，设备可能仍通电。' + tip
      if (phase === 'power_on') tip = '重启中途失败：设备可能处于断电；请立即点「补偿开」。' + tip
      ElMessage.error(tip)
      return
    }
    ElMessage.success(
      actionKey === 'power_on' ? '已开'
        : actionKey === 'power_off' ? '已关'
          : actionKey === 'power_cycle' ? ('已重启（间隔 ' + holdMs + ' ms）')
            : '动作成功',
    )
    // 刷新该行状态：重拉 inventory；顺带读串口能力（状态/holder）
    try {
      await api('/api/devices/' + encodeURIComponent(deviceId) + '/serial')
    } catch (e) { /* 可选刷新，忽略 */ }
    await loadPowerResInventory({ skipLoading: true })
  } catch (e) {
    if (e === 'cancel' || (e && e.message === 'cancel')) return
    const code = (e && e.code) || ''
    let tip = String((e && e.message) || e)
    if (code === 'SERIAL_BUSY' || code === 'POWERRES_BUSY') {
      const holder = e.detail && (e.detail.holder || e.detail.held_by)
      tip = holder
        ? ('通道被 ' + holder + ' 占用')
        : ((code === 'POWERRES_BUSY' ? 'POWERRES_BUSY：' : 'SERIAL_BUSY：') + tip)
    } else if (code === 'POWERRES_NO_ENDPOINT') {
      tip = '端点缺失，请检查 hub 上报'
    } else if (code === 'POWER_DISABLED') {
      tip = '该通道已停用'
    } else if (code === 'CONFIRM_REQUIRED') {
      tip = '需要确认'
    }
    prActionFail[k] = { phase: '', code, message: tip }
    ElMessage.error(tip)
  } finally {
    prActionBusy[k] = false
  }
}

/** admin → 通道级；非 admin → 设备级（需绑定+租约） */
async function runPrPowerAction(row, actionKey) {
  if (prIsAdmin()) {
    await runPrChannelAction(row, actionKey)
  } else {
    await runPrSerialAction(row, actionKey)
  }
}

async function doPrPowerOn(row) {
  await runPrPowerAction(row, 'power_on')
}

async function doPrPowerOff(row) {
  const target = prRowBound(row)
    ? prBoundTargetText(row)
    : ((row && row.power_dev ? row.power_dev : '') + ' CH' + (row && row.channel))
  try {
    await ElMessageBox.confirm(
      `确定关闭 ${target} 电源？`,
      '关机确认',
      { type: 'warning', confirmButtonText: '关机', cancelButtonText: '取消' },
    )
  } catch (e) {
    return
  }
  await runPrPowerAction(row, 'power_off')
}

/** PT8-tres：el-switch 切换；on→power_off / off→power_on；不乐观更新，靠返回 state */
async function onPrPowerSwitch(row, wantedOn) {
  if (!row || !prSwitchKnown(row)) return
  const curOn = prSwitchOn(row)
  if (wantedOn === curOn) return
  if (wantedOn) {
    await doPrPowerOn(row)
  } else {
    await doPrPowerOff(row)
  }
}

async function doPrPowerCycle(row) {
  if (!prIsAdmin() && !prRowBound(row)) return
  const hold = prGetHoldMs(row)
  const target = prRowBound(row)
    ? prBoundTargetText(row)
    : ((row && row.power_dev ? row.power_dev : '') + ' CH' + (row && row.channel))
  try {
    await ElMessageBox.confirm(
      `确定对 ${target} 执行定时重启？\n断电保持间隔：${hold} ms`,
      '重启确认',
      { type: 'warning', confirmButtonText: '重启', cancelButtonText: '取消' },
    )
  } catch (e) {
    return
  }
  await runPrPowerAction(row, 'power_cycle')
}

async function doPrCompensateOn(row) {
  await runPrPowerAction(row, 'power_on')
}

async function doPrPowerQuery(row) {
  if (!prIsAdmin()) return
  await runPrChannelAction(row, 'power_query')
}

/** PT：通道级重启测试（admin）——间隔 / 次数 / 起停 */
function openPrRebootDlg(row) {
  if (!prIsAdmin() || !row) return
  if (!prBindingEnabled(row)) {
    ElMessage.warning('该通道已停用')
    return
  }
  prRebootDlg.row = row
  prRebootDlg.power_dev = row.power_dev || ''
  prRebootDlg.channel = row.channel
  prRebootDlg.node_id = prNodeId.value
  prRebootDlg.hold_ms = prGetHoldMs(row)
  prRebootDlg.interval_s = 30
  prRebootDlg.repeat = 20
  prRebootDlg.status = null
  prRebootDlg.visible = true
  // 打开即读取一次当前状态：单例 runner 可能已在别处运行
  api('/api/powerres/reboot-test/status')
    .then((st) => {
      prRebootDlg.status = st
      if (st && st.running) startPrRebootPoll()
    })
    .catch(() => {})
}

function closePrRebootDlg() {
  prRebootDlg.visible = false
  stopPrRebootPoll()
}

async function startPrRebootTest() {
  const row = prRebootDlg.row
  if (!row || !prRebootDlg.node_id) return
  const repeat = Number(prRebootDlg.repeat) || 0
  const interval = Number(prRebootDlg.interval_s) || 0
  if (repeat < 1 || interval < 1) {
    ElMessage.warning('间隔与次数需 ≥ 1')
    return
  }
  let confirm = false
  if (repeat > 100) {
    try {
      await ElMessageBox.confirm(
        `次数 ${repeat} 超过 100，将连续多轮断电重启，确认开始？`,
        '重启测试确认',
        { type: 'warning', confirmButtonText: '开始', cancelButtonText: '取消' },
      )
    } catch (e) {
      return
    }
    confirm = true
  }
  prRebootDlg.starting = true
  try {
    const out = await api('/api/powerres/reboot-test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        node_id: prRebootDlg.node_id,
        power_dev: row.power_dev,
        channel: row.channel,
        interval_s: interval,
        repeat,
        hold_ms: prRebootDlg.hold_ms,
        confirm,
      }),
    })
    prRebootDlg.status = out
    startPrRebootPoll()
    ElMessage.success('重启测试已启动')
  } catch (e) {
    if (e && e.code === 'FLOW_BUSY') {
      const st = e.detail && e.detail.status
      if (st) prRebootDlg.status = st
      ElMessage.error('已有重启测试在运行')
    } else if (e && e.code === 'TOO_LONG') {
      ElMessage.error('预计耗时超过 1 小时，请减少次数或间隔')
    } else if (e && e.code === 'NEED_CONFIRM') {
      ElMessage.error('次数超过 100，需二次确认')
    } else {
      ElMessage.error(String((e && (e.apiMessage || e.message)) || e))
    }
  } finally {
    prRebootDlg.starting = false
  }
}

async function stopPrRebootTest() {
  prRebootDlg.stopping = true
  try {
    const out = await api('/api/powerres/reboot-test/stop', { method: 'POST' })
    prRebootDlg.status = out
    if (out && !out.running) stopPrRebootPoll()
  } catch (e) {
    ElMessage.error(String((e && (e.apiMessage || e.message)) || e))
  } finally {
    prRebootDlg.stopping = false
  }
}

async function enterPowerRes() {
  if (!(devices.value || []).length) loadDevices()
  await loadPowerResOverview()
}

async function loadPowerResOverview() {
  prError.value = ''
  prUnsupported.value = false
  prLoading.value = true
  try {
    const raw = await api('/api/powerres/overview')
    const nodes = (raw && Array.isArray(raw.nodes)) ? raw.nodes : []
    prNodes.value = nodes
    if (prNodeId.value && !nodes.some((n) => n.node_id === prNodeId.value)) {
      prNodeId.value = ''
      prItems.value = []
    }
    if (!prNodeId.value && nodes.length) {
      prNodeId.value = nodes[0].node_id
    }
    if (prNodeId.value) {
      await loadPowerResInventory({ skipLoading: true })
    } else {
      prItems.value = []
    }
  } catch (e) {
    prNodes.value = []
    prItems.value = []
    if (prIsUnsupportedStatus(e && e.status)) {
      prUnsupported.value = true
      prError.value = ''
    } else {
      prError.value = String(e.message || e)
      ElMessage.error('PowerRes：' + prError.value)
    }
  } finally {
    prLoading.value = false
  }
}

async function loadPowerResInventory(opts) {
  const skipLoading = opts && opts.skipLoading
  prError.value = ''
  if (prUnsupported.value) return
  if (!prNodeId.value) {
    prItems.value = []
    return
  }
  if (!skipLoading) prLoading.value = true
  try {
    const raw = await api('/api/powerres/inventory?node_id=' + encodeURIComponent(prNodeId.value))
    prItems.value = prNormalizeInventory(raw)
  } catch (e) {
    prItems.value = []
    if (prIsUnsupportedStatus(e && e.status)) {
      prUnsupported.value = true
      prError.value = ''
    } else {
      prError.value = String(e.message || e)
      ElMessage.error('PowerRes：' + prError.value)
    }
  } finally {
    if (!skipLoading) prLoading.value = false
  }
}

function onPrNodeChange() {
  loadPowerResInventory()
}

function refreshPowerRes() {
  loadPowerResOverview()
}

async function loadPrBindPowerCounts() {
  const nid = prNodeId.value
  if (!nid || prUnsupported.value) {
    prBindPowerCounts.value = {}
    return
  }
  prBindDlg.bindingsLoading = true
  try {
    const raw = await api('/api/powerres/bindings?node_id=' + encodeURIComponent(nid))
    const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.bindings) ? raw.bindings : [])
    prBindPowerCounts.value = aggregatePowerBoundCounts(list)
  } catch (e) {
    prBindPowerCounts.value = {}
    if (prIsUnsupportedStatus(e && e.status)) {
      prUnsupported.value = true
      return
    }
    // 已绑数仅展示用：失败不阻断绑定对话框
  } finally {
    prBindDlg.bindingsLoading = false
  }
}

function openPrBind(row) {
  if (!row) return
  prBindDlg.power_dev = row.power_dev || ''
  prBindDlg.name = row.name || ''
  prBindDlg.com = row.com || ''
  prBindDlg.location = row.location || ''
  prBindDlg.driver = row.driver || ''
  prBindDlg.channel = row.channel
  prBindDlg.endpoint = row.endpoint || ''
  prBindDlg.device_id = ''
  prBindDlg.label = ''
  prBindDlg.slot = ''
  prBindDlg.modelFilter = ''
  prBindDlg.keyword = ''
  prBindDlg.busy = false
  prBindDlg.visible = true
  if (!(devices.value || []).length) loadDevices()
  loadPrBindPowerCounts()
}

watch(prNodeId, () => {
  if (prBindDlg.visible) loadPrBindPowerCounts()
})

async function submitPrBind() {
  if (!prNodeId.value) {
    ElMessage.warning('请先选择节点')
    return
  }
  if (!prBindDlg.device_id) {
    ElMessage.warning('请选择目标被测设备')
    return
  }
  if (!prBindDlg.endpoint) {
    ElMessage.warning('该通道无 endpoint，无法绑定')
    return
  }
  const body = {
    node_id: prNodeId.value,
    device_id: prBindDlg.device_id,
    power_dev: prBindDlg.power_dev,
    channel: prBindDlg.channel,
    endpoint: prBindDlg.endpoint,
  }
  if (prBindDlg.label && String(prBindDlg.label).trim()) {
    body.label = String(prBindDlg.label).trim()
  }
  if (prBindDlg.com) body.com = prBindDlg.com
  const slotRaw = prBindDlg.slot
  if (slotRaw !== '' && slotRaw != null) {
    const n = Number(slotRaw)
    if (!Number.isInteger(n) || n < 1 || n > PR_SLOT_MAX) {
      ElMessage.warning('slot 须为 1..' + PR_SLOT_MAX + ' 的整数，或留空自动分配')
      return
    }
    body.slot = n
  }
  prBindDlg.busy = true
  try {
    await api('/api/powerres/bind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    ElMessage.success('绑定成功')
    prBindDlg.visible = false
    await loadPowerResOverview()
  } catch (e) {
    if (prIsUnsupportedStatus(e && e.status)) {
      prUnsupported.value = true
      prBindDlg.visible = false
      return
    }
    const code = (e && e.code) || ''
    const msg = String((e && e.message) || e)
    ElMessage.error(code ? (code + ': ' + msg) : msg)
  } finally {
    prBindDlg.busy = false
  }
}

async function doPrUnbind(row) {
  if (!row || !prNodeId.value) return
  try {
    await ElMessageBox.confirm(
      `确定解绑 ${row.power_dev} CH${row.channel}？` +
        (prRowBound(row) ? `（当前绑定 ${prBoundTargetText(row)}）` : ''),
      '解绑确认',
      { type: 'warning' },
    )
    await api('/api/powerres/unbind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        node_id: prNodeId.value,
        power_dev: row.power_dev,
        channel: row.channel,
      }),
    })
    ElMessage.success('已解绑')
    await loadPowerResOverview()
  } catch (e) {
    if (e === 'cancel' || e?.message === 'cancel') return
    if (prIsUnsupportedStatus(e && e.status)) {
      prUnsupported.value = true
      return
    }
    ElMessage.error(String(e.message || e))
  }
}

async function openPrAdd() {
  if (prUnsupported.value) return
  prAddDlg.node_id = prNodeId.value || ''
  prAddDlg.power_dev = ''
  prAddDlg.name = ''
  prAddDlg.com = ''
  prAddDlg.channel = ''
  prAddDlg.endpoint = ''
  prAddDlg.location = ''
  prAddDlg.driver = ''
  prAddDlg.busy = false
  prAddDlg.visible = true
  // 手工登记需可选任意 wirelog 节点（overview 尚无该节点时的建档兜底）
  if (!(wlNodes.value || []).length) {
    try { await loadWlNodes() } catch (e) { /* loadWlNodes 已提示 */ }
  }
}

async function submitPrAdd() {
  const nid = String(prAddDlg.node_id || '').trim()
  const powerDev = String(prAddDlg.power_dev || '').trim()
  const chRaw = prAddDlg.channel
  const endpoint = String(prAddDlg.endpoint || '').trim()
  if (!nid) {
    ElMessage.warning('请选择节点')
    return
  }
  if (!powerDev) {
    ElMessage.warning('请填写电源设备 id')
    return
  }
  const ch = Number(chRaw)
  if (!Number.isInteger(ch) || ch < 0) {
    ElMessage.warning('通道号须为非负整数')
    return
  }
  if (!endpoint) {
    ElMessage.warning('请填写端点（如 tcp://host:port）')
    return
  }
  const body = {
    node_id: nid,
    power_dev: powerDev,
    channel: ch,
    endpoint,
  }
  const name = String(prAddDlg.name || '').trim()
  const com = String(prAddDlg.com || '').trim()
  const location = String(prAddDlg.location || '').trim()
  const driver = String(prAddDlg.driver || '').trim()
  if (name) body.name = name
  if (com) body.com = com
  if (location) body.location = location
  if (driver) body.driver = driver
  prAddDlg.busy = true
  try {
    const raw = await api('/api/powerres/inventory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    ElMessage.success('资源已登记')
    prAddDlg.visible = false
    prNodeId.value = nid
    // 刷新 overview（新建节点会进入下拉）再刷 inventory
    await loadPowerResOverview()
    prItems.value = prNormalizeInventory(raw)
  } catch (e) {
    if (prIsUnsupportedStatus(e && e.status)) {
      prUnsupported.value = true
      prAddDlg.visible = false
      return
    }
    ElMessage.error(String(e.message || e))
  } finally {
    prAddDlg.busy = false
  }
}

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

function isBoundDeviceRetired(row) {
  return !!(row && row.bound_device && row.bound_device.status === 'retired')
}

function jumpToWlNode(nodeId) {
  ctrlresCard.value = 'comres'
  comresTab.value = 'nodes'
  active.value = 'ctrlres'
  syncCtrlresHash()
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
  // 旧导航 key 兼容（概览卡片 / 外部入口）
  if (key === 'wirelog') { key = 'ctrlres'; ctrlresCard.value = 'comres' }
  else if (key === 'powerres') { key = 'ctrlres'; ctrlresCard.value = 'powerres' }
  if (key !== 'device') {
    devicePageId.value = ''
    // 离开详情页时清掉 #/device/...，避免侧栏切页后 hash 残留
    if (/^#\/?device\//i.test(String(window.location.hash || ''))) {
      history.replaceState(null, '', window.location.pathname + window.location.search)
    }
  }
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
  else if (key === 'ctrlres') {
    syncCtrlresHash()
    if (ctrlresCard.value === 'powerres') enterPowerRes()
    else enterComRes()
  }
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
    loadVersionInfo()
    loadHealthInfo()
    loadProductTypes()
    loadBuildServers()
    const routed = resolveLegacyRoute()
    if (!routed) {
      active.value = 'devices'
      loadDashboard()
      loadDevices()
      loadComresResources()
    } else if (active.value === 'ctrlres') {
      // 深链进 CtrlRes 时仍需设备列表（PowerRes 绑定下拉）
      if (!(devices.value || []).length) loadDevices()
    } else if (active.value === 'device') {
      if (!(devices.value || []).length) loadDevices()
    } else if (active.value === 'devices') {
      loadDashboard()
      loadDevices()
      loadComresResources()
    }
  } else {
    resolveLegacyRoute()
  }
}
onMounted(() => {
  boot()
  window.addEventListener('hashchange', () => { resolveLegacyRoute() })
  // 回到窗口/切回设备中心时刷新一次串口统计，避免页面长时间挂着显示旧值
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && me.value) loadComresResources(0)
  })
})
watch(active, (v) => {
  if (v === 'devices' && me.value) loadComresResources(0)
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
function isSelfCredentialRow(row) {
  if (!me.value || !row) return false
  if (me.value.credential_id != null) return row.id === me.value.credential_id
  // auth/me 未暴露 credential_id 时：同邮箱且同 role 保守视为当前会话凭据
  return row.email === me.value.email && row.role === me.value.role
}
async function setCredentialRole(row, role) {
  const promote = role === 'admin'
  try {
    await ElMessageBox.confirm(
      promote
        ? '将该凭据提升为管理员？其持有者将立即获得管理员权限（强制回收/发凭据/机型管理等）。'
        : '将该凭据降为普通用户？其权限立即收窄为普通用户。',
      promote ? '设为管理员' : '设为普通用户',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: promote ? 'warning' : 'warning' },
    )
    await api('/api/credentials/' + row.id + '/role', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    })
    ElMessage.success(promote ? '已设为管理员' : '已设为普通用户')
    loadCredentials()
  } catch (e) {
    if (e === 'cancel') return
    ElMessage.error(e.message || '操作失败')
  }
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
// v0.54.8（#8436）：新增设备留空名称时将采用的默认名（机型展示名优先）
const deviceAddDefaultName = computed(() => {
  const mc = deviceAddConfirm.value.model_class
  if (!mc) return ''
  const t = productTypes.value.find((p) => p.type_code === mc)
  const name = (t && t.display_name || '').trim()
  return name && name !== mc ? name : ''
})
function openPtEdit(row) {
  ptEditRow.value = {
    type_code: row.type_code, display_name: row.display_name || '',
    has_screen: !!row.has_screen, has_led: !!row.has_led,
    serial_count: row.serial_count === undefined || row.serial_count === null ? 1 : row.serial_count,
    adb_port: row.adb_port || 5555, model_pattern: row.model_pattern || '',
    defconfig: row.defconfig || '', sdk_repo_cmd: row.sdk_repo_cmd || '',
    build_cmd: row.build_cmd || '', image_path: row.image_path || '',
    notes: row.notes || '',
    chip_model: row.chip_model || '', float_abi: row.float_abi || 'auto', has_fpu: !!row.has_fpu,
    build_server_id: row.build_server_id || null,
    new_type_code: row.type_code,
    flash_method: row.flash_method || 'db',
    feature_filter: parseFeatureFilter(row.feature_filter),
    db_project_name: row.db_project_name || '',
    db_features: [], db_probing: false,
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
        notes: ptEditRow.value.notes,
        chip_model: ptEditRow.value.chip_model, float_abi: ptEditRow.value.float_abi,
        has_fpu: ptEditRow.value.has_fpu, build_server_id: ptEditRow.value.build_server_id,
        ...ptFlashPayload(ptEditRow.value),
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
    if (r && r.name_warning) { ElMessage.warning(r.name_warning) }
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
    defconfig: '', sdk_repo_cmd: '', build_cmd: '', image_path: '', notes: '',
    chip_model: '', float_abi: 'auto', has_fpu: false, build_server_id: null, copy_from: '',
    flash_method: 'db', feature_filter: [], db_project_name: '', db_features: [], db_probing: false,
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
    notes: tpl.notes || '',
    chip_model: tpl.chip_model || '', float_abi: tpl.float_abi || 'auto',
    has_fpu: !!tpl.has_fpu, build_server_id: tpl.build_server_id || null,
    copy_from: code,
    flash_method: tpl.flash_method || 'db',
    feature_filter: parseFeatureFilter(tpl.feature_filter),
    db_project_name: tpl.db_project_name || '',
    db_features: [], db_probing: false,
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
        image_path: row.image_path, notes: row.notes,
        chip_model: row.chip_model, float_abi: row.float_abi, has_fpu: row.has_fpu,
        build_server_id: row.build_server_id,
        ...ptFlashPayload(row),
      }),
    }).then((r) => { if (r && r.name_warning) { ElMessage.warning(r.name_warning) } })
    // 派生：后端已在一次请求里整行复制（含工程字段），不用再 PUT；普通新增仍走两步。
    if (!row.copy_from) {
      await api('/api/product-types/' + encodeURIComponent(code), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          defconfig: row.defconfig, sdk_repo_cmd: row.sdk_repo_cmd, build_cmd: row.build_cmd,
          image_path: row.image_path, notes: row.notes,
          chip_model: row.chip_model, float_abi: row.float_abi, has_fpu: row.has_fpu,
          build_server_id: row.build_server_id,
          ...ptFlashPayload(row),
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
      <el-menu :default-active="active === 'device' ? 'devices' : active" @select="switchPage" style="flex: 1">
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
        <!-- 统计卡：≥1400 七列一排 / 1100–1400 四列 / 800–1100 三列 / &lt;800 两列；浅色卡 -->
        <div class="device-stats-grid">
          <el-card shadow="never" class="ov-card" :class="{ 'is-active': statsFilter === 'all' }" @click="toggleStatsFilter('all')">
            <div class="ov-label">设备总量</div>
            <div class="ov-num ov-total">{{ deviceTotals.total }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="never" class="ov-card" :class="{ 'is-active': statsFilter === 'online' }" @click="toggleStatsFilter('online')">
            <div class="ov-label">在线</div>
            <div class="ov-num ov-online">{{ deviceTotals.online }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="never" class="ov-card" :class="{ 'is-active': statsFilter === 'offline' }" @click="toggleStatsFilter('offline')">
            <div class="ov-label">离线</div>
            <div class="ov-num ov-offline">{{ deviceTotals.offline }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="never" class="ov-card" :class="{ 'is-active': statsFilter === 'pending' }" @click="toggleStatsFilter('pending')">
            <div class="ov-label">待检</div>
            <div class="ov-num ov-pending">{{ deviceTotals.pending }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="never" class="ov-card" :class="{ 'is-active': statsFilter === 'occupied' }" @click="toggleStatsFilter('occupied')">
            <div class="ov-label">已分配</div>
            <div class="ov-num ov-allocated">{{ deviceTotals.occupied }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="never" class="ov-card" :class="{ 'is-active': statsFilter === 'available' }" @click="toggleStatsFilter('available')">
            <div class="ov-label">空闲</div>
            <div class="ov-num ov-idle">{{ deviceTotals.available }}</div>
            <div class="ov-sub ov-sub-spacer">&nbsp;</div>
          </el-card>
          <el-card shadow="never" class="ov-card ov-card-comres" @click="ctrlresCard = 'comres'; switchPage('ctrlres')">
            <div class="ov-label">串口资源</div>
            <div class="ov-num ov-comres">{{ comresPortStats.total }}</div>
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
            <el-button type="primary" @click="openDeviceAdd('')">＋ 添加设备</el-button>
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
            <el-button type="primary" @click="openDeviceAdd('')">＋ 添加设备</el-button>
          </el-empty>
        </div>

        <!-- 品类详情 -->
        <div v-else>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <el-button size="small" @click="backToOverview">返回概况</el-button>
            <h2 style="margin: 0">{{ detailTitle }}</h2>
            <el-tag size="small" type="info">共 {{ detailCounts.all }} 台</el-tag>
            <el-button size="small" plain :loading="cfgAuditBusy" @click="showConfigAudit">配置体检</el-button>
            <el-button type="primary" style="margin-left: auto"
              @click="openDeviceAdd(prefillType)">＋ 添加 {{ detailTitle }} 设备</el-button>
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
          <el-table v-if="detailFilteredDevices.length" :data="detailDevices" border size="small" class="device-table" height="min(70vh, 640px)" style="min-width: 1600px" @sort-change="onDeviceSortChange">
            <el-table-column label="操作" width="200">
              <template #default="{ row }">
                <el-button v-if="canEditDevice(row)" link type="primary" size="small"
                  :loading="probeBusy === row.device_id" @click="probeOne(row)">探测</el-button>
                <el-button v-if="canEditDevice(row)" link type="primary" size="small" @click="openDeviceEdit(row)">编辑</el-button>
                <el-tooltip
                  :disabled="!!row.serial_openable"
                  content="该设备当前无可用串口（未绑定或不可达）"
                  placement="top">
                  <span style="display:inline-block">
                    <el-button link type="primary" size="small"
                      :disabled="!row.serial_openable"
                      @click="openDeviceWlBindings(row)">串口</el-button>
                  </span>
                </el-tooltip>
                <el-button v-if="canEditDevice(row)" link type="danger" size="small" @click="removeDevice(row)">移除</el-button>
              </template>
            </el-table-column>
            <el-table-column prop="device_id" label="ID" width="115" show-overflow-tooltip>
              <template #default="{ row }">
                <a class="dev-link" href="javascript:;" @click.prevent="openDevicePageFromList(row)">{{ row.device_id }}</a>
              </template>
            </el-table-column>
            <el-table-column prop="display_name" label="名称" width="130" show-overflow-tooltip>
              <template #default="{ row }">
                <a class="dev-link" href="javascript:;" @click.prevent="openDevicePageFromList(row)">{{ row.display_name || row.device_id }}</a>
              </template>
            </el-table-column>
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
            <el-table-column label="电源" width="110">
              <template #default="{ row }">
                <template v-if="listPowerN(row) == null">
                  <span style="color:#c0c4cc">-</span>
                </template>
                <el-popover
                  v-else
                  placement="bottom-start"
                  :width="420"
                  trigger="click"
                  @show="onListPowerPopoverShow(row)"
                >
                  <template #reference>
                    <el-tag
                      v-if="listPowerN(row) > 0"
                      size="small"
                      type="success"
                      style="cursor:pointer"
                      @click.stop
                    >电源 {{ listPowerN(row) }}</el-tag>
                    <el-tag
                      v-else
                      size="small"
                      type="info"
                      effect="plain"
                      style="cursor:pointer"
                      @click.stop
                    >无电源</el-tag>
                  </template>
                  <div class="list-power-pop" @click.stop>
                    <template v-if="listPowerN(row) === 0">
                      <el-empty description="该设备未绑定电源通道" :image-size="48">
                        <el-button
                          v-if="me.role === 'admin'"
                          size="small"
                          type="primary"
                          @click="openDevicePageFromList(row)"
                        >去设备页绑定</el-button>
                      </el-empty>
                    </template>
                    <template v-else>
                      <div v-if="listPowerCache(row.device_id).loading" style="padding:12px;color:#909399;font-size:12px">加载中…</div>
                      <div v-else-if="listPowerCache(row.device_id).error" style="padding:8px;color:#f56c6c;font-size:12px">
                        {{ listPowerCache(row.device_id).error }}
                      </div>
                      <div v-else-if="!(listPowerCache(row.device_id).items || []).length">
                        <el-empty description="暂无电源能力项" :image-size="40" />
                      </div>
                      <div v-else>
                        <div
                          v-for="it in listPowerCache(row.device_id).items"
                          :key="it.id || listPowerItemTitle(it)"
                          class="list-power-item"
                          style="margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #ebeef5"
                        >
                          <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:wrap;margin-bottom:6px">
                            <span style="font-size:12px;word-break:break-all">{{ listPowerItemTitle(it) }}</span>
                            <el-tag size="small" :type="capStatusTagType(it)">{{ capStatusLabel(it) }}</el-tag>
                          </div>
                          <el-tooltip
                            content="需持有目标设备租约"
                            :disabled="!listPowerLeaseBlocked(row)"
                            placement="top"
                          >
                            <span style="display:inline-flex;flex-wrap:wrap;align-items:center;gap:4px">
                              <el-button
                                size="small"
                                type="success"
                                :loading="deviceIsActionBusy('list:' + row.device_id + ':' + (it.id || 'cap'), '*')"
                                :disabled="listPowerLeaseBlocked(row) || deviceIsActionBusy('list:' + row.device_id + ':' + (it.id || 'cap'), '*')"
                                @click.stop="doListCapPower(row, it, 'power_on')"
                              >开</el-button>
                              <el-button
                                size="small"
                                type="warning"
                                :loading="deviceIsActionBusy('list:' + row.device_id + ':' + (it.id || 'cap'), '*')"
                                :disabled="listPowerLeaseBlocked(row) || deviceIsActionBusy('list:' + row.device_id + ':' + (it.id || 'cap'), '*')"
                                @click.stop="doListCapPower(row, it, 'power_off')"
                              >关</el-button>
                              <el-button
                                size="small"
                                :loading="deviceIsActionBusy('list:' + row.device_id + ':' + (it.id || 'cap'), '*')"
                                :disabled="listPowerLeaseBlocked(row) || deviceIsActionBusy('list:' + row.device_id + ':' + (it.id || 'cap'), '*')"
                                @click.stop="doListCapPower(row, it, 'power_cycle')"
                              >重启</el-button>
                              <el-button
                                v-if="deviceFailPhase('list:' + row.device_id + ':' + (it.id || 'cap'), '*') === 'power_on'"
                                size="small"
                                type="danger"
                                plain
                                :disabled="listPowerLeaseBlocked(row)"
                                @click.stop="doListCapPower(row, it, 'power_on')"
                              >补偿开</el-button>
                            </span>
                          </el-tooltip>
                        </div>
                        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:4px">
                          <span class="comres-muted" style="font-size:11px">hold</span>
                          <el-input-number
                            size="small"
                            v-model="listPowerHoldMs"
                            :min="500"
                            :max="30000"
                            :step="500"
                            controls-position="right"
                            style="width:110px"
                          />
                          <span class="comres-muted" style="font-size:11px">ms</span>
                        </div>
                      </div>
                      <div style="margin-top:10px;display:flex;gap:12px;flex-wrap:wrap">
                        <el-button size="small" link type="primary" @click.stop="openDevicePageFromList(row)">查看设备页</el-button>
                        <el-button
                          v-if="me.role === 'admin'"
                          size="small"
                          link
                          type="primary"
                          @click.stop="openDevicePageFromList(row)"
                        >去绑定</el-button>
                      </div>
                    </template>
                  </div>
                </el-popover>
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
                v-if="!detailCounts.all"
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

      <!-- PT3b：设备独立详情页 #/device/{id} -->
      <div v-else-if="active === 'device'" class="dev-page" v-loading="devicePageLoading">
        <div class="dev-head">
          <div class="dev-head-left">
            <el-button size="small" @click="backToDeviceCenter">← 返回设备中心</el-button>
            <template v-if="devicePage">
              <h2 class="dev-title">{{ devicePage.display_name || devicePage.device_id }}</h2>
              <span class="kv-mono">{{ devicePage.device_id }}</span>
              <el-tag size="small" :type="statusTag[devicePage.status] || 'info'">{{ devicePage.status }}</el-tag>
              <el-tag size="small" type="info">健康 {{ devicePage.health_level || '—' }}</el-tag>
              <el-tag v-if="devicePageNodeName" size="small" effect="plain">{{ devicePageNodeName }}</el-tag>
              <span class="dev-owner">责任人 {{ devicePage.owner || '—' }}</span>
            </template>
            <template v-else-if="!devicePageLoading">
              <h2 class="dev-title">设备详情</h2>
              <span class="kv-mono">{{ devicePageId }}</span>
            </template>
          </div>
          <div v-if="devicePage" class="dev-head-actions">
            <el-button v-if="canEditDevice(devicePage)" size="small" type="primary"
              :loading="probeBusy === devicePage.device_id" @click="probeOne(devicePage)">探测</el-button>
            <el-button v-if="me.role === 'admin'" size="small"
              :loading="screenshotBusy === devicePage.device_id" @click="takeScreenshot(devicePage)">截图</el-button>
            <el-button v-if="me.role === 'admin'" size="small"
              @click="openScreenshotHistory(devicePage)">记录</el-button>
            <el-button size="small" :disabled="!devicePage.serial_openable"
              @click="openDeviceWlBindings(devicePage)">串口控制台</el-button>
            <el-button size="small" :loading="devicePageLoading || deviceCapsLoading || deviceSerialLoading || devicePowerLoading"
              @click="refreshDevicePage">刷新</el-button>
          </div>
        </div>

        <el-empty v-if="!devicePage && !devicePageLoading"
          :description="devicePageError || '设备不存在或已移除'">
          <el-button type="primary" @click="backToDeviceCenter">返回设备中心</el-button>
        </el-empty>

        <el-row v-else-if="devicePage" :gutter="16">
          <el-col :xs="24" :sm="24" :md="16">
            <el-card shadow="never" class="dev-section">
              <template #header><span>指纹</span></template>
              <el-descriptions :column="3" border size="small" class="dev-desc">
                <el-descriptions-item label="设备 ID"><span class="kv-mono">{{ devicePage.device_id }}</span></el-descriptions-item>
                <el-descriptions-item label="名称">{{ devicePage.display_name || '—' }}</el-descriptions-item>
                <el-descriptions-item label="机型">{{ devicePage.model_class || '—' }}</el-descriptions-item>
                <el-descriptions-item label="节点">{{ devicePageNodeName || '—' }}</el-descriptions-item>
                <el-descriptions-item label="位置">{{ devicePage.position_code || '—' }}</el-descriptions-item>
                <el-descriptions-item label="责任人">{{ devicePage.owner || '—' }}</el-descriptions-item>
                <el-descriptions-item label="Agent">{{ devicePage.register_agent || '—' }}</el-descriptions-item>
                <el-descriptions-item label="ADB"><span class="kv-mono">{{ devicePage.adb_address || '—' }}</span></el-descriptions-item>
                <el-descriptions-item label="IP"><span class="kv-mono">{{ devicePageIp(devicePage) }}</span></el-descriptions-item>
                <el-descriptions-item label="状态">
                  <el-tag size="small" :type="statusTag[devicePage.status] || 'info'">{{ devicePage.status }}</el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="健康">{{ devicePage.health_level || '—' }}</el-descriptions-item>
                <el-descriptions-item label="共享">{{ devicePage.share_scope || '—' }}</el-descriptions-item>
                <el-descriptions-item label="锁定">
                  <template v-if="devicePage.locked">
                    <el-tag size="small" type="warning">锁定</el-tag>
                    <span v-if="devicePage.locked_reason" style="margin-left:6px">{{ devicePage.locked_reason }}</span>
                  </template>
                  <span v-else>—</span>
                </el-descriptions-item>
                <el-descriptions-item label="最近探测">{{ devicePage.last_probe_at ? formatLocal(devicePage.last_probe_at) : '从未探测' }}</el-descriptions-item>
                <el-descriptions-item label="配置指纹">
                  <el-tooltip v-if="devicePage.config && devicePage.config.state === 'fingerprinted'"
                    :content="cfgTooltip(devicePage.config)" placement="top">
                    <el-tag size="small" :type="cfgTagType(devicePage.config.verdict)">
                      {{ devicePage.config.defconfig_short || '?' }}
                      <span v-if="devicePage.config.verdict"> · {{ devicePage.config.verdict }}</span>
                    </el-tag>
                  </el-tooltip>
                  <el-tooltip v-else-if="devicePage.config && devicePage.config.state === 'legacy'"
                    content="能连但无 build-info（老固件）" placement="top">
                    <el-tag size="small" type="info" effect="plain">老固件</el-tag>
                  </el-tooltip>
                  <span v-else style="color:#c0c4cc">—</span>
                </el-descriptions-item>
              </el-descriptions>
            </el-card>

            <el-card shadow="never" class="dev-section" v-loading="deviceCapsLoading">
              <template #header>
                <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap">
                  <span>能力</span>
                  <span class="comres-muted" style="font-size:12px">
                    <template v-if="deviceCaps.enabled === false">已关闭</template>
                    <template v-else>revision {{ deviceCaps.revision || 0 }}</template>
                  </span>
                </div>
              </template>
              <el-empty v-if="deviceCaps.enabled === false" description="能力开关已关闭" :image-size="64" />
              <el-empty v-else-if="!(deviceCapsFlat || []).length" description="暂无能力项" :image-size="64" />
              <el-row v-else :gutter="12" class="cap-row">
                <el-col
                  v-for="it in deviceCapsFlat"
                  :key="it.id || (it.kind + '-' + (it.label || ''))"
                  :xs="24" :sm="12" :md="8" :lg="8"
                  class="cap-col"
                >
                  <div class="cap-card cap-card--stretch" :class="{ 'cap-card--dim': !capActionsEnabled(it) }">
                    <div class="cap-card-head">
                      <span class="cap-card-name">{{ it.label || it.name || it.id || '—' }}</span>
                      <div class="cap-card-tags">
                        <el-tag size="small" effect="plain">{{ capKindLabel(it.kind) }}</el-tag>
                        <el-tag size="small" :type="capStatusTagType(it)">{{ capStatusLabel(it) }}</el-tag>
                      </div>
                    </div>
                    <div class="cap-card-row">
                      <span class="comres-muted">端点</span>
                      <el-tooltip :content="capEndpointText(it) || '—'" placement="top" :disabled="!capEndpointText(it)">
                        <span class="kv-mono cap-ep">{{ capEndpointText(it) || '—' }}</span>
                      </el-tooltip>
                    </div>
                    <div class="cap-card-row">
                      <span class="comres-muted">来源</span>
                      <span>{{ capSourceLabel(it) }}</span>
                    </div>
                    <div v-if="it.kind === 'serial.io' && capHealthText(it)" class="cap-card-row">
                      <span class="comres-muted">健康</span>
                      <span>{{ capHealthText(it) }}</span>
                    </div>
                    <div v-if="it.kind === 'serial.io'" class="cap-card-row">
                      <span class="comres-muted">占用</span>
                      <el-tooltip
                        :content="capHolderText(it) || '空闲'"
                        placement="top"
                        :disabled="!capHolderText(it)"
                      >
                        <span :class="capHolderText(it) ? '' : 'comres-muted'">{{ capHolderText(it) || '—' }}</span>
                      </el-tooltip>
                    </div>
                    <div v-if="capConstraintOnly(it)" class="cap-card-row">
                      <span class="comres-muted">约束</span>
                      <span>{{ capConstraintOnly(it) }}</span>
                    </div>

                    <!-- 电源：唯一操作入口 -->
                    <template v-if="it.kind === 'power'">
                      <div class="cap-card-row">
                        <span class="comres-muted">重启间隔</span>
                        <span>{{ capRestartHoldText(it) }}</span>
                      </div>
                      <div class="cap-card-actions">
                        <el-button size="small" type="success"
                          :loading="deviceIsActionBusy(it.id, '*')"
                          :disabled="!capActionsEnabled(it) || deviceIsActionBusy(it.id, '*') || devicePowerLeaseBlocked()"
                          @click="doDeviceCapPower(it, 'power_on')">开</el-button>
                        <el-button size="small" type="warning"
                          :loading="deviceIsActionBusy(it.id, '*')"
                          :disabled="!capActionsEnabled(it) || deviceIsActionBusy(it.id, '*') || devicePowerLeaseBlocked()"
                          @click="doDeviceCapPower(it, 'power_off')">关</el-button>
                        <el-button size="small"
                          :loading="deviceIsActionBusy(it.id, '*')"
                          :disabled="!capActionsEnabled(it) || deviceIsActionBusy(it.id, '*') || devicePowerLeaseBlocked()"
                          @click="doDeviceCapPower(it, 'power_cycle')">重启</el-button>
                        <el-button v-if="deviceFailPhase(it.id, '*') === 'power_on'" size="small" type="danger" plain
                          :loading="deviceIsActionBusy(it.id, '*')"
                          :disabled="devicePowerLeaseBlocked()"
                          @click="doDeviceCapPower(it, 'power_on')">补偿开</el-button>
                      </div>
                      <el-collapse class="cap-detail-collapse">
                        <el-collapse-item title="详情" name="detail">
                          <div class="cap-card-row"><span class="comres-muted">已绑对象</span>
                            <span>{{ capPowerMeta(it).power_dev || '—' }}{{ capPowerMeta(it).channel != null ? (' CH' + capPowerMeta(it).channel) : '' }}</span>
                          </div>
                          <div class="cap-card-row"><span class="comres-muted">slot</span>
                            <span>{{ capPowerMeta(it).slot != null ? capPowerMeta(it).slot : '—' }}</span>
                          </div>
                          <div class="cap-card-row"><span class="comres-muted">位置</span>
                            <span>{{ capPowerMeta(it).location || '—' }}</span>
                          </div>
                          <div class="cap-card-row"><span class="comres-muted">来源</span>
                            <span>{{ capPowerMeta(it).source || '—' }}</span>
                          </div>
                          <div class="cap-card-row"><span class="comres-muted">enabled</span>
                            <span v-if="capPowerMeta(it).enabled === false">停用</span>
                            <span v-else-if="capPowerMeta(it).enabled === true">启用</span>
                            <span v-else>—</span>
                          </div>
                        </el-collapse-item>
                      </el-collapse>
                    </template>

                    <!-- 串口：控制台 -->
                    <template v-else-if="it.kind === 'serial.io'">
                      <div class="cap-card-actions">
                        <el-tooltip
                          content="该端口未绑定节点"
                          placement="top"
                          :disabled="!!capSerialConsoleTarget(it)"
                        >
                          <span>
                            <el-button size="small" type="primary"
                              :disabled="!capSerialConsoleTarget(it)"
                              @click="openCapSerialConsole(it)">控制台</el-button>
                          </span>
                        </el-tooltip>
                      </div>
                    </template>

                    <!-- ADB：调试区 -->
                    <template v-else-if="it.kind === 'adb'">
                      <div class="cap-card-actions">
                        <el-button size="small" type="primary"
                          :disabled="!capActionsEnabled(it)"
                          @click="adbPanelOpen = !adbPanelOpen">{{ adbPanelOpen ? '收起调试' : 'ADB 调试' }}</el-button>
                      </div>
                      <div v-if="adbPanelOpen" class="adb-debug">
                        <div class="adb-quick">
                          <span class="comres-muted" style="font-size:11px">常用</span>
                          <el-button
                            v-for="q in ADB_QUICK_CMDS" :key="q.cmd" size="small" link type="primary"
                            @click="setAdbQuickCmd(q.cmd)">{{ q.label }}</el-button>
                        </div>
                        <el-input
                          v-model="adbCmd"
                          size="small"
                          placeholder="adb shell 命令，Enter 执行"
                          :disabled="adbBusy"
                          @keyup.enter="onAdbCmdEnter"
                        >
                          <template #append>
                            <el-button :loading="adbBusy" @click="runDeviceAdbExec({ command: adbCmd })">执行</el-button>
                          </template>
                        </el-input>
                        <p v-if="adbErr" class="adb-err">{{ adbErr }}</p>
                        <pre v-if="adbOut" class="adb-out">rc={{ adbOut.rc }} · {{ adbOut.duration_ms }}ms{{ adbOut.truncated ? ' · truncated' : '' }}
{{ adbOut.stdout || '' }}{{ adbOut.stderr ? ('\n[stderr]\n' + adbOut.stderr) : '' }}</pre>
                      </div>
                    </template>

                    <!-- mode 等其它 kind：保留动作按钮 -->
                    <template v-else>
                      <div v-if="capKindActions(it).length" class="cap-card-actions">
                        <el-button v-for="act in capKindActions(it)" :key="act.action_key" size="small"
                          :loading="deviceIsActionBusy(it.id, '*')"
                          :disabled="!capActionsEnabled(it) || deviceIsActionBusy(it.id, '*')"
                          @click="doDeviceCapAction(it, act)">{{ act.display_name || act.action_key }}</el-button>
                      </div>
                    </template>
                  </div>
                </el-col>
              </el-row>
            </el-card>

            <!-- 非 admin：电源绑定只读摘要 -->
            <el-card v-if="me.role !== 'admin'" shadow="never" class="dev-section"
              v-loading="devicePowerLoading">
              <template #header><span>电源绑定</span></template>
              <p class="comres-muted" style="margin:0;font-size:13px;line-height:1.5">{{ devicePowerBindSummary }}</p>
            </el-card>

            <!-- admin：电源配置（默认折叠） -->
            <el-card v-if="me.role === 'admin'" shadow="never" class="dev-section"
              v-loading="devicePowerLoading || devicePowerInvLoading">
              <template #header><span>电源配置</span></template>
              <el-collapse v-model="devicePowerConfigOpen" class="dev-power-config">
                <el-collapse-item title="展开以管理节点与绑定" name="config">
                  <div class="dev-power-head" style="margin-bottom:10px">
                    <div class="dev-power-node">
                      <template v-if="devicePowerNodeId && !devicePowerNodePicking">
                        <el-tag size="small" effect="plain">节点 {{ devicePowerNodeLabel(devicePowerNodeId) }}</el-tag>
                        <el-button size="small" link type="primary" @click="startDevicePowerNodeReselect">重新选择</el-button>
                      </template>
                      <el-select
                        v-else
                        :model-value="devicePowerNodeId"
                        size="small"
                        filterable
                        clearable
                        placeholder="请先选择设备所在节点"
                        style="width:220px"
                        @change="onDevicePowerNodePick"
                      >
                        <el-option
                          v-for="n in (prNodes || [])"
                          :key="n.node_id"
                          :label="(n.display_name || n.node_id) + ' (' + n.node_id + ')'"
                          :value="n.node_id"
                        />
                      </el-select>
                    </div>
                  </div>
                  <el-alert v-if="devicePowerAlert" :title="devicePowerAlert" type="success" show-icon
                    style="margin-bottom:12px" @close="devicePowerAlert = ''" />
                  <el-alert
                    v-if="!devicePowerNodeId"
                    title="请先选择设备所在节点"
                    type="warning"
                    show-icon
                    :closable="false"
                    style="margin-bottom:12px"
                  />

                  <el-empty v-if="devicePowerFullEmpty"
                    description="该节点暂无电源通道；如需新增，请在 CtrlRes → PowerRes 手工登记或让 hub 上报">
                    <el-button type="primary" size="small" @click="goBindPowerRes">打开 PowerRes 卡片</el-button>
                  </el-empty>

                  <template v-else>
                    <div v-if="(devicePowerBindings || []).length" class="dev-power-bound">
                      <div class="cap-kind-title">已绑定到本机</div>
                      <el-table :data="devicePowerBindings" border size="small">
                        <el-table-column prop="power_dev" label="电源设备" min-width="110" show-overflow-tooltip />
                        <el-table-column label="通道" width="70">
                          <template #default="{ row }">CH{{ row.channel }}</template>
                        </el-table-column>
                        <el-table-column prop="endpoint" label="端点" min-width="140" show-overflow-tooltip>
                          <template #default="{ row }"><span class="kv-mono">{{ row.endpoint || '—' }}</span></template>
                        </el-table-column>
                        <el-table-column label="位置" min-width="120" show-overflow-tooltip>
                          <template #default="{ row }">{{ prLocationText(row) }}</template>
                        </el-table-column>
                        <el-table-column label="来源" min-width="130" show-overflow-tooltip>
                          <template #default="{ row }">{{ prSourceText(row) || '—' }}</template>
                        </el-table-column>
                        <el-table-column label="槽位" width="60">
                          <template #default="{ row }">{{ row.slot != null ? row.slot : '—' }}</template>
                        </el-table-column>
                        <el-table-column prop="label" label="label" min-width="100" show-overflow-tooltip>
                          <template #default="{ row }">{{ row.label || '—' }}</template>
                        </el-table-column>
                        <el-table-column label="状态" width="80">
                          <template #default="{ row }">
                            <el-tag v-if="row.enabled === false" size="small" type="info">停用</el-tag>
                            <el-tag v-else size="small" type="success">启用</el-tag>
                          </template>
                        </el-table-column>
                        <el-table-column label="操作" width="90" fixed="right">
                          <template #default="{ row }">
                            <el-button size="small" type="danger" link @click="doDevicePowerUnbind(row)">解绑</el-button>
                          </template>
                        </el-table-column>
                      </el-table>
                    </div>
                    <el-empty v-else-if="devicePowerNodeId" description="该设备尚未绑定电源通道" :image-size="48"
                      style="padding:8px 0 4px" />

                    <el-collapse v-if="devicePowerNodeId" v-model="devicePowerCandOpen" class="dev-power-cand">
                      <el-collapse-item title="可绑定通道" name="candidates">
                        <el-table v-if="(devicePowerCandidates || []).length" :data="devicePowerCandidates" border size="small">
                          <el-table-column prop="power_dev" label="电源设备" min-width="110" show-overflow-tooltip>
                            <template #default="{ row }">{{ row.name || row.power_dev }}</template>
                          </el-table-column>
                          <el-table-column label="通道" width="70">
                            <template #default="{ row }">CH{{ row.channel }}</template>
                          </el-table-column>
                          <el-table-column label="端点" min-width="140" show-overflow-tooltip>
                            <template #default="{ row }"><span class="kv-mono">{{ row.endpoint || '—' }}</span></template>
                          </el-table-column>
                          <el-table-column label="位置" min-width="120" show-overflow-tooltip>
                            <template #default="{ row }">{{ prLocationText(row) }}</template>
                          </el-table-column>
                          <el-table-column label="来源" min-width="130" show-overflow-tooltip>
                            <template #default="{ row }">{{ prSourceText(row) || '—' }}</template>
                          </el-table-column>
                          <el-table-column label="当前状态" width="90">
                            <template #default="{ row }">{{ row.state || '—' }}</template>
                          </el-table-column>
                          <el-table-column label="绑定情况" min-width="140">
                            <template #default="{ row }">
                              <el-tag v-if="prBoundDeviceId(row)" size="small" type="warning">
                                已绑 {{ prDeviceName(prBoundDeviceId(row)) || prBoundDeviceId(row) }}
                              </el-tag>
                              <span v-else class="comres-muted">未绑定</span>
                            </template>
                          </el-table-column>
                          <el-table-column label="操作" width="100" fixed="right">
                            <template #default="{ row }">
                              <el-tooltip
                                v-if="!(row.endpoint && String(row.endpoint).trim())"
                                content="该通道端点缺失，请在 PowerRes 卡片补登记"
                                placement="top"
                              >
                                <span>
                                  <el-button size="small" type="primary" disabled>绑定</el-button>
                                </span>
                              </el-tooltip>
                              <el-button
                                v-else
                                size="small"
                                type="primary"
                                :loading="devicePowerBindBusy === devicePowerCandRowKey(row)"
                                :disabled="!!devicePowerBindBusy || !devicePowerNodeId"
                                @click="doDevicePowerBind(row)"
                              >绑定</el-button>
                            </template>
                          </el-table-column>
                        </el-table>
                        <p v-else class="comres-muted" style="font-size:12px;margin:4px 0">
                          该节点暂无可绑定通道（均已绑本机，或尚无登记）。
                        </p>
                      </el-collapse-item>
                    </el-collapse>
                  </template>
                </el-collapse-item>
              </el-collapse>
            </el-card>
          </el-col>

          <el-col :xs="24" :sm="24" :md="8">
            <el-card shadow="never" class="dev-section">
              <template #header><span>健康摘要</span></template>
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item label="健康级别">{{ devicePage.health_level || '—' }}</el-descriptions-item>
                <el-descriptions-item label="状态">
                  <el-tag size="small" :type="statusTag[devicePage.status] || 'info'">{{ devicePage.status }}</el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="最近探测">{{ devicePage.last_probe_at ? formatLocal(devicePage.last_probe_at) : '从未探测' }}</el-descriptions-item>
                <el-descriptions-item label="最近在线">{{ devicePage.last_seen_at ? formatLocal(devicePage.last_seen_at) : '—' }}</el-descriptions-item>
                <el-descriptions-item label="串口可用">{{ devicePage.serial_openable ? '是' : '否' }}（{{ devicePage.serial_ports || 0 }}）</el-descriptions-item>
                <el-descriptions-item label="电源绑定">{{ (devicePowerBindings || []).length }} 路</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
        </el-row>
      </div>

      <!-- 资源调度：占用 / 排队 / 异常 -->
      <div v-else-if="active === 'scheduling'">
        <h2>资源调度</h2>
        <el-tabs v-model="schedulingTab">
          <el-tab-pane label="占用" name="leases">
        <el-table :data="leases" border>
          <el-table-column prop="lease_id" label="租约" width="140" />
          <el-table-column label="设备" min-width="210">
            <template #default="{ row }">
              <div class="sched-dev">
                <div class="sched-dev-name">{{ row.display_name || '未命名设备' }}</div>
                <el-link v-if="row.device_id" type="primary" :underline="false"
                         class="sched-dev-id"
                         @click="enterDevicePage(row.device_id)">{{ row.device_id }}</el-link>
                <span v-else class="sched-dev-id">-</span>
              </div>
            </template>
          </el-table-column>
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
              <el-table-column label="权限" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.role === 'admin' ? 'danger' : 'info'" effect="plain">{{ row.role }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="token_prefix" label="token 前缀" width="140" />
              <el-table-column prop="agent_label" label="Agent" width="140" />
              <el-table-column prop="note" label="备注" width="140" />
              <el-table-column label="到期时间" width="200"><template #default="{ row }">{{ formatLocal(row.expires_at) }}</template></el-table-column>
              <el-table-column label="创建时间" width="200"><template #default="{ row }">{{ formatLocal(row.created_at) }}</template></el-table-column>
              <el-table-column label="操作" width="260" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" @click="renewCredential(row.id)">续期</el-button>
                  <el-button size="small" type="danger" @click="revokeCredential(row.id)">吊销</el-button>
                  <el-button
                    v-if="me && me.role === 'admin' && row.role === 'user'"
                    size="small" type="primary" plain
                    @click="setCredentialRole(row, 'admin')"
                  >设为管理员</el-button>
                  <el-button
                    v-if="me && me.role === 'admin' && row.role === 'admin' && !isSelfCredentialRow(row)"
                    size="small" type="warning" plain
                    @click="setCredentialRole(row, 'user')"
                  >设为普通用户</el-button>
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

      <!-- CtrlRes：一页两卡片（el-tabs）。选 tabs 而非并列 el-card：与 ?card= 深链一一对应，且 COMRes 内部已有多层 el-card，避免再套一层。 -->
      <div v-else-if="active === 'ctrlres'">
        <el-tabs v-model="ctrlresCard" @tab-change="onCtrlresCardChange">
          <el-tab-pane label="COMRes" name="comres">
        <!-- 串口恢复对话框（readonly / restart_adbd / reboot） -->
        <el-dialog v-model="recoverDialog" title="串口恢复" width="720px" append-to-body>
          <div v-if="recoverDevice">
            <p style="margin:0 0 8px">
              设备 <strong>{{ recoverDevice.identity.display_name || recoverDevice.device_id }}</strong>
              · {{ recoverChannel && recoverChannel.com }}
              · {{ recoverChannel && recoverChannel.endpoint }}
            </p>
            <el-radio-group v-model="recoverLevel">
              <el-radio-button label="readonly">只读排障</el-radio-button>
              <el-radio-button label="restart_adbd">重启 adbd</el-radio-button>
              <el-radio-button label="reboot">整机重启</el-radio-button>
            </el-radio-group>
            <el-alert v-if="recoverLevel !== 'readonly'" type="warning" :closable="false" show-icon
                      style="margin-top:10px"
                      :title="recoverLevel === 'reboot'
                        ? '会整机重启，ADB 与串口会短暂断开，属正常现象'
                        : '会重启 adbd，ADB 连接短暂中断后自动恢复'" />
            <div v-if="recoverResult">
              <p style="margin:12px 0 6px">
                <el-tag size="small" :type="recoverResult.ok ? 'success' : 'danger'">
                  {{ recoverResult.ok ? '完成' : '未确认完成' }}
                </el-tag>
                <span class="comres-muted" style="font-size:12px;margin-left:8px">
                  {{ recoverResult.level_label }} · {{ recoverResult.duration_ms }}ms
                </span>
              </p>
              <pre class="recover-out">{{ recoverResult.output || '（无输出）' }}</pre>
            </div>
          </div>
          <template #footer>
            <el-button @click="recoverDialog = false">关闭</el-button>
            <el-button type="danger" :loading="recoverBusy" @click="runRecover">执行</el-button>
          </template>
        </el-dialog>

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

        <div class="comres-subtabs" role="tablist">
          <button
            v-for="t in COMRES_TABS"
            :key="t.key"
            type="button"
            role="tab"
            class="comres-subtab"
            :class="{ 'is-active': comresTab === t.key }"
            :aria-selected="comresTab === t.key"
            @click="switchComresTab(t.key)"
          >{{ t.label }}</button>
        </div>

        <el-alert v-if="comresTab === 'nodes' && wlNodesError" type="error" :closable="false" show-icon
                  :title="'节点列表：' + wlNodesError" style="margin-bottom:10px" />
        <el-alert v-if="comresTab === 'pool' && comresError" type="error" :closable="false" show-icon
                  :title="'资源池：' + comresError" style="margin-bottom:10px" />
        <el-alert v-if="comresTab === 'nodes' && wlDetailError" type="error" :closable="false" show-icon
                  :title="'节点详情：' + wlDetailError" style="margin-bottom:10px" />

        <!-- 节点视图 -->
        <template v-if="comresTab === 'nodes'">
        <el-card v-if="!wlDetail" style="margin-bottom:16px" shadow="never" class="comres-panel">
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
            <el-table-column label="授权" width="140">
              <template #default="{ row }">
                <el-tooltip
                  :content="wlAuthSourceLabel(wlNodeAuthSource(row)) || '无来源'"
                  :disabled="!wlAuthSourceLabel(wlNodeAuthSource(row))"
                  placement="top"
                >
                  <div>
                    <el-tag size="small" :type="wlAuthBadge(wlNodeAuthStatus(row)).type">
                      {{ wlAuthBadge(wlNodeAuthStatus(row)).label }}
                    </el-tag>
                    <div v-if="wlAuthSourceLabel(wlNodeAuthSource(row))"
                         class="comres-muted" style="font-size:11px;margin-top:2px">
                      {{ wlAuthSourceLabel(wlNodeAuthSource(row)) }}
                    </div>
                  </div>
                </el-tooltip>
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
            <el-table-column label="最近心跳" min-width="260">
              <template #default="{ row }">
                <span :style="wlHeartbeatTimedOut(row) ? 'color:var(--el-color-danger)' : ''">
                  {{ formatLocal(row.last_heartbeat_at) }}
                  <span v-if="row.last_heartbeat_at" class="comres-muted">（{{ formatHeartbeatAge(row) }}）</span>
                </span>
                <el-tag v-if="wlHeartbeatTimedOut(row)" size="small" type="danger" style="margin-left:6px">超时</el-tag>
              </template>
            </el-table-column>
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

        <el-card v-else style="margin-bottom:16px" shadow="never" class="comres-panel">
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
            <el-descriptions-item label="最近心跳">
              <span :style="wlHeartbeatTimedOut(wlDetail) ? 'color:var(--el-color-danger)' : ''">
                {{ wlDetail.last_heartbeat_at || '—' }}
                <span v-if="wlDetail.last_heartbeat_at" class="comres-muted">（{{ formatHeartbeatAge(wlDetail) }}）</span>
              </span>
              <el-tag v-if="wlHeartbeatTimedOut(wlDetail)" size="small" type="danger" style="margin-left:6px">超时</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="心跳间隔">
              {{ wlDetail.heartbeat_interval_s != null ? wlDetail.heartbeat_interval_s + ' 秒' : '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="心跳年龄">{{ formatHeartbeatAge(wlDetail) }}</el-descriptions-item>
            <el-descriptions-item label="在线端口数">{{ wlOnlinePortCount(wlDetail.node_id) }}</el-descriptions-item>
          </el-descriptions>
          <el-alert type="info" :closable="false" show-icon style="margin:12px 0 0"
                    title="节点凭据由客户端自助申请签发；详情可复制 node_token。宿主自行 register/heartbeat。" />

          <h3>串口资源</h3>
          <el-alert v-if="!(wlDetail.ports && wlDetail.ports.length)"
                    type="info" :closable="false" show-icon
                    title="无串口资源" class="comres-muted" style="margin-bottom:8px" />
          <el-table :data="wlDetail.ports || []" border size="small"
                    :row-class-name="({ row }) => portHasSerial(row) ? '' : 'comres-no-serial'">
            <el-table-column label="资源" min-width="150">
              <template #default="{ row }">
                <a v-if="portHasSerial(row)" class="wl-com-link" @click="openWlShell(row)">
                  {{ portTcpComLabel(row) }}<span class="wl-com-open">↗</span>
                </a>
                <span v-else class="comres-muted">无串口资源</span>
                <div class="wl-com-sub">
                  {{ row.tty || row.com_name || row.com || '—' }}
                  <span v-if="row.baud"> · {{ row.baud }} 8N1</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="接入方式" min-width="220">
              <template #default="{ row }">
                <span class="wl-endpoint">{{ portEndpointText(row) }}</span>
                <el-button size="small" link type="primary" style="margin-left:4px"
                           :disabled="!portHasSerial(row)"
                           @click="copyPortEndpoint(row)">复制</el-button>
              </template>
            </el-table-column>
            <el-table-column label="来源" min-width="150">
              <template #default="{ row }">
                <el-button size="small" link type="primary" class="wl-source-link"
                           @click="gotoPortSource(row)">{{ portSourceName(row) }}</el-button>
                <div class="wl-com-sub">{{ row.node_id || '—' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="接入设备" min-width="230">
              <template #default="{ row }">
                <template v-if="row.bound_device_id">
                  <template v-if="isBoundDeviceRetired(row)">
                    <span>{{ row.bound_device ? (row.bound_device.name || row.bound_device_id) : row.bound_device_id }}</span>
                    <el-tag type="info" size="small" style="margin-left:4px">已退役</el-tag>
                  </template>
                  <template v-else>
                    <el-button size="small" link type="primary"
                               @click="jumpToDevice(row.bound_device_id)">
                      {{ row.bound_device ? (row.bound_device.name || row.bound_device_id) : row.bound_device_id }}
                    </el-button>
                    <el-tag v-if="row.bound_device && row.bound_device.status" size="small"
                            :type="row.bound_device.status === 'online' ? 'success' : 'info'"
                            style="margin-left:4px">{{ row.bound_device.status }}</el-tag>
                  </template>
                  <div class="wl-com-sub">
                    <span v-if="row.bound_device && row.bound_device.model">
                      {{ row.bound_device.model }}{{ row.bound_device.project ? ' / ' + row.bound_device.project : '' }} ·
                    </span>
                    {{ portMac(row) || '无 MAC' }}
                  </div>
                </template>
                <template v-else>
                  <el-tag size="small" type="info">未绑定设备</el-tag>
                  <div class="wl-com-sub">
                    {{ portMac(row) ? '指纹 MAC ' + portMac(row) : '等待指纹识别' }}
                  </div>
                </template>
              </template>
            </el-table-column>
            <el-table-column label="状态" min-width="170">
              <template #default="{ row }">
                <el-tag size="small" :type="row.health_state === 'online' ? 'success' : 'info'">
                  {{ row.health_state || '未知' }}
                </el-tag>
                <el-tag v-if="row.fp_verdict" size="small" style="margin-left:4px"
                        :type="fpVerdictMeta(row.fp_verdict).type">
                  指纹 {{ fpVerdictMeta(row.fp_verdict).text }}
                </el-tag>
                <div class="wl-com-sub">
                  <el-tooltip :content="comresReachLabel(row).tip" placement="top">
                    <span :style="{ color: comresReachLabel(row).ok === true ? '#16a34a' : (comresReachLabel(row).ok === false ? '#dc2626' : undefined) }">
                      连通 {{ comresReachLabel(row).text }}
                    </span>
                  </el-tooltip>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="占用" min-width="170">
              <template #default="{ row }">
                <template v-if="portAllocated(row)">
                  <el-tag size="small" type="warning">已占用</el-tag>
                  <div class="wl-com-sub">{{ portOccupancyLabel(row) }}</div>
                </template>
                <template v-else>
                  <el-tag size="small" type="info">未分配</el-tag>
                  <div class="wl-com-sub">{{ portOccupancyLabel(row) }}</div>
                </template>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-tooltip :content="portHasFingerprint(row) ? '查看设备指纹' : '该端口未上报指纹'" placement="top">
                  <span style="display:inline-block">
                    <el-button size="small" type="primary" link
                               :disabled="!portHasFingerprint(row)"
                               @click="openFingerprintDrawer(row)">设备信息</el-button>
                  </span>
                </el-tooltip>
                <template v-if="portCanOpen(row)">
                  <el-button size="small" type="primary" link @click="openWlShell(row)">串口 Shell</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>

          <div class="wl-config-bar">
            <span class="wl-config-title">下发配置<em>（下次心跳生效）</em></span>
            <el-form inline size="small" class="wl-config-form">
              <el-form-item label="心跳">
                <el-select v-model="wlConfigForm.heartbeat_interval_s" style="width:104px">
                  <el-option :value="30" label="30 秒" />
                  <el-option :value="60" label="60 秒" />
                  <el-option :value="120" label="120 秒" />
                </el-select>
              </el-form-item>
              <el-form-item label="日志">
                <el-select v-model="wlConfigForm.log_level" style="width:96px">
                  <el-option value="debug" label="debug" />
                  <el-option value="info" label="info" />
                  <el-option value="warn" label="warn" />
                  <el-option value="error" label="error" />
                </el-select>
              </el-form-item>
              <el-form-item label="保活">
                <el-switch v-model="wlConfigForm.keepalive_enabled" size="small" />
              </el-form-item>
              <el-form-item label="保活周期">
                <el-select v-model="wlConfigForm.keepalive_interval_s" style="width:104px">
                  <el-option :value="30" label="30 秒" />
                  <el-option :value="60" label="60 秒" />
                  <el-option :value="120" label="120 秒" />
                </el-select>
              </el-form-item>
              <el-form-item label="一致性">
                <el-select v-model="wlConfigForm.consistency_interval_s" style="width:104px">
                  <el-option :value="300" label="300 秒" />
                  <el-option :value="600" label="600 秒" />
                  <el-option :value="900" label="900 秒" />
                </el-select>
              </el-form-item>
            </el-form>
            <el-button size="small" type="primary" @click="saveWlConfig">保存并下发</el-button>
          </div>
        </el-card>
        </template>

        <!-- 资源池视图：/api/comres/resources → allocated / bound / pool / inactive -->
        <el-card v-else-if="comresTab === 'pool'" shadow="never" class="comres-panel">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
              <span>资源池视图（/api/comres/resources）
                <el-tag size="small" type="warning" style="margin-left:8px">已分配 {{ comresAllocated.length }}</el-tag>
                <el-tag size="small" type="success" style="margin-left:4px">已绑定 {{ comresBound.length }}</el-tag>
                <el-tag size="small" type="info" style="margin-left:4px">池 {{ comresPool.length }}</el-tag>
                <el-tag size="small" type="info" effect="plain" style="margin-left:4px">失效 {{ comresInactive.length }}</el-tag>
                <el-tag size="small" :type="comresReachSummary.fail ? 'danger' : 'success'" style="margin-left:4px">
                  可达 {{ comresReachSummary.ok }}/{{ comresReachSummary.total }}
                </el-tag>
              </span>
              <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                <el-switch v-model="comresShowInactiveOnly" size="small"
                           inline-prompt active-text="重复/失效" inactive-text="全部" />
                <el-button size="small" type="warning" plain :loading="comresPruneBusy"
                           @click="runComresPrune">清理无效资源</el-button>
                <el-button size="small" :loading="comresLoading" @click="loadComresResources">刷新资源</el-button>
                <el-button size="small" type="primary" plain :loading="comresReachBusy"
                           @click="runComresReachability">测可达</el-button>
              </div>
            </div>
          </template>
          <el-table :data="comresAllRows" border size="small" stripe v-loading="comresLoading"
                    :row-class-name="comresRowClass" max-height="480">
            <el-table-column label="资源" min-width="140">
              <template #default="{ row }">
                <a v-if="portHasSerial(row) && !row.inactive" class="wl-com-link" @click="openWlShell(row)">
                  {{ portTcpComLabel(row) }}<span class="wl-com-open">↗</span>
                </a>
                <span v-else class="comres-muted">{{ portHasSerial(row) ? portTcpComLabel(row) : '无串口资源' }}</span>
                <div class="wl-com-sub">
                  {{ row.tty || row.com || '—' }}<span v-if="row.baud"> · {{ row.baud }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="接入方式" min-width="210">
              <template #default="{ row }">
                <span class="wl-endpoint">{{ portEndpointText(row) }}</span>
                <el-button size="small" link type="primary" style="margin-left:4px"
                           :disabled="!portHasSerial(row) || row.inactive"
                           @click="copyPortEndpoint(row)">复制</el-button>
              </template>
            </el-table-column>
            <el-table-column label="来源" min-width="150">
              <template #default="{ row }">
                <el-button size="small" link type="primary" class="wl-source-link"
                           @click="gotoPortSource(row)">{{ portSourceName(row) }}</el-button>
                <div class="wl-com-sub">{{ row.node_id }}</div>
              </template>
            </el-table-column>
            <el-table-column label="接入设备" min-width="210">
              <template #default="{ row }">
                <template v-if="row.bound_device_id || row.device_id">
                  <template v-if="isBoundDeviceRetired(row)">
                    <span>{{ row.bound_device ? (row.bound_device.name || row.bound_device_id || row.device_id) : (row.bound_device_id || row.device_id) }}</span>
                    <el-tag type="info" size="small" style="margin-left:4px">已退役</el-tag>
                  </template>
                  <el-button v-else size="small" link type="primary"
                             @click="jumpToDevice(row.bound_device_id || row.device_id)">
                    {{ row.bound_device ? (row.bound_device.name || row.bound_device_id || row.device_id) : (row.bound_device_id || row.device_id) }}
                  </el-button>
                </template>
                <el-tag v-else size="small" type="info">未绑定</el-tag>
                <div class="wl-com-sub">{{ row.mac || '无 MAC' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="状态" min-width="160">
              <template #default="{ row }">
                <el-tag size="small" :type="row.inactive ? 'info' : (row.health_state === 'online' ? 'success' : 'info')">
                  {{ row.inactive ? comresInactiveLabel(row) : (row.health_state || '未知') }}
                </el-tag>
                <el-tag v-if="row.fp_verdict" size="small" style="margin-left:4px"
                        :type="fpVerdictMeta(row.fp_verdict).type">
                  指纹 {{ fpVerdictMeta(row.fp_verdict).text }}
                </el-tag>
                <div class="wl-com-sub">
                  <el-tooltip :content="comresReachLabel(row).tip" placement="top">
                    <span :style="{ color: comresReachLabel(row).ok === true ? '#16a34a' : (comresReachLabel(row).ok === false ? '#dc2626' : undefined) }">
                      连通 {{ comresReachLabel(row).text }}
                    </span>
                  </el-tooltip>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="占用" min-width="190">
              <template #default="{ row }">
                <el-tag size="small" :type="row.allocated ? 'warning' : 'info'">
                  {{ row.allocated ? '已占用' : '未分配' }}
                </el-tag>
                <div class="wl-com-sub">
                  {{ comresOccupancyLabel(row) }}
                  <span v-if="row.allocated && row.allocated_expire"> · 到期 {{ formatLocal(row.allocated_expire) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="180" fixed="right">
              <template #default="{ row }">
                <el-tooltip :content="portHasFingerprint(row) ? '查看设备指纹' : '该端口未上报指纹'" placement="top">
                  <span style="display:inline-block">
                    <el-button size="small" type="primary" link
                               :disabled="!portHasFingerprint(row)"
                               @click="openFingerprintDrawer(row)">设备信息</el-button>
                  </span>
                </el-tooltip>
                <el-button v-if="portCanOpen(row) && !row.inactive" size="small" type="primary" link
                           @click="openWlShell(row)">串口 Shell</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!comresAllRows.length && !comresLoading && !comresError"
                    description="暂无 COMRes 资源" />
          <p class="comres-muted" style="font-size:12px;margin:8px 0 0">
            自检=节点本地探测；可达=TRES 到 endpoint 的连通性；指纹=standalone 上报的设备指纹（fp.v1）。可分配池仅含已授权节点端口。
          </p>
        </el-card>

        <!-- 设备资源包：/api/comres/bundles，ADB 与串口成套 -->
        <el-card v-else-if="comresTab === 'bundles'" shadow="never" class="comres-panel">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
              <span>设备资源包（/api/comres/bundles）
                <el-tag size="small" type="success" style="margin-left:8px">
                  成套 {{ bundleData ? bundleData.summary.with_serial : 0 }} 台
                </el-tag>
                <el-tag size="small" type="info" style="margin-left:4px">
                  串口绑定 {{ bundleData ? bundleData.summary.serial_bound : 0 }}
                </el-tag>
                <el-tag size="small" type="warning" effect="plain" style="margin-left:4px">
                  未归属 {{ bundleData ? bundleData.summary.serial_unattached : 0 }}
                </el-tag>
              </span>
              <div style="display:flex;align-items:center;gap:8px">
                <el-switch v-model="bundleOnlySerial" size="small"
                           inline-prompt active-text="有串口" inactive-text="全部" />
                <el-button size="small" :loading="bundleLoading" @click="loadComresBundles">刷新</el-button>
              </div>
            </div>
          </template>
          <el-alert v-if="bundleError" type="error" :closable="false" show-icon
                    :title="'资源包：' + bundleError" style="margin-bottom:10px" />
          <div v-loading="bundleLoading">
            <div v-for="b in bundleRows" :key="b.device_id" class="bundle-card">
              <div class="bundle-head">
                <el-button size="small" link type="primary" @click="jumpToDevice(b.device_id)">
                  {{ b.identity.display_name || b.device_id }}
                </el-button>
                <el-tag size="small" :type="b.status.status === 'available' ? 'success' : 'info'">
                  {{ b.status.status }}
                </el-tag>
                <el-tag size="small" effect="plain">{{ b.status.health_level }}</el-tag>
                <el-tag v-if="b.lease" size="small" type="warning">
                  已租给 {{ b.lease.agent_label || '—' }}
                </el-tag>
                <span class="comres-muted" style="font-size:12px">
                  {{ b.identity.model_name || b.identity.model_class || '未分类' }}
                  · {{ b.identity.mac || '无 MAC' }}
                </span>
              </div>
              <div class="bundle-row">
                <span class="bundle-label">ADB</span>
                <span class="wl-endpoint">{{ b.channels.adb.address }}</span>
                <span class="comres-muted" style="font-size:12px">ip+port</span>
              </div>
              <div v-if="b.channels.serial.length" class="bundle-row" style="align-items:flex-start">
                <span class="bundle-label">串口</span>
                <div style="flex:1">
                  <div v-for="ch in b.channels.serial" :key="ch.com_key" class="bundle-serial">
                    <a class="wl-com-link" @click="openWlShell({
                      node_id: ch.source.node_id, slot: ch.source.slot,
                      node_ip: ch.source.node_ip, tcp_port: ch.tcp_port,
                      com: ch.com, tty: ch.com, baud: ch.baud,
                      endpoint: ch.endpoint, health_state: ch.health_state, inactive: false,
                    })">{{ ch.com }}<span class="wl-com-open">↗</span></a>
                    <span class="wl-endpoint" style="margin-left:8px">{{ bundleSerialEndpoint(ch) }}</span>
                    <el-tag size="small" :type="ch.health_state === 'online' ? 'success' : 'info'"
                            style="margin-left:8px">{{ ch.health_state || '未知' }}</el-tag>
                    <el-tag v-if="ch.fp_verdict" size="small" style="margin-left:4px"
                            :type="fpVerdictMeta(ch.fp_verdict).type">
                      指纹 {{ fpVerdictMeta(ch.fp_verdict).text }}
                    </el-tag>
                    <el-tag v-if="ch.lease_id" size="small" type="warning" style="margin-left:4px">
                      随设备租用
                    </el-tag>
                    <el-button size="small" type="danger" plain style="margin-left:8px"
                               @click="openRecover(b, ch)">串口恢复</el-button>
                    <span class="comres-muted" style="font-size:12px;margin-left:8px">
                      {{ ch.baud ? ch.baud + ' baud' : '' }}
                    </span>
                    <div class="wl-com-sub">
                      来源
                      <el-button size="small" link type="primary" @click="gotoPortSource({
                        node_id: ch.source.node_id, node_ip: ch.source.node_ip, slot: ch.source.slot,
                      })">{{ ch.source.display_name }}</el-button>
                      <span style="margin-left:6px">{{ ch.source.node_id }}</span>
                    </div>
                  </div>
                </div>
              </div>
              <div v-else class="bundle-row">
                <span class="bundle-label">串口</span>
                <span class="comres-muted">未绑定（先从资源池分配或等待自动匹配）</span>
              </div>
            </div>
            <el-empty v-if="!bundleRows.length && !bundleLoading" description="暂无设备资源包" />
          </div>
          <p class="comres-muted" style="font-size:12px;margin:8px 0 0">
            一套设备 = ADB 通道 + 其名下的 TCP COM；来源只做溯源，点节点名可跳回节点视图。
            ADB 不通时用同套串口登录排障（串口恢复入口下一步接入）。
          </p>
        </el-card>

        <!-- COMRes 设置：自动批准 / 清理 / 客户端下载 -->
        <div v-else-if="comresTab === 'settings'">
          <el-card shadow="never" class="comres-panel" style="margin-bottom:16px">
            <template #header><span>COMRes 设置</span></template>
            <div class="comres-settings-row">
              <div>
                <div style="font-weight:600;margin-bottom:4px">自动批准</div>
                <div class="comres-muted" style="font-size:12px">关闭后客户端申请进入待审，需人工批准（授权抽屉同步）</div>
              </div>
              <el-switch :model-value="comresAutoApprove" @change="toggleComresAutoApprove" />
            </div>
            <div class="comres-settings-row" style="margin-top:16px;border-top:1px solid #ebeef5;padding-top:16px">
              <div>
                <div style="font-weight:600;margin-bottom:4px">清理无效资源</div>
                <div class="comres-muted" style="font-size:12px">审计重复/失效资源后可执行清理（与资源池视图按钮相同）</div>
              </div>
              <el-button size="small" type="warning" plain :loading="comresPruneBusy"
                         @click="runComresPrune">清理无效资源</el-button>
            </div>
          </el-card>

          <el-card shadow="never" class="comres-panel" style="margin-bottom:16px">
            <template #header>
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
                <span>客户端下载</span>
                <el-button size="small" :loading="packagesLoading" @click="loadPackages">刷新</el-button>
              </div>
            </template>
            <el-alert type="info" :closable="false" show-icon style="margin-bottom:12px"
                      title="下载 standalone（ELSB）安装包；解压后双击 start_gui.pyw 或 start.cmd 启动，配置 TRES 地址与 token 即可自助注册" />
            <el-alert v-if="packagesError" type="error" :closable="false" show-icon
                      :title="'列表：' + packagesError" style="margin-bottom:10px" />

            <div v-if="me.role === 'admin'" class="pkg-upload-box" style="margin-bottom:16px">
              <div style="font-weight:600;margin-bottom:10px">上传安装包（仅管理员）</div>
              <div class="pkg-upload-grid">
                <div>
                  <div class="comres-muted" style="font-size:12px;margin-bottom:4px">文件（zip / exe / msi）</div>
                  <el-upload :auto-upload="false" :show-file-list="true" :limit="1"
                             accept=".zip,.exe,.msi"
                             :on-change="onPackageFileChange"
                             :on-remove="() => { packageForm.file = null }">
                    <el-button size="small">选择文件</el-button>
                  </el-upload>
                </div>
                <div>
                  <div class="comres-muted" style="font-size:12px;margin-bottom:4px">名称</div>
                  <el-input v-model="packageForm.name" size="small" />
                </div>
                <div>
                  <div class="comres-muted" style="font-size:12px;margin-bottom:4px">版本</div>
                  <el-input v-model="packageForm.version" size="small" placeholder="0.40.0" />
                </div>
                <div>
                  <div class="comres-muted" style="font-size:12px;margin-bottom:4px">平台</div>
                  <el-select v-model="packageForm.platform" size="small" style="width:100%">
                    <el-option label="Windows" value="Windows" />
                    <el-option label="Linux" value="Linux" />
                  </el-select>
                </div>
                <div>
                  <div class="comres-muted" style="font-size:12px;margin-bottom:4px">渠道</div>
                  <el-select v-model="packageForm.channel" size="small" style="width:100%">
                    <el-option label="prod" value="prod" />
                    <el-option label="beta" value="beta" />
                  </el-select>
                </div>
                <div>
                  <div class="comres-muted" style="font-size:12px;margin-bottom:4px">说明</div>
                  <el-input v-model="packageForm.notes" size="small" placeholder="可选" />
                </div>
                <div class="pkg-upload-action">
                  <el-button size="small" type="primary" plain :loading="packageUploadBusy" @click="uploadPackage">上传</el-button>
                </div>
              </div>
            </div>

            <el-table :data="packages" border size="small" stripe v-loading="packagesLoading">
              <el-table-column prop="name" label="名称" min-width="100" />
              <el-table-column prop="version" label="版本" width="100" />
              <el-table-column prop="platform" label="平台" width="100" />
              <el-table-column label="渠道" width="90">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.channel === 'prod' ? 'success' : 'warning'">{{ row.channel }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="大小" width="100">
                <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
              </el-table-column>
              <el-table-column label="sha256" min-width="140">
                <template #default="{ row }">
                  <el-tooltip :content="row.sha256" placement="top">
                    <el-button link type="primary" size="small" @click="copySha256(row)">{{ sha12(row.sha256) }}…</el-button>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column prop="uploaded_by" label="上传人" min-width="140" show-overflow-tooltip />
              <el-table-column label="时间" min-width="170">
                <template #default="{ row }">{{ formatLocal(row.uploaded_at) }}</template>
              </el-table-column>
              <el-table-column prop="download_count" label="下载次数" width="90" />
              <el-table-column label="操作" width="160" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" type="primary" plain tag="a"
                             :href="packageDownloadHref(row)">下载</el-button>
                  <el-button v-if="me.role === 'admin'" size="small" type="danger" plain
                             @click="deletePackage(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-if="!packages.length && !packagesLoading && !packagesError"
                      description="还没有安装包，管理员可在上方上传" />
          </el-card>
        </div>

        <el-drawer v-model="fpDrawer" :title="fpDrawerTitle" size="660px" direction="rtl">
          <div v-loading="fpDrawerLoading">
            <template v-if="fpDrawerData && fpDrawerData.fingerprint">
              <div class="fp-summary">
                <el-tag size="small" :type="fpVerdictMeta(fpDrawerData.fp_verdict || fpDrawerData.fingerprint.verdict).type">
                  {{ fpVerdictMeta(fpDrawerData.fp_verdict || fpDrawerData.fingerprint.verdict).text }}
                </el-tag>
                <span class="fp-summary-text">
                  {{ fpDrawerData.fingerprint.schema || 'fp.v1' }}
                  <span v-if="fpDrawerData.fingerprint.pattern_id"> · {{ fpDrawerData.fingerprint.pattern_id }}</span>
                </span>
                <span class="fp-summary-time">{{ fpDrawerData.fingerprint.collected_at || '—' }}</span>
              </div>

              <div class="fp-grid">
                <el-card shadow="never" class="fp-section-card">
                  <template #header><span>标识</span></template>
                  <el-descriptions :column="1" size="small" border class="fp-desc">
                    <el-descriptions-item label="SN">{{ (fpDrawerData.fingerprint.identity && fpDrawerData.fingerprint.identity.sn) || '—' }}</el-descriptions-item>
                    <el-descriptions-item label="主 MAC">{{ (fpDrawerData.fingerprint.identity && fpDrawerData.fingerprint.identity.mac) || '—' }}</el-descriptions-item>
                    <el-descriptions-item label="主 IP">{{ (fpDrawerData.fingerprint.identity && fpDrawerData.fingerprint.identity.ip) || '—' }}</el-descriptions-item>
                  </el-descriptions>
                </el-card>

                <el-card shadow="never" class="fp-section-card">
                  <template #header><span>系统</span></template>
                  <el-descriptions :column="1" size="small" border class="fp-desc">
                    <el-descriptions-item label="OS">{{ (fpDrawerData.fingerprint.system && fpDrawerData.fingerprint.system.os) || '—' }}</el-descriptions-item>
                    <el-descriptions-item label="内核">{{ (fpDrawerData.fingerprint.system && fpDrawerData.fingerprint.system.kernel) || '—' }}</el-descriptions-item>
                    <el-descriptions-item label="主机名">{{ (fpDrawerData.fingerprint.system && fpDrawerData.fingerprint.system.hostname) || '—' }}</el-descriptions-item>
                    <el-descriptions-item label="项目">{{ (fpDrawerData.fingerprint.system && fpDrawerData.fingerprint.system.project) || '—' }}</el-descriptions-item>
                    <el-descriptions-item label="型号">{{ (fpDrawerData.fingerprint.system && fpDrawerData.fingerprint.system.model) || '—' }}</el-descriptions-item>
                  </el-descriptions>
                </el-card>
              </div>

              <el-card shadow="never" class="fp-section-card">
                <template #header><span>网卡</span></template>
                <el-table v-if="fpNetRows(fpDrawerData.fingerprint).length"
                          :data="fpNetRows(fpDrawerData.fingerprint)" size="small" border class="fp-net-table">
                  <el-table-column label="接口" min-width="90">
                    <template #default="{ row }">
                      {{ row.iface || '—' }}
                      <el-tag v-if="row.primary" size="small" type="success" style="margin-left:4px">主</el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="mac" label="MAC" min-width="140" show-overflow-tooltip />
                  <el-table-column prop="ip" label="IP" min-width="120" show-overflow-tooltip />
                  <el-table-column label="up" width="56">
                    <template #default="{ row }">{{ row.up === true ? '是' : (row.up === false ? '否' : '—') }}</template>
                  </el-table-column>
                </el-table>
                <div v-else class="fp-empty-line">未上报网卡信息（指纹判定为 partial 时常见，设备侧可能未回全）</div>
              </el-card>

              <el-card shadow="never" class="fp-section-card">
                <template #header><span>构建</span></template>
                <el-descriptions :column="1" size="small" border class="fp-desc fp-desc-mono">
                  <el-descriptions-item label="build_info"><span class="mono">{{ (fpDrawerData.fingerprint.system && fpDrawerData.fingerprint.system.build_info) || '—' }}</span></el-descriptions-item>
                  <el-descriptions-item label="proc_version"><span class="mono">{{ (fpDrawerData.fingerprint.system && fpDrawerData.fingerprint.system.proc_version) || '—' }}</span></el-descriptions-item>
                  <el-descriptions-item label="uname"><span class="mono">{{ (fpDrawerData.fingerprint.system && fpDrawerData.fingerprint.system.uname) || '—' }}</span></el-descriptions-item>
                </el-descriptions>
              </el-card>

              <el-collapse class="fp-raw-collapse">
                <el-collapse-item title="原始输出（raw + errors）" name="raw">
                  <pre class="fp-raw mono">{{ JSON.stringify({
                    raw: fpDrawerData.fingerprint.raw || null,
                    errors: fpDrawerData.fingerprint.errors || [],
                  }, null, 2) }}</pre>
                </el-collapse-item>
              </el-collapse>
            </template>
            <el-empty v-else description="该端口未上报指纹" />
          </div>
        </el-drawer>

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
                    <template v-if="row.source === 'enroll' || (row.client_id || '').startsWith('enroll:')">
                      · 预置 token
                    </template>
                  </div>
                  <div class="comres-muted" style="font-size:12px;margin-top:2px">
                    首次 {{ formatLocal(row.first_seen_at) }} · 申请 {{ row.request_count }} 次
                  </div>
                </div>
                <el-tag size="small" :type="wlAuthBadge(row.status).type">
                  {{ (row.source === 'enroll' || (row.client_id || '').startsWith('enroll:')) && row.status === 'approved'
                    ? '已批准'
                    : wlAuthBadge(row.status).label }}
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
          </el-tab-pane>

          <el-tab-pane label="PowerRes" name="powerres">
        <!-- PowerRes：电源通道绑定到被测设备 -->
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:12px;flex-wrap:wrap">
          <h2 style="margin:0">PowerRes</h2>
          <div v-if="!prUnsupported" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <el-select
              v-model="prNodeId"
              placeholder="选择节点"
              filterable
              clearable
              style="width:280px"
              @change="onPrNodeChange"
            >
              <el-option
                v-for="n in prNodes"
                :key="n.node_id"
                :label="(n.display_name || n.node_id) + ' (' + n.node_id + ')'"
                :value="n.node_id"
              />
            </el-select>
            <el-button size="small" :loading="prLoading" @click="refreshPowerRes">刷新</el-button>
            <el-button v-if="me.role === 'admin'" size="small" type="primary" plain :disabled="prUnsupported" @click="openPrAdd">手工添加资源</el-button>
            <el-tag size="small" type="info">设备 {{ prStats.devices }}</el-tag>
            <el-tag size="small" type="info">通道 {{ prStats.channels }}</el-tag>
            <el-tag size="small" :type="prStats.bound ? 'success' : 'info'">已绑定 {{ prStats.bound }}</el-tag>
          </div>
        </div>

        <div
          v-if="prUnsupported"
          style="display:flex;align-items:center;justify-content:center;min-height:280px;padding:24px"
        >
          <div style="text-align:center;max-width:420px;color:var(--el-text-color-regular);line-height:1.6">
            {{ PR_UNSUPPORTED_HINT }}
          </div>
        </div>

        <template v-else>
          <el-alert v-if="prError" type="error" :closable="false" show-icon
                    :title="'PowerRes：' + prError" style="margin-bottom:10px" />

          <el-alert
            v-else-if="!prLoading && !(prNodes || []).length"
            type="info"
            :closable="false"
            show-icon
            :title="PR_NO_NODES_HINT"
            style="margin-bottom:10px"
          />

          <el-alert
            v-if="!prUnsupported && me.role !== 'admin'"
            type="info"
            :closable="false"
            show-icon
            title="绑定关系由管理员维护；你可对已绑定到你设备的通道执行电源动作"
            style="margin-bottom:10px"
          />

          <el-card shadow="never" class="comres-panel" v-loading="prLoading">
            <template #header>
              <span>电源资源（/api/powerres/inventory）</span>
            </template>
            <el-empty
              v-if="!prLoading && prNodeId && !(prItems || []).length && !prError"
              description="该节点暂无电源资源，可点「手工添加资源」建档"
            />
            <el-empty
              v-else-if="!prLoading && !prNodeId"
              :description="(prNodes || []).length ? '请选择节点以查看电源通道' : PR_NO_NODES_HINT"
            />
            <el-table
              v-else-if="(prItems || []).length"
              :data="prItems"
              border
              size="small"
              stripe
            >
              <el-table-column prop="power_dev" label="电源设备" min-width="120" show-overflow-tooltip />
              <el-table-column prop="name" label="名称" min-width="100" show-overflow-tooltip>
                <template #default="{ row }">{{ row.name || '—' }}</template>
              </el-table-column>
              <el-table-column prop="com" label="COM" width="90" show-overflow-tooltip>
                <template #default="{ row }">{{ row.com || '—' }}</template>
              </el-table-column>
              <el-table-column label="通道" width="80">
                <template #default="{ row }">CH{{ row.channel }}</template>
              </el-table-column>
              <el-table-column prop="endpoint" label="端点" min-width="160" show-overflow-tooltip>
                <template #default="{ row }">{{ row.endpoint || '—' }}</template>
              </el-table-column>
              <el-table-column label="位置" min-width="120" show-overflow-tooltip>
                <template #default="{ row }">{{ prLocationText(row) }}</template>
              </el-table-column>
              <el-table-column label="来源" min-width="140" show-overflow-tooltip>
                <template #default="{ row }">{{ prSourceText(row) || '—' }}</template>
              </el-table-column>
              <el-table-column prop="state" label="当前状态" width="100">
                <template #default="{ row }">{{ row.state || '—' }}</template>
              </el-table-column>
              <el-table-column label="启用" width="80">
                <template #default="{ row }">
                  <el-tag v-if="prRowBound(row) && !prBindingEnabled(row)" size="small" type="info">停用</el-tag>
                  <el-tag v-else-if="prRowBound(row)" size="small" type="success">启用</el-tag>
                  <span v-else class="comres-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="绑定目标" min-width="160" show-overflow-tooltip>
                <template #default="{ row }">
                  <template v-if="prRowBound(row)">
                    <a
                      class="pr-device-link"
                      href="javascript:;"
                      :title="'设备详情 ' + prBoundDeviceId(row)"
                      @click.prevent="openPrDevicePage(prBoundDeviceId(row))"
                    >{{ prDeviceName(prBoundDeviceId(row)) || prBoundDeviceId(row) }}</a>
                    <span v-if="prBoundSlot(row) != null" class="comres-muted">({{ prBoundSlot(row) }})</span>
                  </template>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" min-width="380" fixed="right">
                <template #default="{ row }">
                  <div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px">
                    <!-- 未绑定 + 非 admin：禁用 -->
                    <el-tooltip
                      v-if="!prRowBound(row) && me.role !== 'admin'"
                      content="请先绑定目标设备"
                      placement="top"
                    >
                      <span style="display:inline-flex;gap:4px;align-items:center">
                        <el-switch size="small" disabled :model-value="prSwitchOn(row)" />
                        <el-button size="small" disabled>重启</el-button>
                      </span>
                    </el-tooltip>
                    <!-- 已绑定且停用：禁用 -->
                    <el-tooltip
                      v-else-if="prRowBound(row) && !prBindingEnabled(row)"
                      content="该通道已停用"
                      placement="top"
                    >
                      <span style="display:inline-flex;gap:4px;align-items:center">
                        <el-switch size="small" disabled :model-value="prSwitchOn(row)" />
                        <el-button size="small" disabled>重启</el-button>
                      </span>
                    </el-tooltip>
                    <!-- 已绑定 + 非 admin 无租约：禁用 -->
                    <el-tooltip
                      v-else-if="prRowBound(row) && prActionLeaseBlocked(row)"
                      content="需持有目标设备租约"
                      placement="top"
                    >
                      <span style="display:inline-flex;gap:4px;align-items:center">
                        <el-switch size="small" disabled :model-value="prSwitchOn(row)" />
                        <el-button size="small" disabled>重启</el-button>
                      </span>
                    </el-tooltip>
                    <!-- admin 未绑定 / admin 已绑启用 / 非 admin 持租：可操作 -->
                    <span
                      v-else
                      style="display:inline-flex;gap:4px;flex-wrap:wrap;align-items:center"
                    >
                      <el-tooltip
                        :disabled="prSwitchKnown(row)"
                        content="状态未知（等待端侧上报/心跳）"
                        placement="top"
                      >
                        <span style="display:inline-flex;align-items:center">
                          <el-switch
                            size="small"
                            :model-value="prSwitchOn(row)"
                            :disabled="!prSwitchKnown(row) || prIsActionBusy(row)"
                            :loading="prIsActionBusy(row)"
                            @change="(v) => onPrPowerSwitch(row, v)"
                          />
                        </span>
                      </el-tooltip>
                      <el-button
                        size="small"
                        :loading="prIsActionBusy(row)"
                        :disabled="prIsActionBusy(row)"
                        @click="doPrPowerCycle(row)"
                      >重启</el-button>
                      <el-input-number
                        size="small"
                        :model-value="prGetHoldMs(row)"
                        :min="500"
                        :max="30000"
                        :step="500"
                        controls-position="right"
                        style="width:110px"
                        :disabled="prIsActionBusy(row) || prHoldFollowsHub(row)"
                        @update:model-value="(v) => prSetHoldMs(row, v)"
                      />
                      <span class="comres-muted" style="font-size:11px">
                        {{ prHoldFollowsHub(row) ? 'ms·跟随端侧' : 'ms' }}
                      </span>
                      <el-button
                        v-if="me.role === 'admin'"
                        size="small"
                        link
                        type="primary"
                        :loading="prIsActionBusy(row)"
                        :disabled="prIsActionBusy(row)"
                        @click="doPrPowerQuery(row)"
                      >查询</el-button>
                      <el-button
                        v-if="me.role === 'admin'"
                        size="small"
                        :disabled="prIsActionBusy(row) || !prBindingEnabled(row)"
                        @click="openPrRebootDlg(row)"
                      >重启测试</el-button>
                      <el-button
                        v-if="prFailPhase(row) === 'power_on'"
                        size="small"
                        type="danger"
                        plain
                        :loading="prIsActionBusy(row)"
                        :disabled="prIsActionBusy(row)"
                        @click="doPrCompensateOn(row)"
                      >补偿开</el-button>
                    </span>
                    <el-button
                      v-if="!prRowBound(row) && me.role === 'admin'"
                      size="small"
                      type="primary"
                      link
                      :disabled="!row.endpoint"
                      @click="openPrBind(row)"
                    >绑定</el-button>
                    <el-button
                      v-else-if="me.role === 'admin'"
                      size="small"
                      type="danger"
                      link
                      :disabled="prIsActionBusy(row)"
                      @click="doPrUnbind(row)"
                    >解绑</el-button>
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </template>

        <el-dialog v-model="prBindDlg.visible" title="绑定电源通道" width="600px" append-to-body>
          <el-form label-width="110px" size="small">
            <el-form-item label="电源设备">
              <span>{{ prBindDlg.power_dev }}</span>
              <span v-if="prBindDlg.name" class="comres-muted" style="margin-left:8px">{{ prBindDlg.name }}</span>
              <span v-if="prBindDlg.driver" class="comres-muted" style="margin-left:8px">{{ prBindDlg.driver }}</span>
            </el-form-item>
            <el-form-item label="通道">
              <span>CH{{ prBindDlg.channel }}</span>
            </el-form-item>
            <el-form-item label="端点">
              <span class="mono">{{ prBindDlg.endpoint || '—' }}</span>
            </el-form-item>
            <el-form-item label="COM">
              <span>{{ prBindDlg.com || '—' }}</span>
            </el-form-item>
            <el-form-item label="位置">
              <span v-if="prBindDlg.location">{{ prBindDlg.location }}</span>
              <span v-else>
                <span class="comres-muted">未登记位置</span>
                <span class="comres-muted" style="font-size:11px;margin-left:6px">可在 deskserialhub 设置里登记</span>
              </span>
            </el-form-item>
            <el-form-item label="筛选">
              <div class="pr-bind-filters">
                <el-select
                  v-model="prBindDlg.modelFilter"
                  size="small"
                  clearable
                  placeholder="全部机型"
                  style="width:160px"
                >
                  <el-option label="全部机型" value="" />
                  <el-option
                    v-for="m in prBindModelOptions"
                    :key="m"
                    :label="m"
                    :value="m"
                  />
                </el-select>
                <el-input
                  v-model="prBindDlg.keyword"
                  size="small"
                  clearable
                  placeholder="ID / 名称 / 位置 / 负责人 / ADB"
                  style="flex:1;min-width:160px"
                />
              </div>
            </el-form-item>
            <el-form-item label="目标设备" required>
              <el-select
                v-model="prBindDlg.device_id"
                filterable
                size="small"
                placeholder="选择被测设备"
                style="width:100%"
                :loading="prBindDlg.bindingsLoading"
              >
                <template #empty>
                  <el-empty description="无匹配设备" :image-size="48" />
                </template>
                <el-option
                  v-for="d in prDeviceOptions"
                  :key="d.device_id"
                  :label="d.label"
                  :value="d.device_id"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="label">
              <el-input v-model="prBindDlg.label" size="small" placeholder="可选；默认 power_dev CHn" clearable />
            </el-form-item>
            <el-form-item label="slot">
              <el-input
                v-model="prBindDlg.slot"
                size="small"
                :placeholder="'留空=自动分配 1..' + PR_SLOT_MAX"
                clearable
              />
            </el-form-item>
          </el-form>
          <div class="comres-muted" style="font-size:12px;margin-top:4px">
            绑定后控制权跟随设备：管理员直控，其他人需持该设备租约
          </div>
          <template #footer>
            <el-button size="small" @click="prBindDlg.visible = false">取消</el-button>
            <el-button size="small" type="primary" :loading="prBindDlg.busy" @click="submitPrBind">绑定</el-button>
          </template>
        </el-dialog>

        <el-dialog v-model="prAddDlg.visible" title="手工添加电源资源" width="520px" append-to-body>
          <el-form label-width="110px">
            <el-form-item label="节点" required>
              <el-select
                v-model="prAddDlg.node_id"
                filterable
                placeholder="选择 wirelog 节点"
                style="width:100%"
              >
                <el-option
                  v-for="n in wlNodes"
                  :key="n.node_id"
                  :label="(n.display_name || n.node_id) + ' (' + n.node_id + ')'"
                  :value="n.node_id"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="设备 id" required>
              <el-input v-model="prAddDlg.power_dev" placeholder="power_dev，如 relay1" clearable />
            </el-form-item>
            <el-form-item label="名称">
              <el-input v-model="prAddDlg.name" placeholder="可选" clearable />
            </el-form-item>
            <el-form-item label="COM">
              <el-input v-model="prAddDlg.com" placeholder="可选，如 COM24" clearable />
            </el-form-item>
            <el-form-item label="通道号" required>
              <el-input v-model="prAddDlg.channel" placeholder="如 1" clearable />
            </el-form-item>
            <el-form-item label="端点" required>
              <el-input v-model="prAddDlg.endpoint" placeholder="tcp://host:port" clearable />
            </el-form-item>
            <el-form-item label="位置">
              <el-input v-model="prAddDlg.location" placeholder="可选，电源设备所在位置（≤64 字）" maxlength="64" clearable />
            </el-form-item>
            <el-form-item label="驱动/型号">
              <el-input v-model="prAddDlg.driver" placeholder="可选，relay 驱动名（≤32 字）" maxlength="32" clearable />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="prAddDlg.visible = false">取消</el-button>
            <el-button type="primary" :loading="prAddDlg.busy" @click="submitPrAdd">登记</el-button>
          </template>
        </el-dialog>

        <el-dialog
          v-model="prRebootDlg.visible"
          :title="'重启测试 · ' + prRebootDlg.power_dev + ' CH' + prRebootDlg.channel"
          width="520px"
          append-to-body
          @close="stopPrRebootPoll"
        >
          <el-form label-width="90px" size="small">
            <el-form-item label="间隔">
              <el-input-number v-model="prRebootDlg.interval_s" :min="1" :max="3600" />
              <span class="comres-muted" style="margin-left:8px">秒</span>
            </el-form-item>
            <el-form-item label="次数">
              <el-input-number v-model="prRebootDlg.repeat" :min="1" :max="1000" />
              <span class="comres-muted" style="margin-left:8px">次</span>
            </el-form-item>
            <el-form-item label="预计耗时">
              <span>{{ prRebootEstimatedText }}</span>
            </el-form-item>
          </el-form>
          <div
            v-if="prRebootDlg.status"
            style="margin:8px 0;padding:8px;background:#f5f7fa;border-radius:4px;font-size:13px"
          >
            <el-tag v-if="prRebootDlg.status.running" size="small" type="warning">运行中</el-tag>
            <el-tag v-else size="small" type="info">{{ prRebootDlg.status.cancelled ? '已停止' : '已结束' }}</el-tag>
            <span style="margin-left:8px">{{ prRebootStatusText }}</span>
          </div>
          <div class="comres-muted" style="font-size:12px">
            一轮 = 断电→保持({{ prRebootDlg.hold_ms }}ms)→上电，然后等待间隔秒。
          </div>
          <template #footer>
            <el-button
              size="small"
              type="primary"
              :loading="prRebootDlg.starting"
              :disabled="prRebootDlg.status && prRebootDlg.status.running"
              @click="startPrRebootTest"
            >开始</el-button>
            <el-button
              size="small"
              :loading="prRebootDlg.stopping"
              :disabled="!(prRebootDlg.status && prRebootDlg.status.running)"
              @click="stopPrRebootTest"
            >停止</el-button>
            <el-button size="small" @click="closePrRebootDlg">关闭</el-button>
          </template>
        </el-dialog>
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
              <el-form-item label="探测后端">
                <el-select v-model="settingsForm.probe_backend" style="width: 160px">
                  <el-option label="real（真实 ADB 探测）" value="real" />
                </el-select>
                <span style="margin-left: 8px; color: #909399">生产只允许 real；mock 仅开发/测试（须 allow_mock_probe）</span>
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
              <h3 style="margin: 0">设备类型元数据（defconfig / SDK / 编译 / 镜像·升级方式 / 芯片·浮点 / 备注）</h3>
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
              <el-table-column label="操作" width="240">
                <template #default="{ row }">
                  <el-button size="small" link type="success" @click="reviewFeedback(row, 'accepted')">采纳</el-button>
                  <el-button size="small" link type="info" @click="reviewFeedback(row, 'deferred')">暂缓</el-button>
                  <el-button size="small" link type="danger" @click="reviewFeedback(row, 'rejected')">驳回</el-button>
                  <el-button size="small" link type="danger" @click="deleteFeedback(row)">删除</el-button>
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
            <el-form-item label="名称">
              <el-input v-model="ptEditRow.display_name" placeholder="展示名（给人看），留空则用 type_code" />
              <div class="form-hint">该名称会作为新建设备的默认名称，建议使用人类可读名称，不要与 type_code 相同。</div>
            </el-form-item>
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
            <el-form-item label="镜像路径">
              <el-input v-model="ptEditRow.image_path" :placeholder="ptEditRow.flash_method === 'image' ? '编译机上的镜像 script.ini 路径或 qmimages 目录' : 'out/qm10xd_linux/qmimages/'" />
              <div v-if="ptEditRow.flash_method === 'image'" class="form-hint">镜像升级：填写编译机上的 script.ini 路径或 qmimages 目录。</div>
            </el-form-item>
            <el-form-item label="升级方式">
              <el-select v-model="ptEditRow.flash_method" style="width: 100%">
                <el-option label="DB 升级（默认）" value="db" />
                <el-option label="镜像升级" value="image" />
                <el-option label="不支持远程升级" value="unsupported" />
              </el-select>
              <div class="form-hint">决定刷机入口默认走 DB / 镜像，或直接拒绝远程升级。</div>
            </el-form-item>
            <el-form-item v-if="ptEditRow.flash_method === 'db'" label="DB 路径" class="pt-full">
              <div style="display:flex; gap:8px; width:100%">
                <el-input v-model="ptEditRow.db_project_name" placeholder="工程名 / 相对路径 / http(s) URL / 局域网地址如 \\host\path" style="flex:1" />
                <el-button :loading="ptEditRow.db_probing" @click="probeDbFeatures(ptEditRow)">探测特征</el-button>
              </div>
              <div v-if="ptEditRow.db_features && ptEditRow.db_features.length" style="margin-top:8px">
                <div class="form-hint" style="margin-bottom:4px">候选特征（AND 过滤版本目录名）：</div>
                <el-checkbox-group v-model="ptEditRow.feature_filter">
                  <el-checkbox v-for="f in ptEditRow.db_features" :key="f.name" :label="f.name">
                    {{ f.name }}（{{ f.count }} 个目录）
                  </el-checkbox>
                </el-checkbox-group>
              </div>
            </el-form-item>
            <el-alert v-if="ptEditRow.flash_method === 'unsupported'" type="warning" :closable="false" show-icon style="margin-bottom:10px"
              title="该机型已标记为不支持远程升级，刷机入口将返回 FLASH_UNSUPPORTED" />
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
      <el-dialog v-model="deviceAddVisible" title="添加设备" width="920px" :close-on-click-modal="false">
        <el-steps :active="deviceAddStep - 1" align-center finish-status="success" style="margin-bottom: 16px">
          <el-step title="探测设备" />
          <el-step title="确认信息" />
        </el-steps>

        <template v-if="deviceAddStep === 1">
          <el-radio-group v-model="deviceAddMode" style="margin-bottom: 14px"
                          @change="onDeviceAddModeChange">
            <el-radio-button value="ip">IP 直连</el-radio-button>
            <el-radio-button value="discover">设备发现</el-radio-button>
            <el-radio-button value="serial">串口（UART）</el-radio-button>
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

          <div v-else-if="deviceAddMode === 'discover'">
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

          <div v-else>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
              <span class="form-hint" style="margin:0">L3 = 节点在线、串口实测可达且已取到 MAC/SN/IP；L2/L0 请先点「探测」，达到 L3 才允许添加。</span>
              <el-button size="small" :loading="serialAddLoading" @click="loadSerialAddResources">刷新</el-button>
            </div>
            <el-table :data="serialAddResources" border size="small" max-height="320"
                      v-loading="serialAddLoading" style="width:100%"
                      :row-style="{ cursor: 'pointer' }" @row-click="onSerialRowClick">
              <el-table-column label="日志机/路" min-width="150">
                <template #default="{ row }">
                  {{ row.node_name || row.node_id }} · 路{{ row.slot }}
                  <el-tag v-if="row.node_status !== 'online'" size="small" type="info">{{ row.node_status }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="TCP COM / 接入" min-width="190">
                <template #default="{ row }">
                  <div>{{ row.com_name || row.tty_name || ('COM' + row.slot) }}</div>
                  <div class="wl-endpoint">{{ row.endpoint || '—' }}</div>
                </template>
              </el-table-column>
              <el-table-column label="指纹身份" min-width="220">
                <template #default="{ row }">
                  <div>MAC {{ serialIdentity(row).mac || '—' }}</div>
                  <div>SN {{ serialIdentity(row).sn || '—' }}</div>
                  <div>IP {{ serialIdentity(row).ip || '—' }} · {{ serialIdentity(row).model || '未识别' }}</div>
                </template>
              </el-table-column>
              <el-table-column label="等级" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="serialLevel(row) === 'L3' ? 'success' : (serialLevel(row) === 'L2' ? 'warning' : 'info')">
                    {{ serialLevel(row) === 'L2' ? 'L2·无指纹' : serialLevel(row) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="150">
                <template #default="{ row }">
                  <el-button size="small" link type="primary"
                             :loading="serialProbingKey === (row.node_id + ':' + row.slot)"
                             :disabled="!!serialProbingKey"
                             @click.stop="probeSerialResource(row)">探测</el-button>
                  <el-button size="small" link type="primary"
                             :disabled="!serialQualified(row)"
                             @click.stop="pickSerialResource(row)">选择</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-if="!serialAddResources.length && !serialAddLoading"
                      description="没有未绑定的串口资源（先在 COMRes 里确认节点在线、端口已纳管）" />
          </div>
        </template>

        <template v-else>
          <el-descriptions v-if="deviceAddProbe" :column="2" border size="small" style="margin-bottom: 12px">
            <el-descriptions-item label="健康等级">{{ deviceAddProbe.probe?.level }}</el-descriptions-item>
            <el-descriptions-item label="MAC">{{ deviceAddProbe.probe?.mac || '-' }}</el-descriptions-item>
            <el-descriptions-item label="SN">{{ deviceAddProbe.probe?.sn || '-' }}</el-descriptions-item>
            <el-descriptions-item label="探测机型">{{ deviceAddProbe.probe?.model || '-' }}</el-descriptions-item>
            <el-descriptions-item v-if="deviceAddProbe.serial" label="串口">
              {{ deviceAddProbe.probe.node }} · 路{{ deviceAddProbe.probe.slot }}
              · {{ deviceAddProbe.probe.com || '—' }}
              <el-tag size="small" :type="deviceAddProbe.probe.reachable ? 'success' : 'info'" style="margin-left:6px">
                {{ deviceAddProbe.probe.reachable ? '可达' : '不可达' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item v-if="deviceAddProbe.serial" label="接入地址">
              <span class="wl-endpoint">{{ deviceAddProbe.probe.endpoint || '—' }}</span>
            </el-descriptions-item>
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
            <el-form-item label="名称（可选）">
              <el-input v-model="deviceAddConfirm.display_name" maxlength="128" placeholder="留空自动使用设备类型名称" />
              <div class="form-hint">
                {{ deviceAddDefaultName ? ('留空时默认名称：' + deviceAddDefaultName) : '留空且所选机型无可读名称时，将使用 机型-位置 作为默认名。' }}
              </div>
            </el-form-item>
          </el-form>
        </template>

        <template #footer>
          <el-button @click="deviceAddVisible = false">取消</el-button>
          <el-button v-if="deviceAddStep === 2" @click="deviceAddStep = 1">上一步</el-button>
          <el-button v-if="deviceAddStep === 1 && deviceAddMode === 'ip'" type="primary" :loading="deviceAddProbing" @click="submitDeviceProbe">探测</el-button>
          <el-button v-else-if="deviceAddStep === 2 && deviceAddMode === 'serial'" type="primary" :loading="deviceAddSaving" @click="submitSerialDevice">从串口创建</el-button>
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
            <el-form-item label="IP">
              <el-input v-model="deviceEditRow.ip" />
              <div class="form-hint">串口接入的设备可在这里后补 ADB IP：ADB 通了优先走 ADB，不通仍按串口判活。</div>
            </el-form-item>
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
                 width="1040px">
        <el-table :data="wlBindDlg.rows" border size="small" v-loading="wlBindDlg.loading" max-height="440">
          <el-table-column label="日志机/路" min-width="160">
            <template #default="{ row }">
              <el-button link type="primary" @click="jumpToWlNode(row.node_id)">
                {{ row.node_display || row.node_id }} · 路{{ row.slot }}
              </el-button>
            </template>
          </el-table-column>
          <el-table-column label="TCP COM（浏览器入口）" min-width="240">
            <template #default="{ row }">
              <a class="wl-com-link" @click="openWlShell(row)">
                {{ row.com_name || row.tty || ('TCP COM ' + row.slot) }}<span class="wl-com-open">↗</span>
              </a>
              <div class="wl-com-sub">
                <span class="wl-endpoint">{{ row.endpoint || '—' }}</span>
                <el-button v-if="row.endpoint" link type="primary" size="small"
                           style="margin-left:4px" @click="copyPortEndpoint(row)">复制</el-button>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="tty" label="tty" width="110" show-overflow-tooltip />
          <el-table-column label="串口就绪" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.shell_reachable ? 'success' : 'info'">
                {{ row.shell_reachable ? '可达' : '不可达' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="shell_mac" label="实测 MAC" width="150" />
          <el-table-column prop="shell_ip" label="实测 IP" width="130" />
        </el-table>
        <el-empty v-if="!wlBindDlg.rows.length && !wlBindDlg.loading" description="该设备当前未绑定任何 WireLog 串口" />
        <template #footer><el-button @click="wlBindDlg.visible = false">关闭</el-button></template>
      </el-dialog>
    <el-dialog v-model="ptCreateDialog" :title="ptCreateTitle" width="860px" :close-on-click-modal="false">
          <el-alert v-if="ptCreateRow.copy_from" type="info" :closable="false" show-icon style="margin-bottom:10px"
            :title="'已从模板 ' + ptCreateRow.copy_from + ' 完整复制配置（工程命令 / 镜像路径 / 芯片浮点 / 编译服务器 / 固件策略），只需修改有差异的项'" />
          <el-form ref="ptCreateForm" :model="ptCreateRow" :rules="ptCreateRules" label-width="96px" class="pt-grid">
            <el-form-item label="type_code" prop="type_code"><el-input v-model="ptCreateRow.type_code" placeholder="如 moto_ld" /></el-form-item>
            <el-form-item label="名称">
              <el-input v-model="ptCreateRow.display_name" placeholder="展示名，留空则用 type_code" />
              <div class="form-hint">该名称会作为新建设备的默认名称，建议使用人类可读名称，不要与 type_code 相同。</div>
            </el-form-item>
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
            <el-form-item label="镜像路径" prop="image_path">
              <el-input v-model="ptCreateRow.image_path" :placeholder="ptCreateRow.flash_method === 'image' ? '编译机上的镜像 script.ini 路径或 qmimages 目录' : 'out/qm10xd_linux/qmimages/'" />
              <div v-if="ptCreateRow.flash_method === 'image'" class="form-hint">镜像升级：填写编译机上的 script.ini 路径或 qmimages 目录。</div>
            </el-form-item>
            <el-form-item label="升级方式">
              <el-select v-model="ptCreateRow.flash_method" style="width: 100%">
                <el-option label="DB 升级（默认）" value="db" />
                <el-option label="镜像升级" value="image" />
                <el-option label="不支持远程升级" value="unsupported" />
              </el-select>
              <div class="form-hint">决定刷机入口默认走 DB / 镜像，或直接拒绝远程升级。</div>
            </el-form-item>
            <el-form-item v-if="ptCreateRow.flash_method === 'db'" label="DB 路径" class="pt-full">
              <div style="display:flex; gap:8px; width:100%">
                <el-input v-model="ptCreateRow.db_project_name" placeholder="工程名 / 相对路径 / http(s) URL / 局域网地址如 \\host\path" style="flex:1" />
                <el-button :loading="ptCreateRow.db_probing" @click="probeDbFeatures(ptCreateRow)">探测特征</el-button>
              </div>
              <div v-if="ptCreateRow.db_features && ptCreateRow.db_features.length" style="margin-top:8px">
                <div class="form-hint" style="margin-bottom:4px">候选特征（AND 过滤版本目录名）：</div>
                <el-checkbox-group v-model="ptCreateRow.feature_filter">
                  <el-checkbox v-for="f in ptCreateRow.db_features" :key="f.name" :label="f.name">
                    {{ f.name }}（{{ f.count }} 个目录）
                  </el-checkbox>
                </el-checkbox-group>
              </div>
            </el-form-item>
            <el-alert v-if="ptCreateRow.flash_method === 'unsupported'" type="warning" :closable="false" show-icon style="margin-bottom:10px"
              title="该机型已标记为不支持远程升级，刷机入口将返回 FLASH_UNSUPPORTED" />
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
.wl-config-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 16px;
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
}
.wl-config-title { font-size: 14px; font-weight: 600; white-space: nowrap; }
.wl-config-title em { font-style: normal; font-weight: 400; font-size: 12px; color: var(--el-text-color-secondary); }
.wl-config-form .el-form-item { margin: 0 10px 0 0; }
.wl-config-form .el-form-item__label { padding-right: 6px; font-size: 12px; }
/* 串口资源表：TCP COM 为主键 */
.wl-com-link {
  font-size: 15px;
  font-weight: 600;
  color: var(--el-color-primary);
  cursor: pointer;
}
.wl-com-link:hover { text-decoration: underline; }
.wl-com-open { font-size: 12px; margin-left: 4px; font-weight: 400; }
.wl-com-sub { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.5; margin-top: 2px; }
.wl-endpoint { font-family: Consolas, Monaco, "Courier New", monospace; font-size: 12px; }
.wl-source-link { padding: 0 !important; font-size: 13px; font-weight: 600; }
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
/* 统计卡：≥1400 七列一排 / ≥1100 四列 / ≥800 三列 / <800 两列；末行居中 */
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
  flex: 0 0 calc((100% - var(--ov-gap)) / 2);
  max-width: calc((100% - var(--ov-gap)) / 2);
  min-width: 0;
  box-sizing: border-box;
}
@media (min-width: 800px) {
  .device-stats-grid > .ov-card {
    flex-basis: calc((100% - 2 * var(--ov-gap)) / 3);
    max-width: calc((100% - 2 * var(--ov-gap)) / 3);
  }
}
@media (min-width: 1100px) {
  .device-stats-grid > .ov-card {
    flex-basis: calc((100% - 3 * var(--ov-gap)) / 4);
    max-width: calc((100% - 3 * var(--ov-gap)) / 4);
  }
}
@media (min-width: 1400px) {
  .device-stats-grid > .ov-card {
    flex: 1 1 0;
    flex-basis: 0;
    max-width: none;
    min-width: 150px;
  }
}
.ov-card {
  --el-card-bg-color: #fbfcfe;
  --el-card-border-color: #e5e8ef;
  cursor: pointer;
  transition: box-shadow .2s, border-color .2s, background-color .2s, transform .15s;
  border-radius: 12px;
  background: #fbfcfe;
  border: 1px solid #e5e8ef;
  color: #111827;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 1px 3px rgba(16, 24, 40, .06);
  height: 100%;
  min-height: 96px;
}
.ov-card :deep(.el-card__body) {
  padding: 12px 14px; background: transparent; height: 100%; box-sizing: border-box;
  display: flex; flex-direction: column; justify-content: flex-start;
}
.ov-card:hover {
  border-color: #bfdbfe;
  box-shadow: 0 2px 8px rgba(37, 99, 235, .10);
  transform: translateY(-1px);
}
.ov-card.is-active {
  border-color: #2563eb;
  background: #eff6ff;
  --el-card-bg-color: #eff6ff;
  box-shadow: 0 0 0 1px #2563eb inset, 0 2px 8px rgba(37, 99, 235, .12);
}
.ov-label { color: #6b7280; font-size: 12px; line-height: 1.3; }
.ov-num {
  font-size: 28px; line-height: 1.15; margin-top: 6px; font-weight: 700; color: #111827;
  font-variant-numeric: tabular-nums; letter-spacing: -0.02em;
}
.ov-sub { margin-top: auto; padding-top: 4px; font-size: 11px; color: #9ca3af; line-height: 1.3; min-height: 1.3em; }
.ov-sub-spacer { visibility: hidden; }
.ov-total { color: #111827; }
.ov-online { color: #16a34a; }
.ov-offline { color: #dc2626; }
.ov-pending { color: #d97706; }
.ov-allocated { color: #2563eb; }
.ov-idle { color: #16a34a; }
.ov-comres { color: #111827; }
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
/* COMRes 子页签：与统计卡同风格（浅色、12px 圆角） */
.comres-subtabs {
  display: flex; flex-wrap: wrap; gap: 10px; margin: 4px 0 14px;
}
.comres-subtab {
  flex: 0 0 auto; min-width: 96px; padding: 10px 16px;
  border: 1px solid #e5e8ef; border-radius: 12px;
  background: #fbfcfe; color: #111827; font-size: 13px; font-weight: 600;
  cursor: pointer; line-height: 1.3;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04), 0 1px 3px rgba(16, 24, 40, .06);
  transition: box-shadow .2s, border-color .2s, background-color .2s, transform .15s;
}
.comres-subtab:hover {
  border-color: #bfdbfe; box-shadow: 0 2px 8px rgba(37, 99, 235, .10); transform: translateY(-1px);
}
.comres-subtab.is-active {
  border-color: #2563eb; background: #eff6ff;
  box-shadow: 0 0 0 1px #2563eb inset, 0 2px 8px rgba(37, 99, 235, .12);
}
.comres-panel {
  --el-card-bg-color: #fbfcfe;
  --el-card-border-color: #e5e8ef;
  border-radius: 12px;
  background: #fbfcfe;
  border: 1px solid #e5e8ef;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
}
.comres-settings-row {
  display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
}
.pkg-upload-box {
  border: 1px solid #ebeef5; border-radius: 12px; padding: 12px 14px; background: #fff;
}
.pkg-upload-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  align-items: end;
}
.pkg-upload-action { display: flex; align-items: end; }
@media (max-width: 899px) {
  .pkg-upload-grid { grid-template-columns: 1fr; }
}
/* COMRes：无串口资源 / 失效资源灰显 */
.comres-muted { color: #c0c4cc; }
.pr-device-link {
  color: var(--el-color-primary);
  text-decoration: none;
  cursor: pointer;
}
.pr-device-link:hover { text-decoration: underline; }
/* PT3b 设备独立页 */
.dev-link {
  color: inherit;
  text-decoration: none;
  cursor: pointer;
}
.dev-link:hover {
  color: var(--el-color-primary);
  text-decoration: underline;
}
.dev-page { max-width: 1400px; }
.dev-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.dev-head-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.dev-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.dev-title { margin: 0; font-size: 18px; }
.dev-owner { color: #909399; font-size: 13px; }
.dev-section { margin-bottom: 16px; }
.dev-section .el-card__header { padding: 10px 14px; font-weight: 600; }
.dev-section .el-card__body { padding: 12px 14px; }
.dev-power-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; flex-wrap: wrap;
}
.dev-power-node {
  display: flex; align-items: center; gap: 6px; font-weight: 400;
}
.dev-power-bound { margin-bottom: 8px; }
.dev-power-cand { margin-top: 4px; border: none; }
.dev-power-cand .el-collapse-item__header { font-size: 13px; font-weight: 600; height: 36px; line-height: 36px; }
.dev-power-cand .el-collapse-item__wrap { border-bottom: none; }
.dev-power-cand .el-collapse-item__content { padding-bottom: 4px; }
.kv-mono {
  font-family: Consolas, Monaco, "Courier New", monospace;
  font-size: 12px;
  word-break: break-all;
}
.cap-kind-block { margin-bottom: 12px; }
.cap-kind-title {
  font-size: 13px; font-weight: 600; color: #606266;
  margin: 4px 0 8px;
}
.cap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
.cap-row { align-items: stretch; }
.cap-col {
  display: flex;
  margin-bottom: 12px;
}
.cap-card {
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 8px;
  padding: 10px 12px;
  background: #fff;
}
.cap-card--stretch {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
}
.cap-card--dim { opacity: 0.65; }
.cap-card-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px; margin-bottom: 8px;
}
.cap-card-tags { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.cap-card-name { font-weight: 600; font-size: 13px; }
.cap-card-row {
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; margin-bottom: 4px;
}
.cap-ep {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 160px;
}
.cap-card-actions {
  display: flex; flex-wrap: wrap; align-items: center; gap: 4px;
  margin-top: auto;
  padding-top: 8px;
}
.cap-detail-collapse {
  margin-top: 6px;
  border: none;
}
.cap-detail-collapse .el-collapse-item__header {
  height: 28px; line-height: 28px; font-size: 12px; font-weight: 500; border: none;
}
.cap-detail-collapse .el-collapse-item__wrap { border: none; }
.cap-detail-collapse .el-collapse-item__content { padding-bottom: 2px; }
.dev-power-config { border: none; }
.dev-power-config .el-collapse-item__header {
  font-size: 13px; font-weight: 600; height: 36px; line-height: 36px;
}
.dev-power-config .el-collapse-item__wrap { border-bottom: none; }
.adb-debug { margin-top: 8px; }
.adb-quick {
  display: flex; align-items: center; flex-wrap: wrap; gap: 2px 6px;
  margin-bottom: 6px;
}
.adb-err { color: #f56c6c; font-size: 12px; margin: 6px 0 0; }
.adb-out {
  margin: 8px 0 0;
  max-height: 220px;
  overflow: auto;
  padding: 8px;
  background: #f5f7fa;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 4px;
  font-family: Consolas, Monaco, "Courier New", monospace;
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-all;
}
.pr-bind-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
  align-items: center;
}
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
/* t981：设备指纹抽屉（紧凑卡片） */
.fp-summary {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 12px; margin-bottom: 10px;
  background: #f7f9fc; border: 1px solid #e5e8ef; border-radius: 10px;
}
.fp-summary-text { font-size: 12px; color: #6b7280; }
.fp-summary-time { margin-left: auto; font-size: 12px; color: #9aa1ac; white-space: nowrap; }
.fp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.fp-section-card {
  --el-card-bg-color: #fbfcfe;
  --el-card-border-color: #e5e8ef;
  margin-bottom: 10px;
  border-radius: 10px;
  background: #fbfcfe;
  border: 1px solid #e5e8ef;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
}
.fp-section-card .el-card__header {
  padding: 7px 12px; font-size: 13px; font-weight: 600;
  border-bottom: 1px solid #eef1f6;
}
.fp-section-card .el-card__body { padding: 8px 12px; }
.fp-desc .el-descriptions__label {
  width: 76px; white-space: nowrap; color: #6b7280;
  padding: 6px 8px !important; font-weight: 400;
}
.fp-desc .el-descriptions__content {
  padding: 6px 8px !important; font-size: 12px; word-break: break-all;
}
.fp-desc-mono .el-descriptions__label { width: 104px; }
.fp-net-table .el-table__cell { padding: 5px 0; }
.fp-empty-line { font-size: 12px; color: #9aa1ac; padding: 2px 2px 4px; }
.fp-raw-collapse .el-collapse-item__header { font-size: 12px; height: 36px; line-height: 36px; }
.fp-raw {
  margin: 0; padding: 10px 12px; max-height: 280px; overflow: auto;
  background: #f5f7fa; border: 1px solid #e4e7ed; border-radius: 8px;
  white-space: pre-wrap; word-break: break-all; font-size: 12px;
}
@media (max-width: 720px) {
  .fp-grid { grid-template-columns: 1fr; gap: 0; }
}

/* v0.42 设备资源包：一套设备一张卡 */
.bundle-card {
  border: 1px solid #e4e7ed; border-radius: 8px;
  padding: 10px 12px; margin-bottom: 10px; background: #fff;
}
.bundle-head {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding-bottom: 6px; border-bottom: 1px dashed #ebeef5; margin-bottom: 6px;
}
.bundle-row {
  display: flex; align-items: center; gap: 8px; padding: 3px 0;
}
.bundle-label {
  width: 42px; flex: 0 0 42px; color: #909399; font-size: 12px;
}
.bundle-serial { padding: 2px 0; }
.recover-out {
  margin: 0; padding: 10px 12px; max-height: 320px; overflow: auto;
  background: #0f1115; color: #d7dbe7; border-radius: 8px;
  white-space: pre-wrap; word-break: break-all; font-size: 12px;
}.sched-dev { line-height: 1.4; }
.sched-dev-name { color: #303133; font-weight: 600; }
.sched-dev-id { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
</style>
