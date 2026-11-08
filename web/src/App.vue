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

const newProduct = ref({ type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1 })

const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
async function api(path, opts) {
  const r = await fetch(API_BASE + path, opts)
  if (!r.ok) {
    let detail = String(r.status)
    try { const j = await r.json(); if (j.detail) detail = j.detail } catch (e) { /* ignore */ }
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
  else if (key === 'settings') loadSettings()
}

function filterAudit(action) {
  auditAction.value = action || ''
  loadAudit()
}

function offerCountdown(row) {
  if (row.state !== 'OFFERED' || row.offer_remaining_seconds == null) return '-'
  const s = row.offer_remaining_seconds
  return s > 0 ? `${s} 秒后过期` : '已过期'
}

async function revokeCredential(id) {
  await api('/api/credentials/' + id + '/revoke', { method: 'POST' })
  ElMessage.success('已吊销')
  loadCredentials()
}
async function renewCredential(id) {
  await api('/api/credentials/renew', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ credential_id: id, days: 90 }),
  })
  ElMessage.success('已续期 90 天')
  loadCredentials()
}
async function addProductType() {
  await api('/api/product-types', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(newProduct.value),
  })
  ElMessage.success('已新增机型')
  newProduct.value = { type_code: '', display_name: '', has_screen: false, has_led: false, serial_count: 1 }
  loadSettings()
}
async function disableProductType(code) {
  await api('/api/product-types/' + encodeURIComponent(code) + '/disable', { method: 'POST' })
  ElMessage.success('已停用 ' + code)
  loadSettings()
}

const statusTag = {
  available: 'success', occupied: 'warning', pending: 'info',
  pending_removal: 'warning', retired: 'info', offline: 'danger', maintenance: 'danger',
}

onMounted(loadDashboard)
</script>

<template>
  <el-container style="height: 100vh">
    <el-aside width="200px" style="border-right: 1px solid #eee">
      <div style="padding: 16px; font-weight: 700">Quamm RES</div>
      <el-menu :default-active="active" @select="switchPage">
        <el-menu-item v-for="p in pages" :key="p.key" :index="p.key">{{ p.label }}</el-menu-item>
      </el-menu>
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
          <el-col :span="4"><el-card><div>活跃租约</div><div style="font-size: 28px">{{ dashboard.active_leases }}</div></el-card></el-col>
          <el-col :span="4"><el-card><div>排队中</div><div style="font-size: 28px">{{ dashboard.queued }}</div></el-card></el-col>
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
            <template #default="{ row }">
              <el-tag :type="statusTag[row.status] || 'info'">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="adb_address" label="ADB 地址" width="160" />
        </el-table>
      </div>

      <!-- 占用 -->
      <div v-else-if="active === 'leases'">
        <h2>占用</h2>
        <el-table :data="leases" border>
          <el-table-column prop="lease_id" label="租约" width="140" />
          <el-table-column prop="display_name" label="设备" />
          <el-table-column prop="email" label="占用人" width="180" />
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
          <el-table-column label="等待者">
            <template #default="{ row }">{{ row.waiters.length }}</template>
          </el-table-column>
        </el-table>
        <h3>按设备</h3>
        <el-table :data="queueData ? queueData.by_device : []" border>
          <el-table-column prop="target_code" label="设备" />
          <el-table-column prop="queued" label="排队数" />
          <el-table-column prop="offered" label="已发 offer" />
          <el-table-column label="等待者">
            <template #default="{ row }">{{ row.waiters.length }}</template>
          </el-table-column>
        </el-table>
        <h3>全部条目</h3>
        <el-table :data="queueData ? queueData.entries : []" border>
          <el-table-column prop="entry_id" label="ticket" width="140" />
          <el-table-column prop="target_code" label="目标" width="150" />
          <el-table-column prop="target_type" label="类型" width="90" />
          <el-table-column prop="email" label="申请者" width="180" />
          <el-table-column prop="priority" label="priority" width="90" />
          <el-table-column prop="position" label="队位" width="70" />
          <el-table-column prop="state" label="状态" width="110" />
          <el-table-column label="offer 倒计时" width="130">
            <template #default="{ row }">{{ offerCountdown(row) }}</template>
          </el-table-column>
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

      <!-- 凭据 -->
      <div v-else-if="active === 'credentials'">
        <h2>凭据</h2>
        <el-table :data="credentials" border>
          <el-table-column prop="email" label="邮箱" width="200" />
          <el-table-column prop="token_prefix" label="token 尾号" width="160" />
          <el-table-column prop="expires_at" label="有效期" width="200" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="row.revoked ? 'danger' : 'success'">{{ row.revoked ? '已吊销' : '有效' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="active_leases" label="当前占用" width="100" />
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
          <el-table-column label="详情">
            <template #default="{ row }">{{ row.detail ? JSON.stringify(row.detail) : '-' }}</template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 设置 -->
      <div v-else-if="active === 'settings'">
        <h2>设置</h2>
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
          <el-descriptions-item label="allow_apply_when_suspect">
            <el-switch :model-value="settings.allow_apply_when_suspect" disabled />
          </el-descriptions-item>
        </el-descriptions>

        <h3>机型字典</h3>
        <el-table :data="settings ? settings.product_types : []" border>
          <el-table-column prop="type_code" label="type_code" width="150" />
          <el-table-column prop="display_name" label="名称" />
          <el-table-column prop="has_screen" label="有屏" width="70" />
          <el-table-column prop="has_led" label="有灯" width="70" />
          <el-table-column prop="serial_count" label="串口数" width="90" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-button size="small" type="danger" :disabled="!row.enabled" @click="disableProductType(row.type_code)">停用</el-button>
            </template>
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
      </div>
    </el-main>
  </el-container>
</template>
