<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'

const pages = [
  { key: 'dashboard', label: '看板' },
  { key: 'devices', label: '设备' },
  { key: 'leases', label: '占用' },
  { key: 'queue', label: '排队' },
  { key: 'anomaly', label: '异常' },
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

async function api(path, opts) {
  const r = await fetch(path, opts)
  if (!r.ok) throw new Error(path + ' -> ' + r.status)
  return r.json()
}

async function loadDashboard() {
  dashboard.value = await api('/api/dashboard')
}
async function loadDevices() {
  devices.value = await api('/api/devices')
}
async function loadLeases() {
  leases.value = await api('/api/leases')
}
async function loadCredentials() {
  credentials.value = await api('/api/credentials')
}
async function loadNotices() {
  notices.value = await api('/api/notices')
}
async function loadPatrol() {
  patrolRuns.value = await api('/api/patrol/runs')
}

function switchPage(key) {
  active.value = key
  if (key === 'dashboard') loadDashboard()
  else if (key === 'devices') loadDevices()
  else if (key === 'leases') loadLeases()
  else if (key === 'credentials') loadCredentials()
  else if (key === 'anomaly') { loadNotices(); loadPatrol() }
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
          <el-col :span="4"><el-card><div>可用</div><div style="font-size: 28px; color: #67c23a">{{ dashboard.by_status.available || 0 }}</div></el-card></el-col>
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

      <!-- 排队（占位） -->
      <div v-else-if="active === 'queue'">
        <h2>排队</h2>
        <el-empty description="排队页占位（二期实现）" />
      </div>

      <!-- 异常 -->
      <div v-else-if="active === 'anomaly'">
        <h2>异常 / 待修复</h2>
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

      <!-- 审计 / 设置（占位） -->
      <div v-else-if="active === 'audit'"><h2>审计</h2><el-empty description="审计页占位（二期实现）" /></div>
      <div v-else-if="active === 'settings'"><h2>设置</h2><el-empty description="设置页占位（二期实现）" /></div>
    </el-main>
  </el-container>
</template>
