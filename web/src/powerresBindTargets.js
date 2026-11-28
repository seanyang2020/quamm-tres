/**
 * PowerRes 绑定对话框：目标设备过滤/排序（纯前端，无 IO）。
 * 槽位上限与后端 powerres.SLOT_MAX 对齐。
 */

export const SLOT_MAX = 16

const EXCLUDED_STATUS = new Set(['retired', 'disabled'])

/**
 * 是否可出现在绑定下拉（排除 retired / 已停用）。
 * @param {object|null|undefined} device
 */
export function isBindableDeviceListed(device) {
  if (!device || !device.device_id) return false
  const st = String(device.status || '').trim().toLowerCase()
  if (EXCLUDED_STATUS.has(st)) return false
  return true
}

/**
 * 从 devices 收集出现过的机型（去重、排序）；空 model_class 不计。
 * @param {Array<object>} devices
 * @returns {string[]}
 */
export function collectModelClasses(devices) {
  const set = new Set()
  for (const d of devices || []) {
    if (!isBindableDeviceListed(d)) continue
    const mc = d.model_class != null ? String(d.model_class).trim() : ''
    if (mc) set.add(mc)
  }
  return Array.from(set).sort((a, b) => a.localeCompare(b, 'zh'))
}

/**
 * 按 device_id 聚合电源绑定数。
 * @param {Array<{device_id?: string}>} bindings
 * @returns {Record<string, number>}
 */
export function aggregatePowerBoundCounts(bindings) {
  const out = Object.create(null)
  for (const b of bindings || []) {
    if (!b || !b.device_id) continue
    const id = String(b.device_id)
    out[id] = (out[id] || 0) + 1
  }
  return out
}

function _kwHaystack(device) {
  const parts = [
    device.device_id,
    device.display_name,
    device.position_code,
    device.owner,
    device.adb_address,
  ]
  return parts.map((x) => (x == null ? '' : String(x))).join('\n').toLowerCase()
}

/**
 * 关键字匹配：device_id / display_name / position_code / owner / adb_address（不区分大小写 includes）。
 * @param {object} device
 * @param {string} keyword
 */
export function deviceMatchesKeyword(device, keyword) {
  const kw = (keyword == null ? '' : String(keyword)).trim().toLowerCase()
  if (!kw) return true
  return _kwHaystack(device).includes(kw)
}

function _slotUsedOf(device, slotUsed) {
  if (slotUsed && typeof slotUsed === 'object') {
    const id = device && device.device_id
    if (id != null && Object.prototype.hasOwnProperty.call(slotUsed, id)) {
      const n = Number(slotUsed[id])
      return Number.isFinite(n) && n >= 0 ? n : 0
    }
  }
  const n = Number(device && device.serial_ports)
  return Number.isFinite(n) && n >= 0 ? n : 0
}

function _powerBoundOf(device, powerBound) {
  if (!powerBound || typeof powerBound !== 'object') return 0
  const id = device && device.device_id
  if (id == null) return 0
  const n = Number(powerBound[id])
  return Number.isFinite(n) && n >= 0 ? n : 0
}

/**
 * 过滤 + 排序绑定目标。
 *
 * @param {Array<object>} devices
 * @param {{ model?: string, keyword?: string, slotUsed?: Record<string, number>, powerBound?: Record<string, number>, slotMax?: number }} [opts]
 * @returns {Array<{
 *   device_id: string,
 *   label: string,
 *   model_class: string,
 *   slotUsed: number,
 *   slotMax: number,
 *   hasFreeSlot: boolean,
 *   powerBound: number,
 * }>}
 */
export function filterBindTargets(devices, opts) {
  const o = opts || {}
  const model = o.model == null ? '' : String(o.model).trim()
  const keyword = o.keyword == null ? '' : String(o.keyword)
  const slotMaxRaw = o.slotMax == null ? SLOT_MAX : Number(o.slotMax)
  const slotMax = Number.isFinite(slotMaxRaw) && slotMaxRaw > 0 ? slotMaxRaw : SLOT_MAX
  const slotUsed = o.slotUsed || null
  const powerBound = o.powerBound || null

  const rows = []
  for (const d of devices || []) {
    if (!isBindableDeviceListed(d)) continue
    if (model) {
      const mc = d.model_class != null ? String(d.model_class).trim() : ''
      if (mc !== model) continue
    }
    if (!deviceMatchesKeyword(d, keyword)) continue

    const used = _slotUsedOf(d, slotUsed)
    const bound = _powerBoundOf(d, powerBound)
    const hasFreeSlot = used < slotMax
    const display = (d.display_name && String(d.display_name).trim()) || d.device_id
    const modelLabel = (d.model_class != null && String(d.model_class).trim())
      ? String(d.model_class).trim()
      : '未标机型'
    const posLabel = (d.position_code != null && String(d.position_code).trim())
      ? ' · 位置 ' + String(d.position_code).trim()
      : ''
    let label =
      display +
      ' · ' +
      d.device_id +
      ' · ' +
      modelLabel +
      posLabel +
      ' · 槽 ' +
      used +
      '/' +
      slotMax +
      ' · 电源已绑 ' +
      bound
    if (!hasFreeSlot) label += '（无空闲槽）'

    rows.push({
      device_id: d.device_id,
      label,
      model_class: d.model_class != null ? String(d.model_class).trim() : '',
      slotUsed: used,
      slotMax,
      hasFreeSlot,
      powerBound: bound,
    })
  }

  rows.sort((a, b) => {
    if (a.hasFreeSlot !== b.hasFreeSlot) return a.hasFreeSlot ? -1 : 1
    const an = (a.label || '').toLowerCase()
    const bn = (b.label || '').toLowerCase()
    if (an < bn) return -1
    if (an > bn) return 1
    return String(a.device_id).localeCompare(String(b.device_id))
  })
  return rows
}
