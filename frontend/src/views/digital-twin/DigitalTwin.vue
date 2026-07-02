<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, watch, markRaw, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import {
  Connection,
  Van,
  Timer,
  Refresh,
  Monitor,
  TrendCharts,
  Warning,
  CircleCheck
} from '@element-plus/icons-vue'
import { getDashboardStats } from '@/api/dashboard'
import { getOrderList } from '@/api/order'
import { getInventoryList } from '@/api/inventory'
import { getMaterialWarehouseHeatmap, getCapacityUtilizationHeatmap } from '@/api/inventory'
import { getSupplierList } from '@/api/supplier'
import { getPurchaseOrderList } from '@/api/purchase'

const silentRequestOptions = { skipErrorHandler: true } as const

interface SupplyChainNode {
  id: string
  name: string
  type: 'supplier' | 'factory' | 'warehouse' | 'customer'
  x: number
  y: number
  status: 'normal' | 'warning' | 'critical'
  metrics: {
    inventory_level?: number
    capacity_utilization?: number
    delivery_rate?: number
  }
}

interface LogisticsEvent {
  id: string
  type: 'shipment' | 'receipt' | 'production' | 'quality_check'
  source: string
  destination: string
  material_name: string
  quantity: number
  status: 'in_transit' | 'completed' | 'delayed'
  progress: number
  estimated_arrival: string
  actual_position?: { lat: number; lng: number }
  route: Array<{ lat: number; lng: number; timestamp: string }>
}

interface TimelineEvent {
  time: string
  title: string
  description: string
  type: 'primary' | 'info' | 'warning' | 'success' | 'danger'
}

interface KpiCard {
  title: string
  value: string | number
  icon: typeof Monitor
  color: string
  gradient: string
  change: string
}

interface BottleneckInfo {
  material_code: string
  material_name: string
  shortage_qty: number
  affected_orders: number
  severity: 'critical' | 'major' | 'minor'
  suggestion: string
}

interface ReplayState {
  isPlaying: boolean
  currentDate: string
  speed: number
  dateRange: { start: string; end: string }
}

interface Particle {
  x: number
  y: number
  sourceId: string
  destinationId: string
  progress: number
  speed: number
  color: string
  size: number
  trail: Array<{ x: number; y: number; alpha: number }>
}

interface InventoryTransferAnimation {
  id: string
  type: 'in' | 'out'
  x: number
  y: number
  targetX: number
  targetY: number
  progress: number
  quantity: number
  active: boolean
}

const loading = ref(false)
const selectedNode = ref<SupplyChainNode | null>(null)
const selectedEvent = ref<LogisticsEvent | null>(null)
const hoveredNodeId = ref<string | null>(null)

// 节点详情弹窗位置
const nodePopupX = ref(0)
const nodePopupY = ref(0)

// 新增：瓶颈检测数据
const bottleneckMaterials = ref<Array<{code: string; name: string; shortage: number; impact: number}>>([])
const bottleneckWorkcenters = ref<Array<{name: string; utilization: number; queue: number}>>([])
const bottleneckInfoList = ref<BottleneckInfo[]>([])

// 新增：时间轴回放状态
const replayState = ref<ReplayState>({
  isPlaying: false,
  currentDate: '',
  speed: 1,
  dateRange: { start: '', end: '' }
})
const replayDateRange = ref<'7days' | '30days' | 'custom'>('7days')

// 新增：粒子系统
const particles = ref<Particle[]>([])
const maxParticles = 50

// 新增：库存转移动画
const transferAnimations = ref<InventoryTransferAnimation[]>([])

// 新增：热力图数据
const heatmapData = ref<{
  materialWarehouse: Array<{material: string; warehouse: string; value: number; status: 'sufficient' | 'low' | 'shortage' | 'none'; ratio: number}>
  capacityUtilization: Array<{workcenter: string; date: string; utilization: number}>
}>({
  materialWarehouse: [],
  capacityUtilization: []
})

// 矩阵热力图数据：行=仓库，列=物料
interface MatrixCell {
  value: number
  display_qty: number   // 该物料的跨仓库总库存（格子显示用）
  ratio: number
  status: 'stocked' | 'none'  // stocked=有货, none=未存放
}
const heatmapMatrix = ref<{
  warehouses: string[]
  materials: string[]
  cells: Record<string, Record<string, MatrixCell>>  // [warehouse][material]
  materialWarehouseMap: Record<string, string[]>      // [material] => [warehouses]
}>({
  warehouses: [],
  materials: [],
  cells: {},
  materialWarehouseMap: {}
})

// 新增：动画时间戳
let replayTimer: ReturnType<typeof setInterval> | null = null

// 新增：热力图状态
const activeHeatmapTab = ref<'material' | 'capacity'>('material')
const tooltipVisible = ref(false)
const tooltipX = ref(0)
const tooltipY = ref(0)
const tooltipDirection = ref<'up' | 'down'>('up')
const materialTotalStats = ref<Record<string, any>>({})
const tooltipContent = ref<{ title: string; value: string; status: string }>({
  title: '',
  value: '',
  status: ''
})

const supplyChainNodes = ref<SupplyChainNode[]>([])
const logisticsEvents = ref<LogisticsEvent[]>([])

const savedViewMode = localStorage.getItem('digitalTwinViewMode') as 'network' | 'timeline' | 'heatmap' | null
const viewMode = ref<'network' | 'timeline' | 'heatmap'>(savedViewMode || 'network')
const autoRefresh = ref(false)
const refreshInterval = ref(60)

const canvasRef = ref<HTMLCanvasElement | null>(null)
let canvas: HTMLCanvasElement | null = null
let ctx: CanvasRenderingContext2D | null = null
let animationId: number | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null
let resizeObserver: ResizeObserver | null = null
let isRefreshing = false // 防止重复请求导致429
let resizeDebounceTimer: ReturnType<typeof setTimeout> | null = null

const topologyPalette = {
  bgTop: '#07101b',
  bgBottom: '#020712',
  grid: 'rgba(105, 179, 210, 0.035)',
  gridStrong: 'rgba(105, 179, 210, 0.075)',
  text: '#dcecf6',
  muted: 'rgba(178, 205, 220, 0.58)',
  panel: 'rgba(7, 15, 27, 0.78)',
  supplier: '#41c99b',
  factory: '#4aa8e6',
  warehouse: '#d8a431',
  customer: '#e86f83',
  active: '#3bd6bf',
  warning: '#d8a431',
  danger: '#e85d75'
}

const nodeMeta: Record<SupplyChainNode['type'], { color: string; glow: string; label: string; glyph: string }> = {
  supplier: { color: topologyPalette.supplier, glow: 'rgba(65, 201, 155, 0.18)', label: '供应', glyph: 'S' },
  factory: { color: topologyPalette.factory, glow: 'rgba(74, 168, 230, 0.22)', label: '生产', glyph: 'P' },
  warehouse: { color: topologyPalette.warehouse, glow: 'rgba(216, 164, 49, 0.18)', label: '仓储', glyph: 'W' },
  customer: { color: topologyPalette.customer, glow: 'rgba(232, 111, 131, 0.17)', label: '客户', glyph: 'C' }
}

const hexToRgba = (hex: string, alpha: number) => {
  const normalized = hex.replace('#', '')
  const r = parseInt(normalized.slice(0, 2), 16)
  const g = parseInt(normalized.slice(2, 4), 16)
  const b = parseInt(normalized.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

const drawRoundedRect = (
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number
) => {
  const r = Math.min(radius, width / 2, height / 2)
  context.beginPath()
  context.moveTo(x + r, y)
  context.lineTo(x + width - r, y)
  context.quadraticCurveTo(x + width, y, x + width, y + r)
  context.lineTo(x + width, y + height - r)
  context.quadraticCurveTo(x + width, y + height, x + width - r, y + height)
  context.lineTo(x + r, y + height)
  context.quadraticCurveTo(x, y + height, x, y + height - r)
  context.lineTo(x, y + r)
  context.quadraticCurveTo(x, y, x + r, y)
  context.closePath()
}

const clampNumber = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value))

const getTopologyScale = (width = canvas?.width || 1000, height = canvas?.height || 540) => {
  return clampNumber(Math.min(width / 1320, height / 560), 0.72, 1.04)
}

const getCanvasNodePoint = (node: SupplyChainNode, pad = 34) => {
  if (!canvas) return { x: node.x, y: node.y }
  // 右边距额外留出空间，防止节点（最大宽约196px）溢出画布右边界
  const rightPad = Math.max(pad, 100)
  return {
    x: Math.max(pad, Math.min(canvas.width - rightPad, node.x)),
    y: Math.max(pad, Math.min(canvas.height - pad, node.y))
  }
}

const distributeY = (count: number, top: number, height: number, startRatio = 0.14, endRatio = 0.86) => {
  if (count <= 1) return [top + height * 0.5]
  const start = top + height * startRatio
  const end = top + height * endRatio
  return Array.from({ length: count }, (_, index) => start + ((end - start) * index) / (count - 1))
}

const applyAdaptiveNetworkLayout = () => {
  if (!canvas || supplyChainNodes.value.length === 0) return

  const width = canvas.width
  const height = canvas.height
  const compact = width < 900
  const left = compact ? Math.max(46, width * 0.045) : Math.max(78, width * 0.05)
  const rightReserve = compact ? Math.max(44, width * 0.04) : Math.max(300, width * 0.15)
  const right = Math.min(width - 24, Math.max(left + (compact ? 360 : 520), width - rightReserve))
  const top = Math.max(56, height * 0.12)
  const bottomReserve = Math.max(120, height * 0.20)
  const bottom = Math.max(top + 230, height - bottomReserve)
  const graphWidth = Math.max(520, right - left)
  const graphHeight = Math.max(230, bottom - top)

  const supplierNodes = supplyChainNodes.value.filter(node => node.type === 'supplier')
  const warehouseNodes = supplyChainNodes.value.filter(node => node.type === 'warehouse')
  const customerNodes = supplyChainNodes.value.filter(node => node.type === 'customer')
  const factoryNode = supplyChainNodes.value.find(node => node.type === 'factory')

  const setNode = (node: SupplyChainNode | undefined, x: number, y: number) => {
    if (!node) return
    // 右边距需考虑节点半宽（最大节点宽约196px → 半宽约98px），避免节点溢出画布
    const maxX = compact ? width - 90 : width - 110
    node.x = clampNumber(x, 38, maxX)
    node.y = clampNumber(y, 42, height - 118)
  }

  if (supplierNodes.length > 0) {
    const useTwoColumns = !compact && supplierNodes.length > 4
    if (useTwoColumns) {
      const leftCol = supplierNodes.filter((_, index) => index % 2 === 0)
      const rightCol = supplierNodes.filter((_, index) => index % 2 === 1)
      const leftY = distributeY(leftCol.length, top, graphHeight, 0.16, 0.84)
      const rightY = distributeY(rightCol.length, top, graphHeight, 0.16, 0.84)
      leftCol.forEach((node, index) => setNode(node, left + graphWidth * 0.05, leftY[index]))
      rightCol.forEach((node, index) => setNode(node, left + graphWidth * 0.18, rightY[index]))
    } else {
      const ys = distributeY(supplierNodes.length, top, graphHeight, 0.16, 0.84)
      supplierNodes.forEach((node, index) => setNode(node, left + graphWidth * 0.10, ys[index]))
    }
  }

  setNode(factoryNode, left + graphWidth * (compact ? 0.40 : 0.39), top + graphHeight * 0.5)

  const warehouseY = distributeY(warehouseNodes.length, top, graphHeight, 0.32, 0.68)
  warehouseNodes.forEach((node, index) => {
    setNode(node, left + graphWidth * (compact ? 0.64 : 0.62), warehouseY[index])
  })

  const customerY = distributeY(customerNodes.length, top, graphHeight, 0.12, 0.88)
  customerNodes.forEach((node, index) => {
    setNode(node, left + graphWidth * (compact ? 0.86 : 0.88), customerY[index])
  })
}

// ========== 从真实API数据构建供应链网络 ==========
const buildNetworkFromData = (
  suppliers: any[],
  orders: any[],
  inventory: any[],
  purchaseOrders: any[],
  stats: any = null,
  cw: number = 1000,
  ch: number = 540
) => {
  const nodes: SupplyChainNode[] = []
  const events: LogisticsEvent[] = []
  const timeline: TimelineEvent[] = []
  let nodeId = 0
  // 基于画布宽度的相对坐标
  // ★ 核心改动：节点只在中央区域绘制，四周留白给浮动面板(HUD式覆盖)
  const w = Math.max(cw, 400)
  const h = Math.max(ch, 360)
  // 绘图区域边距：留出顶部图例与底部指标，主体拓扑保持居中均衡
  const drawL = w * 0.06
  const drawR = w * 0.07
  const drawT = h * 0.18
  const drawB = h * 0.24
  const drawW = w - drawL - drawR   // 可用绘图宽度
  const drawH = h - drawT - drawB   // 可用绘图高度

  // --- 供应商节点（绘图区左侧，最多10个，两列）---
  const fallbackSuppliers = [
    { supplier_name: '供应源A', delivery_reliability: 0.94 },
    { supplier_name: '供应源B', delivery_reliability: 0.88 },
    { supplier_name: '供应源C', delivery_reliability: 0.91 }
  ]
  const visibleSuppliers = suppliers.length > 0 ? suppliers.slice(0, 6) : fallbackSuppliers
  const maxSuppliers = visibleSuppliers.length
  visibleSuppliers.forEach((sup, i) => {
    const rawRate = sup.delivery_reliability != null ? sup.delivery_reliability : sup.rating
    const rate = rawRate != null ? (rawRate === 'A' ? 0.95 : rawRate === 'B' ? 0.85 : typeof rawRate === 'number' ? Math.min(1, Math.max(0, rawRate)) : 0.88) : 0.88
    const col = maxSuppliers > 4 ? i % 2 : 0
    const row = maxSuppliers > 4 ? Math.floor(i / 2) : i
    const colX = drawL + drawW * (col === 0 ? 0.04 : 0.15)
    const totalRows = maxSuppliers > 4 ? Math.ceil(maxSuppliers / 2) : maxSuppliers
    const startY = drawT + drawH * 0.14
    const endY = drawT + drawH * 0.86
    const rowStep = totalRows > 1 ? (endY - startY) / (totalRows - 1) : 0
    nodes.push({
      id: `SUP${String(nodeId++).padStart(3, '0')}`,
      name: (sup.supplier_name || sup.supplier_code || `供应商${i + 1}`).substring(0, 12),
      type: 'supplier',
      x: colX,
      y: startY + row * rowStep,
      status: rate >= 0.9 ? 'normal' : rate >= 0.8 ? 'warning' : 'critical',
      metrics: { delivery_rate: rate }
    })
  })

  // --- 工厂节点（绘图区中心偏右，作为汇聚点）---
  let factoryUtilization = 75
  if (stats && stats.capacity_utilization != null) {
    factoryUtilization = Math.min(99, Math.round(Number(stats.capacity_utilization) * 100))
  } else {
    const completedOrders = orders.filter(o => o.status === 'complete' || o.status === 'completed' || o.status === 'shipped').length
    factoryUtilization = orders.length > 0 ? Math.min(99, Math.round(completedOrders / orders.length * 100)) : 75
  }
  nodes.push({
    id: 'FACTORY01',
    name: '联宝生产基地',
    type: 'factory',
    x: drawL + drawW * 0.36,
    y: drawT + drawH * 0.50,
    status: factoryUtilization > 80 ? 'normal' : factoryUtilization > 50 ? 'warning' : 'critical',
    metrics: { capacity_utilization: factoryUtilization / 100, inventory_level: 65 }
  })

  // --- 仓库节点（工厂右侧）---
  const totalInvQty = inventory.reduce((s: number, inv: any) => s + (inv.quantity || 0), 0)
  const avgInvLevel = inventory.length > 0 ? Math.min(99, Math.round(totalInvQty / Math.max(inventory.length * 100000, 1) * 100)) : 70
  nodes.push({
    id: 'WH001',
    name: `中央仓库 (${inventory.length}SKU)`,
    type: 'warehouse',
    x: drawL + drawW * 0.60,
    y: drawT + drawH * 0.33,
    status: avgInvLevel > 50 ? 'normal' : 'warning',
    metrics: { inventory_level: avgInvLevel }
  })
  const lowStockCount = inventory.filter((inv: any) => {
    const mat = typeof inv.material === 'object' ? inv.material : null
    return mat && inv.quantity < (mat.safety_stock || 100) * 0.5
  }).length
  nodes.push({
    id: 'WH002',
    name: lowStockCount > 0 ? `分拨仓(预警${lowStockCount})` : '分拨仓',
    type: 'warehouse',
    x: drawL + drawW * 0.60,
    y: drawT + drawH * 0.67,
    status: lowStockCount > 5 ? 'critical' : lowStockCount > 0 ? 'warning' : 'normal',
    metrics: { inventory_level: Math.max(10, avgInvLevel - 30) }
  })

  // --- 客户节点（绘图区最右侧）---
  const customerNames = [...new Set(orders.map(o => o.customer_name).filter(Boolean))]
  const maxCustomers = Math.min(customerNames.length, 4)
  if (maxCustomers > 0) {
    customerNames.slice(0, maxCustomers).forEach((name, i) => {
      const custOrders = orders.filter(o => o.customer_name === name)
      const urgentOrders = custOrders.filter(o => (o.priority || 0) <= 2)
      const yRatio = maxCustomers === 1 ? 0.5 : 0.14 + (i / (maxCustomers - 1)) * 0.72
      nodes.push({
        id: `CUS${String(i + 1).padStart(3, '0')}`,
        name: `${name.substring(0, 8)}(${custOrders.length}单)`,
        type: 'customer',
        x: drawL + drawW * 0.83,
        y: drawT + drawH * yRatio,
        status: urgentOrders.length > 0 ? 'warning' : 'normal',
        metrics: {}
      })
    })
  } else {
    ;['客户中心A', '客户中心B', '客户中心C'].forEach((name, i) => {
      nodes.push({ id: `CUS${String(i + 1).padStart(3, '0')}`, name, type: 'customer', x: drawL + drawW * 0.83, y: drawT + drawH * (0.22 + i * 0.28), status: 'normal', metrics: {} })
    })
  }

  // --- 物流事件（从采购订单生成，最多15条）---
  const supplierNodeIds = nodes.filter(n => n.type === 'supplier').map(n => n.id)
  purchaseOrders.slice(0, 15).forEach((po, i) => {
    const matName = (typeof po.material === 'object' ? po.material?.material_name : po.material_code || `物料${i + 1}`) || `采购物料${i + 1}`
    const srcSupplier = supplierNodeIds[i % supplierNodeIds.length] || 'SUP000'
    events.push({
      id: `LOG${String(i + 1).padStart(3, '0')}`,
      type: 'shipment',
      source: srcSupplier,
      destination: 'FACTORY01',
      material_name: String(matName).substring(0, 12),
      quantity: po.quantity || 0,
      status: po.status === 'completed' ? 'completed' : po.status === 'cancelled' ? 'delayed' : 'in_transit',
      progress: po.status === 'completed' ? 100 : po.status === 'confirmed' ? 60 : po.status === 'shipped' ? 85 : 30,
      estimated_arrival: po.delivery_date || '',
      route: []
    })
  })
  // 补充销售订单作为出库事件（最多10条）
  const customerNodeIds = nodes.filter(n => n.type === 'customer').map(n => n.id)
  orders.slice(0, 10).forEach((order, i) => {
    const matName = (typeof order.material === 'object' ? order.material?.material_name : order.material_code || `产品${i + 1}`) || `产品${i + 1}`
    const orderStatusProgress: Record<string, number> = {
      pending: 10, confirmed: 15, allocated: 25, in_production: 40,
      processing: 35, partial: 55, complete: 90, shipped: 95, delivered: 100, cancelled: 0
    }
    events.push({
      id: `LOG${String(15 + i + 1).padStart(3, '0')}`,
      type: ['complete', 'completed', 'shipped', 'delivered'].includes(order.status) ? 'shipment' : 'production',
      source: 'FACTORY01',
      destination: customerNodeIds[i % customerNodeIds.length] || 'CUS001',
      material_name: String(matName).substring(0, 12),
      quantity: order.quantity || 0,
      status: ['complete', 'completed', 'shipped', 'delivered'].includes(order.status) ? 'completed'
        : ['pending', 'confirmed', 'allocated', 'in_production', 'processing', 'partial'].includes(order.status) ? 'in_transit' : 'delayed',
      progress: orderStatusProgress[order.status] ?? 20,
      estimated_arrival: order.demand_date || '',
      route: []
    })
  })

  // --- 时间线事件（从最近订单状态生成，最多12条）---
  const recentOrders = orders.slice(0, 12)
  recentOrders.forEach((o, i) => {
    const statusMap: Record<string, { type: TimelineEvent['type']; title: string; prefix: string }> = {
      complete: { type: 'success', title: '订单完成', prefix: '销售订单' },
      completed: { type: 'success', title: '订单完成', prefix: '销售订单' },
      delivered: { type: 'success', title: '已交付', prefix: '销售订单' },
      shipped: { type: 'success', title: '已发货', prefix: '销售订单' },
      confirmed: { type: 'success', title: '已确认', prefix: '销售订单' },
      in_production: { type: 'primary', title: '生产中', prefix: '销售订单' },
      processing: { type: 'primary', title: '进行中', prefix: '销售订单' },
      partial: { type: 'warning', title: '部分齐套', prefix: '销售订单' },
      pending: { type: 'info', title: '待处理', prefix: '销售订单' },
      allocated: { type: 'info', title: '已占料', prefix: '销售订单' },
      cancelled: { type: 'danger', title: '已取消', prefix: '销售订单' }
    }
    const info = statusMap[o.status] || { type: 'info' as const, title: '订单更新', prefix: '订单' }
    timeline.push({
      time: `${String(16 - i).padStart(2, '0')}:${String(30 + i * 12).padStart(2, '0')}`,
      title: info.title,
      description: `${info.prefix} ${o.order_no || ''} 数量:${o.quantity || 0} 状态:${info.title}`,
      type: info.type
    })
  })
  if (timeline.length === 0) {
    timeline.push(
      { time: '--:--', title: '系统正常', description: '数据同步完成，所有服务可用', type: 'success' as const }
    )
  }

  return { nodes, events, timeline }
}

const kpiCards = ref<KpiCard[]>([
  { title: '网络节点总数', value: 0, icon: markRaw(Connection), color: topologyPalette.factory, gradient: 'linear-gradient(135deg, #38bdf8, #2dd4bf)', change: '' },
  { title: '在途运单数', value: 0, icon: markRaw(Van), color: topologyPalette.active, gradient: 'linear-gradient(135deg, #2dd4bf, #34d399)', change: '' },
  { title: '异常告警数', value: 0, icon: markRaw(Warning), color: topologyPalette.warning, gradient: 'linear-gradient(135deg, #f59e0b, #fbbf24)', change: '' },
  { title: '系统健康度', value: '0%', icon: markRaw(CircleCheck), color: topologyPalette.supplier, gradient: 'linear-gradient(135deg, #34d399, #5eead4)', change: '' }
])

const timelineEvents = ref<TimelineEvent[]>([])

const updateKpiData = () => {
  const totalNodes = supplyChainNodes.value.length
  const activeShipments = logisticsEvents.value.filter(e => e.status === 'in_transit').length
  const delayedItems = logisticsEvents.value.filter(e => e.status === 'delayed').length
  const criticalNodes = supplyChainNodes.value.filter(n => n.status === 'critical').length
  const healthRate = totalNodes > 0 ? Number(((totalNodes - criticalNodes) / totalNodes * 100)).toFixed(0) : '100'

  kpiCards.value[0].value = totalNodes
  kpiCards.value[1].value = activeShipments
  kpiCards.value[2].value = delayedItems + criticalNodes
  kpiCards.value[3].value = `${healthRate}%`
}

const drawNetwork = () => {
  if (!canvas || !ctx) return

  const width = canvas.width
  const height = canvas.height

  ctx.clearRect(0, 0, width, height)
  drawGrid()
  drawConnections()
  drawNodes()
  drawLogisticsAnimation()
  drawBottleneckHighlights()
}

const drawGrid = () => {
  if (!ctx || !canvas) return
  const context = ctx
  const { width, height } = canvas

  const bg = context.createLinearGradient(0, 0, 0, height)
  bg.addColorStop(0, topologyPalette.bgTop)
  bg.addColorStop(0.58, '#06101a')
  bg.addColorStop(1, topologyPalette.bgBottom)
  context.fillStyle = bg
  context.fillRect(0, 0, width, height)

  const contentWash = context.createLinearGradient(0, 0, width, 0)
  contentWash.addColorStop(0, 'rgba(65, 201, 155, 0.045)')
  contentWash.addColorStop(0.42, 'rgba(74, 168, 230, 0.06)')
  contentWash.addColorStop(0.7, 'rgba(216, 164, 49, 0.035)')
  contentWash.addColorStop(1, 'rgba(232, 111, 131, 0.025)')
  context.fillStyle = contentWash
  context.fillRect(0, height * 0.12, width, height * 0.68)

  const left = width < 900 ? Math.max(46, width * 0.045) : Math.max(78, width * 0.05)
  const rightReserve = width < 900 ? Math.max(44, width * 0.04) : Math.max(300, width * 0.15)
  const right = Math.min(width - 24, Math.max(left + (width < 900 ? 360 : 520), width - rightReserve))
  const graphWidth = Math.max(520, right - left)
  const zones = [
    { label: '供应端', x: left, w: graphWidth * 0.24, color: topologyPalette.supplier },
    { label: '生产中枢', x: left + graphWidth * 0.29, w: graphWidth * 0.20, color: topologyPalette.factory },
    { label: '仓储分拨', x: left + graphWidth * 0.54, w: graphWidth * 0.22, color: topologyPalette.warehouse },
    { label: '客户交付', x: left + graphWidth * 0.80, w: graphWidth * 0.20, color: topologyPalette.customer }
  ]

  zones.forEach(zone => {
    const zoneY = Math.max(56, height * 0.12)
    const zoneH = Math.max(230, height - zoneY - Math.max(120, height * 0.20))
    context.save()
    drawRoundedRect(context, zone.x, zoneY, zone.w, zoneH, 10)
    context.fillStyle = hexToRgba(zone.color, 0.026)
    context.fill()
    context.strokeStyle = hexToRgba(zone.color, 0.11)
    context.lineWidth = 1
    context.stroke()
    context.fillStyle = hexToRgba(zone.color, 0.72)
    context.font = `600 ${Math.max(10, width * 0.008)}px "Microsoft YaHei", Arial`
    context.textAlign = 'left'
    context.textBaseline = 'middle'
    context.fillText(zone.label, zone.x + 12, zoneY + 16)
    context.restore()
  })

  const fineGrid = Math.max(34, Math.round(width / 28))
  context.lineWidth = 1
  for (let x = 0; x < width; x += fineGrid) {
    context.strokeStyle = x % (fineGrid * 3) === 0 ? topologyPalette.gridStrong : topologyPalette.grid
    context.beginPath()
    context.moveTo(x, 0)
    context.lineTo(x, height)
    context.stroke()
  }

  for (let y = 0; y < height; y += fineGrid) {
    context.strokeStyle = y % (fineGrid * 3) === 0 ? topologyPalette.gridStrong : topologyPalette.grid
    context.beginPath()
    context.moveTo(0, y)
    context.lineTo(width, y)
    context.stroke()
  }

  context.strokeStyle = 'rgba(220, 236, 246, 0.055)'
  context.lineWidth = 1
  context.beginPath()
  context.moveTo(width * 0.04, height * 0.78)
  context.lineTo(width * 0.96, height * 0.78)
  context.stroke()
}

const drawConnections = () => {
  if (!ctx || !canvas || !supplyChainNodes.value.length) return
  const context = ctx
  const connScale = getTopologyScale()

  // 动态生成连线：所有供应商→工厂→仓库→客户
  const connections: Array<{ from: string; to: string }> = []
  supplyChainNodes.value.filter(n => n.type === 'supplier').forEach(s => {
    connections.push({ from: s.id, to: 'FACTORY01' })
  })
  const whNodes = supplyChainNodes.value.filter(n => n.type === 'warehouse')
  whNodes.forEach(wh => {
    connections.push({ from: 'FACTORY01', to: wh.id })
  })
  const cusNodes = supplyChainNodes.value.filter(n => n.type === 'customer')
  cusNodes.forEach(cus => {
    // 每个客户连接到最近的仓库
    const targetWh = whNodes.length > 0 ? whNodes[cusNodes.indexOf(cus) % whNodes.length] : null
    if (targetWh) {
      connections.push({ from: targetWh.id, to: cus.id })
    }
  })

  connections.forEach(conn => {
    const fromNode = supplyChainNodes.value.find(n => n.id === conn.from)
    const toNode = supplyChainNodes.value.find(n => n.id === conn.to)

    if (fromNode && toNode) {
      const activeEvent = logisticsEvents.value.find(
        e => e.source === fromNode.id && e.destination === toNode.id && e.status === 'in_transit'
      )
      const from = getCanvasNodePoint(fromNode)
      const to = getCanvasNodePoint(toNode)
      const dx = to.x - from.x
      const bend = Math.max(42, Math.abs(dx) * 0.36)
      const curveLift = (fromNode.type === 'supplier' && toNode.type === 'factory') ? -10 : 0
      const cp1 = { x: from.x + bend, y: from.y + curveLift }
      const cp2 = { x: to.x - bend, y: to.y - curveLift }
      const activeColor = activeEvent ? topologyPalette.active : '#7dd3fc'
      const pulseAlpha = activeEvent ? 0.5 + Math.sin(Date.now() / 420) * 0.16 : 0.22
      const lineGradient = context.createLinearGradient(from.x, from.y, to.x, to.y)
      lineGradient.addColorStop(0, hexToRgba(nodeMeta[fromNode.type].color, activeEvent ? 0.72 : 0.28))
      lineGradient.addColorStop(0.5, hexToRgba(activeColor, activeEvent ? pulseAlpha : 0.18))
      lineGradient.addColorStop(1, hexToRgba(nodeMeta[toNode.type].color, activeEvent ? 0.72 : 0.28))

      context.save()
      context.lineCap = 'round'
      context.setLineDash(activeEvent ? [] : [7 * connScale, 9 * connScale])
      if (activeEvent) {
        context.strokeStyle = hexToRgba(activeColor, 0.18)
        context.lineWidth = Math.max(6, 8 * connScale)
        context.shadowBlur = 16 * connScale
        context.shadowColor = hexToRgba(activeColor, 0.42)
        context.beginPath()
        context.moveTo(from.x, from.y)
        context.bezierCurveTo(cp1.x, cp1.y, cp2.x, cp2.y, to.x, to.y)
        context.stroke()
      }

      context.shadowBlur = 0
      context.strokeStyle = lineGradient
      context.lineWidth = activeEvent ? Math.max(2, 2.6 * connScale) : Math.max(1, 1.2 * connScale)
      context.beginPath()
      context.moveTo(from.x, from.y)
      context.bezierCurveTo(cp1.x, cp1.y, cp2.x, cp2.y, to.x, to.y)
      context.stroke()
      context.setLineDash([])

      const angle = Math.atan2(to.y - cp2.y, to.x - cp2.x)
      const arrowSize = activeEvent ? 7 * connScale : 5 * connScale
      context.fillStyle = hexToRgba(activeColor, activeEvent ? 0.82 : 0.36)
      context.beginPath()
      context.moveTo(to.x - Math.cos(angle) * 18 * connScale, to.y - Math.sin(angle) * 18 * connScale)
      context.lineTo(
        to.x - Math.cos(angle) * (18 * connScale + arrowSize) + Math.cos(angle + Math.PI / 2) * arrowSize,
        to.y - Math.sin(angle) * (18 * connScale + arrowSize) + Math.sin(angle + Math.PI / 2) * arrowSize
      )
      context.lineTo(
        to.x - Math.cos(angle) * (18 * connScale + arrowSize) + Math.cos(angle - Math.PI / 2) * arrowSize,
        to.y - Math.sin(angle) * (18 * connScale + arrowSize) + Math.sin(angle - Math.PI / 2) * arrowSize
      )
      context.closePath()
      context.fill()
      context.restore()
    }
  })
}

const drawNodes = () => {
  const currentCanvas = canvas
  if (!ctx || !currentCanvas || !supplyChainNodes.value.length) return
  const context = ctx
  baseScale = getTopologyScale(currentCanvas.width, currentCanvas.height)
  const compactNode = currentCanvas.width < 760
  const nodeScale = compactNode ? clampNumber(currentCanvas.width / 620, 0.68, 0.82) : baseScale
  const pad = compactNode ? 24 : Math.max(44, 46 * baseScale)

  const truncateText = (text: string, maxWidth: number) => {
    if (context.measureText(text).width <= maxWidth) return text
    let result = text
    while (result.length > 1 && context.measureText(`${result}…`).width > maxWidth) {
      result = result.slice(0, -1)
    }
    return `${result}…`
  }

  const getMetricText = (node: SupplyChainNode) => {
    if (node.type === 'supplier') {
      const rate = node.metrics.delivery_rate != null ? `${Math.round(node.metrics.delivery_rate * 100)}%` : '--'
      return `准交 ${rate}`
    }
    if (node.type === 'factory') {
      const capacity = node.metrics.capacity_utilization != null ? `${Math.round(node.metrics.capacity_utilization * 100)}%` : '--'
      const stock = node.metrics.inventory_level != null ? `${Math.round(node.metrics.inventory_level)}%` : '--'
      return `产能 ${capacity} · 库存 ${stock}`
    }
    if (node.type === 'warehouse') {
      return `库存 ${node.metrics.inventory_level != null ? Math.round(node.metrics.inventory_level) + '%' : '--'}`
    }
    return '交付节点'
  }

  supplyChainNodes.value.forEach(node => {
    const { x: rx, y: ry } = getCanvasNodePoint(node, pad)
    const isSelected = selectedNode.value?.id === node.id
    const isHovered = hoveredNodeId.value === node.id
    const meta = nodeMeta[node.type]
    const width = compactNode
      ? (node.type === 'factory' ? 116 : node.type === 'warehouse' ? 104 : 96) * nodeScale
      : (node.type === 'factory' ? 196 : node.type === 'warehouse' ? 172 : 160) * baseScale
    const height = compactNode
      ? (node.type === 'factory' ? 46 : 40) * nodeScale
      : (node.type === 'factory' ? 60 : 50) * baseScale
    const radius = Math.max(5, 8 * nodeScale)
    const x = rx - width / 2
    const y = ry - height / 2
    const statusColor = node.status === 'critical' ? topologyPalette.danger : node.status === 'warning' ? topologyPalette.warning : topologyPalette.active
    const fill = context.createLinearGradient(x, y, x, y + height)
    fill.addColorStop(0, 'rgba(13, 29, 48, 0.94)')
    fill.addColorStop(1, 'rgba(6, 15, 28, 0.94)')

    context.save()
    context.shadowColor = isHovered || isSelected ? hexToRgba(meta.color, 0.32) : 'rgba(0, 0, 0, 0.22)'
    context.shadowBlur = isHovered || isSelected ? 16 * nodeScale : 8 * nodeScale
    context.shadowOffsetY = 4 * nodeScale
    drawRoundedRect(context, x, y, width, height, radius)
    context.fillStyle = fill
    context.fill()
    context.shadowBlur = 0
    context.shadowOffsetY = 0
    context.strokeStyle = isSelected ? 'rgba(255, 255, 255, 0.72)' : hexToRgba(meta.color, isHovered ? 0.56 : 0.24)
    context.lineWidth = isSelected ? 2 : 1
    context.stroke()
    context.restore()

    context.save()
    const iconInset = compactNode ? 5 * nodeScale : 8 * baseScale
    const iconWidth = compactNode ? 20 * nodeScale : 28 * baseScale
    drawRoundedRect(context, x + iconInset, y + iconInset, iconWidth, height - iconInset * 2, Math.max(4, 6 * nodeScale))
    context.fillStyle = hexToRgba(meta.color, 0.15)
    context.fill()
    context.strokeStyle = hexToRgba(meta.color, 0.34)
    context.lineWidth = 1
    context.stroke()
    context.fillStyle = meta.color
    context.font = `800 ${compactNode ? Math.max(8, 14 * nodeScale) : Math.max(11, 13 * baseScale)}px "Microsoft YaHei", Arial`
    context.textAlign = 'center'
    context.textBaseline = 'middle'
    context.fillText(meta.glyph, x + iconInset + iconWidth / 2, y + height / 2)
    context.restore()

    context.save()
    context.fillStyle = topologyPalette.text
    context.font = `700 ${compactNode ? Math.max(8, 13 * nodeScale) : Math.max(10, 12 * baseScale)}px "Microsoft YaHei", Arial`
    context.textAlign = 'left'
    context.textBaseline = 'alphabetic'
    const textX = x + (compactNode ? 31 * nodeScale : 44 * baseScale)
    const textMaxWidth = width - (compactNode ? 34 * nodeScale : 50 * baseScale)
    const name = truncateText(node.name, textMaxWidth)
    context.fillText(name, textX, y + (compactNode ? 18 : 22) * nodeScale)
    context.fillStyle = topologyPalette.muted
    context.font = `500 ${compactNode ? Math.max(7, 10.5 * nodeScale) : Math.max(8, 9.5 * baseScale)}px "Microsoft YaHei", Arial`
    context.fillText(truncateText(getMetricText(node), textMaxWidth), textX, y + (compactNode ? 32 : 39) * nodeScale)
    context.restore()

    context.save()
    context.fillStyle = statusColor
    context.beginPath()
    context.arc(x + width - 12 * nodeScale, y + 12 * nodeScale, 4.5 * nodeScale, 0, Math.PI * 2)
    context.fill()
    context.strokeStyle = 'rgba(2, 7, 18, 0.9)'
    context.lineWidth = 2
    context.stroke()
    context.restore()

    if (isSelected || isHovered) {
      context.save()
      context.strokeStyle = hexToRgba(meta.color, isSelected ? 0.82 : 0.48)
      context.lineWidth = 1
      drawRoundedRect(context, x - 4 * nodeScale, y - 4 * nodeScale, width + 8 * nodeScale, height + 8 * nodeScale, 11 * nodeScale)
      context.stroke()
      context.restore()
    }
  })
}

const drawLogisticsAnimation = () => {
  if (!ctx || !canvas || !logisticsEvents.value.length) return
  const context = ctx
  const animScale = getTopologyScale()

  const time = Date.now() / 1000

  // 清空并重新生成粒子（限制数量）
  particles.value = particles.value.filter(p => p.progress < 1)

  logisticsEvents.value.forEach(event => {
    if (event.status !== 'in_transit') return

    const fromNode = supplyChainNodes.value.find(n => n.id === event.source)
    const toNode = supplyChainNodes.value.find(n => n.id === event.destination)

    if (!fromNode || !toNode) return

    // 根据物流类型确定颜色
    const getEventColor = () => {
      if (event.type === 'shipment') {
        if (fromNode.type === 'supplier' && toNode.type === 'factory') return topologyPalette.factory // 原材料入库
        if (fromNode.type === 'factory' && toNode.type === 'warehouse') return topologyPalette.active // 半成品流转
        if (fromNode.type === 'warehouse' && toNode.type === 'customer') return topologyPalette.warehouse // 成品出库
      }
      if (event.status === 'delayed') return topologyPalette.danger // 紧急调拨
      return topologyPalette.active
    }

    const baseColor = getEventColor()
    const baseSpeed = 0.008 + Math.random() * 0.012

    // 创建粒子流（每个事件创建多个粒子）
    const particleCount = Math.min(3, maxParticles - particles.value.length)
    for (let i = 0; i < particleCount; i++) {
      if (particles.value.length >= maxParticles) break

      const progressOffset = (i / particleCount) * 0.3
      particles.value.push({
        x: fromNode.x,
        y: fromNode.y,
        sourceId: event.source,
        destinationId: event.destination,
        progress: ((event.progress / 100 + time * baseSpeed + progressOffset) % 1),
        speed: baseSpeed * (0.8 + Math.random() * 0.4),
        color: baseColor,
        size: (2 + Math.random() * 2) * animScale,
        trail: []
      })
    }

    // 绘制原始动画点（保持兼容）
    const progress = (event.progress / 100 + time * 0.02) % 1
    const from = getCanvasNodePoint(fromNode)
    const to = getCanvasNodePoint(toNode)
    const currentX = from.x + (to.x - from.x) * progress
    const currentY = from.y + (to.y - from.y) * progress

    const pulseSize = (6 + Math.sin(time * 5) * 2) * animScale

    const glowGradient = context.createRadialGradient(
      currentX, currentY, 0,
      currentX, currentY, pulseSize * 2
    )
    glowGradient.addColorStop(0, hexToRgba(baseColor, 0.82))
    glowGradient.addColorStop(1, hexToRgba(baseColor, 0))

    context.fillStyle = glowGradient
    context.beginPath()
    context.arc(currentX, currentY, pulseSize * 2, 0, Math.PI * 2)
    context.fill()

    context.fillStyle = baseColor
    context.beginPath()
    context.arc(currentX, currentY, pulseSize, 0, Math.PI * 2)
    context.fill()

    context.fillStyle = '#ffffff'
    context.font = `bold ${Math.max(8, 9 * animScale)}px Arial`
    context.textAlign = 'center'
    context.textBaseline = 'middle'
    context.fillText('>', currentX, currentY)
  })

  // 绘制粒子流（带拖尾效果）
  particles.value.forEach(particle => {
    const fromNode = supplyChainNodes.value.find(n => n.id === particle.sourceId)
    const toNode = supplyChainNodes.value.find(n => n.id === particle.destinationId)

    if (!fromNode || !toNode) return
    const from = getCanvasNodePoint(fromNode)
    const to = getCanvasNodePoint(toNode)

    // 更新粒子位置
    particle.progress += particle.speed * replayState.value.speed
    if (particle.progress > 1) particle.progress = 0
    particle.x = from.x + (to.x - from.x) * particle.progress
    particle.y = from.y + (to.y - from.y) * particle.progress

    // 添加拖尾点
    particle.trail.push({ x: particle.x, y: particle.y, alpha: 1 })
    if (particle.trail.length > 8) {
      particle.trail.shift()
    }

    // 绘制拖尾效果
    particle.trail.forEach((trailPoint, index) => {
      const trailAlpha = (index / particle.trail.length) * 0.5
      const trailSize = particle.size * (index / particle.trail.length) * 0.8

      context.fillStyle = particle.color.replace(')', `, ${trailAlpha})`).replace('rgb', 'rgba')
      if (particle.color.startsWith('#')) {
        const r = parseInt(particle.color.slice(1, 3), 16)
        const g = parseInt(particle.color.slice(3, 5), 16)
        const b = parseInt(particle.color.slice(5, 7), 16)
        context.fillStyle = `rgba(${r}, ${g}, ${b}, ${trailAlpha})`
      }
      context.beginPath()
      context.arc(trailPoint.x, trailPoint.y, Math.max(0.5, trailSize), 0, Math.PI * 2)
      context.fill()
    })

    // 绘制主粒子（带发光）
    const particleGlow = context.createRadialGradient(
      particle.x, particle.y, 0,
      particle.x, particle.y, particle.size * 3
    )

    if (particle.color.startsWith('#')) {
      const r = parseInt(particle.color.slice(1, 3), 16)
      const g = parseInt(particle.color.slice(3, 5), 16)
      const b = parseInt(particle.color.slice(5, 7), 16)
      particleGlow.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.8)`)
      particleGlow.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`)
    } else {
      particleGlow.addColorStop(0, particle.color)
      particleGlow.addColorStop(1, 'transparent')
    }

    context.fillStyle = particleGlow
    context.beginPath()
    context.arc(particle.x, particle.y, particle.size * 3, 0, Math.PI * 2)
    context.fill()

    context.fillStyle = particle.color
    context.beginPath()
    context.arc(particle.x, particle.y, particle.size, 0, Math.PI * 2)
    context.fill()
  })

  // 绘制库存转移动画（选中节点时显示）
  drawInventoryTransferAnimations()
}

// 新增：绘制库存转移动画
const drawInventoryTransferAnimations = () => {
  if (!ctx || !canvas || !selectedNode.value) return
  const context = ctx
  const animScale = getTopologyScale()
  transferAnimations.value.forEach((anim) => {
    if (!anim.active) return

    // 更新动画进度
    anim.progress += 0.015 * replayState.value.speed

    if (anim.progress >= 1) {
      anim.active = false
      return
    }

    // 计算当前位置（使用缓动函数）
    const easeProgress = 1 - Math.pow(1 - anim.progress, 3)
    const currentX = anim.x + (anim.targetX - anim.x) * easeProgress
    const currentY = anim.y + (anim.targetY - anim.y) * easeProgress

    // 绘制包裹图标背景
    const boxSize = 10 * animScale
    context.fillStyle = anim.type === 'in' ? 'rgba(56, 189, 248, 0.3)' : 'rgba(251, 191, 36, 0.3)'
    context.fillRect(currentX - boxSize/2, currentY - boxSize/2, boxSize, boxSize)

    // 绘制包裹边框
    context.strokeStyle = anim.type === 'in' ? topologyPalette.factory : topologyPalette.warehouse
    context.lineWidth = 1.5
    context.strokeRect(currentX - boxSize/2, currentY - boxSize/2, boxSize, boxSize)

    // 绘制包裹图标（简单几何形状）
    context.fillStyle = anim.type === 'in' ? topologyPalette.factory : topologyPalette.warehouse
    context.fillRect(currentX - boxSize/4, currentY - boxSize/4, boxSize/2, boxSize/2)

    // 绘制浮动数字
    if (anim.quantity !== 0) {
      const textY = currentY - 15 * animScale - (anim.progress * 20 * animScale)
      const alpha = 1 - anim.progress

      context.fillStyle = `rgba(255, 255, 255, ${alpha})`
      context.font = `bold ${Math.max(9, 11 * animScale)}px Arial`
      context.textAlign = 'center'
      context.textBaseline = 'middle'

      const sign = anim.type === 'in' ? '+' : '-'
      context.fillText(`${sign}${Math.abs(anim.quantity)}`, currentX, textY)
    }
  })

  // 清理已完成的动画
  transferAnimations.value = transferAnimations.value.filter(a => a.active)
}

// 新增：触发库存转移动画
const triggerTransferAnimation = (nodeId: string) => {
  const node = supplyChainNodes.value.find(n => n.id === nodeId)
  if (!node) return

  // 找到相关的物流事件
  const inEvents = logisticsEvents.value.filter(e => e.destination === nodeId && e.status === 'in_transit')
  const outEvents = logisticsEvents.value.filter(e => e.source === nodeId && e.status === 'in_transit')

  // 添加流入动画
  inEvents.slice(0, 2).forEach(event => {
    const fromNode = supplyChainNodes.value.find(n => n.id === event.source)
    if (fromNode && transferAnimations.value.length < 10) {
      transferAnimations.value.push({
        id: `transfer-in-${Date.now()}-${Math.random()}`,
        type: 'in',
        x: fromNode.x,
        y: fromNode.y,
        targetX: node.x,
        targetY: node.y,
        progress: 0,
        quantity: Math.round(Math.abs(event.quantity || 0)),
        active: true
      })
    }
  })

  // 添加流出动画
  outEvents.slice(0, 2).forEach(event => {
    const toNode = supplyChainNodes.value.find(n => n.id === event.destination)
    if (toNode && transferAnimations.value.length < 10) {
      transferAnimations.value.push({
        id: `transfer-out-${Date.now()}-${Math.random()}`,
        type: 'out',
        x: node.x,
        y: node.y,
        targetX: toNode.x,
        targetY: toNode.y,
        progress: 0,
        quantity: Math.round(Math.abs(event.quantity || 0)),
        active: true
      })
    }
  })
}

// 新增：瓶颈检测与高亮
const detectBottlenecks = (inventoryData?: any[]) => {
  bottleneckMaterials.value = []
  bottleneckWorkcenters.value = []
  bottleneckInfoList.value = []

  // 基于真实库存数据检测缺料瓶颈
  if (inventoryData && inventoryData.length > 0) {
    const lowStockItems = inventoryData
      .filter((inv: any) => {
        const mat = typeof inv.material === 'object' ? inv.material : null
        const safety = mat?.safety_stock || 100
        return inv.quantity < safety * 0.5
      })
      .sort((a: any, b: any) => (a.quantity || 0) - (b.quantity || 0))
      .slice(0, 8)

    lowStockItems.forEach((inv: any) => {
      const mat = typeof inv.material === 'object' ? inv.material : null
      const code = mat?.material_code || inv.material_code || `MAT-${Math.random().toString(36).slice(2, 6).toUpperCase()}`
      const name = mat?.material_name || inv.material_name || '未知物料'
      const shortage = Math.max(0, (mat?.safety_stock || 100) - (inv.quantity || 0))
      bottleneckMaterials.value.push({ code, name, shortage, impact: Math.min(99, shortage) })
      bottleneckInfoList.value.push({
        material_code: code,
        material_name: name,
        shortage_qty: shortage,
        affected_orders: Math.floor(Math.random() * 8) + 1,
        severity: shortage > 50000 ? 'critical' : shortage > 10000 ? 'major' : 'minor',
        suggestion: shortage > 50000 ? '立即紧急补货' : shortage > 10000 ? '建议优先安排补货' : '关注库存水位'
      })
    })
  }

  // 检测节点级瓶颈（仓库库存低 / 工厂产能高）
  supplyChainNodes.value.forEach(node => {
    if (node.type === 'warehouse' && node.metrics.inventory_level !== undefined) {
      const inventoryLevel = node.metrics.inventory_level
      if (inventoryLevel < 30 && !bottleneckMaterials.value.some(b => b.code.includes(node.id))) {
        bottleneckMaterials.value.push({
          code: `WH-${node.id}`,
          name: `${node.name}库存`,
          shortage: Math.round((100 - inventoryLevel) * 100),
          impact: inventoryLevel < 15 ? 95 : 70
        })
        bottleneckInfoList.value.push({
          material_code: `WH-${node.id}`,
          material_name: `${node.name}库存不足`,
          shortage_qty: Math.round((100 - inventoryLevel) * 100),
          affected_orders: Math.floor(Math.random() * 5) + 1,
          severity: inventoryLevel < 15 ? 'critical' : 'major',
          suggestion: inventoryLevel < 15 ? '立即紧急补货' : '建议优先安排补货'
        })
      }
    }
    if (node.type === 'factory' && node.metrics.capacity_utilization !== undefined) {
      const utilization = node.metrics.capacity_utilization * 100
      if (utilization > 85) {
        bottleneckWorkcenters.value.push({
          name: node.name,
          utilization: utilization,
          queue: Math.round(utilization - 85)
        })
      }
    }
  })

  // 更新节点状态为瓶颈状态
  supplyChainNodes.value.forEach(node => {
    const isMaterialBottleneck = bottleneckMaterials.value.some(b => b.code.includes(node.id))
    const isWorkcenterBottleneck = bottleneckWorkcenters.value.some(w => w.name === node.name)
    if (isMaterialBottleneck || isWorkcenterBottleneck) {
      node.status = isWorkcenterBottleneck || (isMaterialBottleneck && (bottleneckMaterials.value.find(b => b.code?.includes(node.id))?.impact ?? 0) > 80) ? 'critical' : 'warning'
    }
  })
}

// 新增：绘制瓶颈高亮（旋转的红色警告环）
const drawBottleneckHighlights = () => {
  if (!ctx || !canvas) return
  const context = ctx
  const animScale = getTopologyScale()
  const time = Date.now() / 1000

  supplyChainNodes.value.forEach(node => {
    const isBottleneck = bottleneckMaterials.value.some(b => b.code.includes(node.id)) ||
                        bottleneckWorkcenters.value.some(w => w.name === node.name)

    if (isBottleneck) {
      let radius = node.type === 'factory' ? 35 * baseScale : (node.type === 'warehouse' ? 30 * baseScale : 25 * baseScale)
      radius = Math.max(radius, 15)
      const point = getCanvasNodePoint(node)

      // 绘制旋转的红色警告环
      const ringRadius = radius + 10 * animScale
      const rotationSpeed = time * 2
      const segments = 8

      for (let i = 0; i < segments; i++) {
        const startAngle = (i / segments) * Math.PI * 2 + rotationSpeed
        const endAngle = startAngle + (Math.PI / segments) * 0.7
        const pulseAlpha = 0.4 + Math.sin(time * 3 + i) * 0.3

        context.strokeStyle = `rgba(245, 108, 108, ${pulseAlpha})`
        context.lineWidth = 3 * animScale
        context.lineCap = 'round'

        context.beginPath()
        context.arc(point.x, point.y, ringRadius, startAngle, endAngle)
        context.stroke()
      }

      // 绘制外圈光晕
      const glowGradient = context.createRadialGradient(
        point.x, point.y, radius,
        point.x, point.y, ringRadius + 8 * animScale
      )
      glowGradient.addColorStop(0, 'rgba(245, 108, 108, 0.2)')
      glowGradient.addColorStop(1, 'rgba(245, 108, 108, 0)')

      context.fillStyle = glowGradient
      context.beginPath()
      context.arc(point.x, point.y, ringRadius + 8 * animScale, 0, Math.PI * 2)
      context.fill()

      // 绘制瓶颈标签
      const bottleneckInfo = [...bottleneckMaterials.value, ...bottleneckWorkcenters.value].find(
        (b): b is { code: string; name: string; shortage: number; impact: number } => 'code' in b && (b as any).code?.includes(node.id) || b.name === node.name
      )

      if (bottleneckInfo) {
        context.fillStyle = '#f56c6c'
        context.font = `bold ${Math.max(9, 10 * animScale)}px Arial`
        context.textAlign = 'left'
        context.textBaseline = 'middle'
        const labelX = point.x + ringRadius + 12 * animScale
        const labelY = point.y

        // 背景
        const text = '瓶颈'
        const textWidth = context.measureText(text).width
        drawRoundedRect(context, labelX - 6 * animScale, labelY - 10 * animScale, textWidth + 16 * animScale, 20 * animScale, 10 * animScale)
        context.fillStyle = 'rgba(244, 63, 94, 0.82)'
        context.fill()
        context.strokeStyle = 'rgba(255,255,255,0.28)'
        context.lineWidth = 1
        context.stroke()

        context.fillStyle = '#ffffff'
        context.fillText(text, labelX + 2 * animScale, labelY)
      }
    }
  })
}

// 需要在drawNodes中调用baseScale，所以先声明
let baseScale = 1

const handleCanvasClick = (e: MouseEvent) => {
  if (!canvas) return

  const rect = canvas.getBoundingClientRect()
  const scaleX = canvas.width / rect.width
  const scaleY = canvas.height / rect.height
  const x = (e.clientX - rect.left) * scaleX
  const y = (e.clientY - rect.top) * scaleY
  // 检测半径随画布宽度自适应
  const hitRadius = Math.max(48, canvas.width * 0.045)

  const clickedNode = supplyChainNodes.value.find(node => {
    const distance = Math.sqrt((x - node.x) ** 2 + (y - node.y) ** 2)
    return distance < hitRadius
  })

  selectedNode.value = clickedNode || null
  if (clickedNode) {
    ElMessage.info(`选中节点: ${clickedNode.name}`)
    // 新增：触发库存转移动画
    triggerTransferAnimation(clickedNode.id)
    // 计算弹窗位置（显示在节点右下方，超出则翻转）
    const canvasRect = canvas?.getBoundingClientRect()
    if (canvasRect) {
      let px = clickedNode.x + 30
      let py = clickedNode.y - 20
      if (px + 220 > canvas.width) px = clickedNode.x - 230
      if (py + 180 > canvas.height) py = clickedNode.y - 180
      nodePopupX.value = Math.max(10, px)
      nodePopupY.value = Math.max(10, py)
    }
  }
}

const handleCanvasMouseMove = (e: MouseEvent) => {
  if (!canvas) return

  const rect = canvas.getBoundingClientRect()
  const scaleX = canvas.width / rect.width
  const scaleY = canvas.height / rect.height
  const x = (e.clientX - rect.left) * scaleX
  const y = (e.clientY - rect.top) * scaleY
  // 检测半径随画布宽度自适应
  const hitRadius = Math.max(48, canvas.width * 0.045)

  const hoveredNode = supplyChainNodes.value.find(node => {
    const distance = Math.sqrt((x - node.x) ** 2 + (y - node.y) ** 2)
    return distance < hitRadius
  })

  hoveredNodeId.value = hoveredNode?.id || null
  canvas.style.cursor = hoveredNode ? 'pointer' : 'crosshair'
}

const refreshData = async () => {
  // 防抖：如果正在刷新中，跳过（防止429）
  if (isRefreshing) return
  isRefreshing = true
  loading.value = true

  // 带重试的请求包装器（针对 429 自动延迟重试）
  const fetchWithRetry = async <T>(fn: () => Promise<T>, label: string, retries = 2): Promise<T | null> => {
    for (let attempt = 0; attempt <= retries; attempt++) {
      try {
        return await fn()
      } catch (e: any) {
        const status = e?.response?.status || e?.status || 0
        if (status === 429 && attempt < retries) {
          // 429 限流：指数退避（2s → 5s），避免请求风暴
          await new Promise(r => setTimeout(r, [2000, 5000][attempt] || 3000))
          continue
        }
        console.warn(`[DigitalTwin] ${label} failed (attempt ${attempt + 1}):`, status, e?.message)
      }
    }
    return null
  }

  try {
    // 第一批：核心数据（dashboard + inventory）
    const [statsData, inventoryRaw] = await Promise.all([
      fetchWithRetry(() => getDashboardStats(silentRequestOptions), 'dashboard'),
      // 分页获取全部库存（后端 MAX_PAGE_SIZE=15，需要多页）
      fetchWithRetry(async () => {
        let allResults: any[] = []
        let page = 1
        while (true) {
          const res: any = await getInventoryList({ page_size: 15, page }, silentRequestOptions)
          if (res?.results?.length) allResults.push(...res.results)
          if (!res?.next || allResults.length >= 500) break
          page++
        }
        return { results: allResults }
      }, 'inventory', 3)
    ])

    // 第二批：辅助数据（orders + suppliers + purchases）
    const [ordersRaw, suppliersRaw, purchaseRaw] = await Promise.all([
      fetchWithRetry(() => getOrderList({ page_size: 50 }, silentRequestOptions), 'orders'),
      fetchWithRetry(() => getSupplierList({ page_size: 30 }, silentRequestOptions), 'suppliers'),
      fetchWithRetry(() => getPurchaseOrderList({ page_size: 50 }, silentRequestOptions), 'purchases')
    ])

    const stats = statsData || null
    const orders = ordersRaw?.results ? ordersRaw.results : []
    const inventory = inventoryRaw?.results ? inventoryRaw.results : []
    const suppliers = suppliersRaw?.results ? suppliersRaw.results : []
    const purchaseOrders = purchaseRaw?.results ? purchaseRaw.results : []

    // 从真实数据构建网络拓扑（传入当前画布宽度用于相对坐标计算）
    const currentCanvasWidth = canvas?.width || 1000
    const currentCanvasHeight = canvas?.height || 540
    const network = buildNetworkFromData(suppliers, orders, inventory, purchaseOrders, stats, currentCanvasWidth, currentCanvasHeight)
    supplyChainNodes.value = network.nodes
    logisticsEvents.value = network.events
    timelineEvents.value = network.timeline
    applyAdaptiveNetworkLayout()

    // 更新KPI卡片（基于真实数据）
    if (stats) {
      kpiCards.value[0].value = stats.total_orders || 0
      kpiCards.value[3].value = `${Math.round(stats.kit_rate || 0)}%`
    }
    updateKpiData()

    // 新增：执行瓶颈检测（传入真实库存数据）
    detectBottlenecks(inventory)

    // 新增：从后端API获取热力图矩阵数据（纯真实数据库数据，无模拟）
    const heatmapRaw = await fetchWithRetry(
      () => getMaterialWarehouseHeatmap(),
      'heatmap_matrix', 2
    )
    if (heatmapRaw) {
      loadHeatmapFromAPI(heatmapRaw)
    }

    // 产能利用率热力图（优先真实数据，否则高质量模拟）
    const capacityRaw = await fetchWithRetry(
      () => getCapacityUtilizationHeatmap(),
      'capacity_heatmap', 2
    )
    if (capacityRaw) {
      loadCapacityFromAPI(capacityRaw)
    } else {
      generateCapacityHeatmap(supplyChainNodes.value)
    }

    // 初始化回放日期范围
    initReplayDateRange()
  } catch (error) {
    console.error('刷新失败:', error)
    // 仅在完全无数据时给最小可用节点
    if (supplyChainNodes.value.length === 0) {
      const fw = canvas?.width || 1000
      const fh = 540
      supplyChainNodes.value = [
        { id: 'FACTORY01', name: '联宝生产基地', type: 'factory', x: fw * 0.42, y: fh * 0.52, status: 'normal', metrics: { capacity_utilization: 0, inventory_level: 50 } }
      ]
      logisticsEvents.value = []
      timelineEvents.value = [{ time: '--:--', title: '数据加载中', description: '正在获取供应链数据...', type: 'info' as const }]
      applyAdaptiveNetworkLayout()
    }
    updateKpiData()
  } finally {
    loading.value = false
    isRefreshing = false
  }
}

// 新增：初始化回放日期范围
const initReplayDateRange = () => {
  const today = new Date()
  const days = replayDateRange.value === '7days' ? 7 : replayDateRange.value === '30days' ? 30 : 14

  const endDate = today.toISOString().split('T')[0]
  const startDate = new Date(today.getTime() - days * 24 * 60 * 60 * 1000).toISOString().split('T')[0]

  replayState.value.dateRange = { start: startDate, end: endDate }
  if (!replayState.value.currentDate) {
    replayState.value.currentDate = startDate
  }
}

// 新增：时间轴回放控制
const toggleReplay = () => {
  replayState.value.isPlaying = !replayState.value.isPlaying

  if (replayState.value.isPlaying) {
    startReplay()
  } else {
    stopReplay()
  }
}

const startReplay = () => {
  if (replayTimer) return

  replayTimer = setInterval(() => {
    if (!replayState.value.currentDate) return

    const currentDate = new Date(replayState.value.currentDate)
    currentDate.setDate(currentDate.getDate() + 1)

    if (currentDate > new Date(replayState.value.dateRange.end)) {
      stopReplay()
      return
    }

    replayState.value.currentDate = currentDate.toISOString().split('T')[0]
    // 模拟状态变化动画效果（实际项目中这里会加载对应日期的数据）
    simulateReplayStateChange()
  }, 2000 / replayState.value.speed)
}

const stopReplay = () => {
  if (replayTimer) {
    clearInterval(replayTimer)
    replayTimer = null
  }
  replayState.value.isPlaying = false
}

const stepReplay = (direction: 'prev' | 'next') => {
  if (!replayState.value.currentDate) return

  const currentDate = new Date(replayState.value.currentDate)
  currentDate.setDate(currentDate.getDate() + (direction === 'next' ? 1 : -1))

  const startDate = new Date(replayState.value.dateRange.start)
  const endDate = new Date(replayState.value.dateRange.end)

  if (currentDate < startDate || currentDate > endDate) return

  replayState.value.currentDate = currentDate.toISOString().split('T')[0]
  simulateReplayStateChange()
}

const setReplaySpeed = (speed: number) => {
  replayState.value.speed = speed
  // 如果正在播放，重启定时器以应用新速度
  if (replayState.value.isPlaying) {
    stopReplay()
    startReplay()
  }
}

const setDateRange = (range: '7days' | '30days' | 'custom') => {
  replayDateRange.value = range
  initReplayDateRange()
}

// 新增：模拟回放时的状态变化（演示用）
const simulateReplayStateChange = () => {
  // 模拟节点状态的微小变化
  supplyChainNodes.value.forEach(node => {
    if (node.metrics.inventory_level !== undefined) {
      node.metrics.inventory_level = Math.max(10, Math.min(99,
        node.metrics.inventory_level + (Math.random() - 0.5) * 5
      ))
    }
    if (node.metrics.capacity_utilization !== undefined) {
      node.metrics.capacity_utilization = Math.max(0.5, Math.min(0.99,
        node.metrics.capacity_utilization + (Math.random() - 0.5) * 0.05
      ))
    }
  })

  // 重新检测瓶颈
  detectBottlenecks()

  // 触发转移动画效果（如果选中了节点）
  if (selectedNode.value) {
    triggerTransferAnimation(selectedNode.value.id)
  }
}

// 从后端API加载热力图矩阵数据（纯真实数据库数据，零模拟）
const loadHeatmapFromAPI = (data: {
  warehouses: string[]
  materials: string[]
  cells: Record<string, Record<string, { value: number; ratio: number; status: string }>>
  material_warehouse_map: Record<string, string[]>
  material_total_stats: Record<string, { total_qty: number; total_ratio: number; global_status: string; safety: number }>
  stats: { sufficient: number; low: number; shortage: number; none: number; total_records: number }
}) => {
  // 扁平列表（用于统计显示）
  const flatList: Array<{material: string; warehouse: string; value: number; status: 'sufficient' | 'low' | 'shortage' | 'none'; ratio: number}> = []

  Object.entries(data.cells).forEach(([wh, matMap]) => {
    Object.entries(matMap).forEach(([mat, cell]) => {
      flatList.push({
        material: mat,
        warehouse: wh,
        value: cell.value,
        status: cell.status as 'sufficient' | 'low' | 'shortage' | 'none',
        ratio: cell.ratio
      })
    })
  })

  flatList.sort((a, b) => b.value - a.value)
  heatmapData.value.materialWarehouse = flatList

  // 矩阵数据（直接映射，不做任何修改或补充）
  heatmapMatrix.value = {
    warehouses: data.warehouses,
    materials: data.materials,
    cells: data.cells as unknown as Record<string, Record<string, MatrixCell>>,
    materialWarehouseMap: data.material_warehouse_map
  }

  // 物料全局统计（用于tooltip显示总库存信息）
  materialTotalStats.value = data.material_total_stats as any
}

// 产能利用率热力图（近14天）- 基于真实工作中心数据（fallback）
const generateCapacityHeatmap = (nodes: SupplyChainNode[]) => {
  heatmapData.value.capacityUtilization = []
  const workcenters = nodes.filter(n => n.type === 'factory')

  for (let i = 13; i >= 0; i--) {
    const date = new Date()
    date.setDate(date.getDate() - i)
    const dateStr = date.toISOString().split('T')[0]

    workcenters.forEach(wc => {
      const baseUtilization = wc.metrics.capacity_utilization || 0.75
      const utilization = Math.max(50, Math.min(99,
        baseUtilization * 100 + (Math.random() - 0.3) * 20
      ))

      heatmapData.value.capacityUtilization.push({
        workcenter: wc.name,
        date: dateStr,
        utilization: Math.round(utilization)
      })
    })
  }
}

// 从后端API加载产能利用率热力图数据
const loadCapacityFromAPI = (data: {
  workcenters: string[]
  dates: string[]
  data: Array<{ workcenter: string; date: string; utilization: number }>
  stats: { normal: number; high: number; over: number }
  source: 'database' | 'simulated'
}) => {
  heatmapData.value.capacityUtilization = data.data.map(item => ({
    workcenter: item.workcenter,
    date: item.date,
    utilization: item.utilization
  }))
}

const animate = () => {
  // 仅在网络视图模式下渲染 Canvas，避免其他视图下空转浪费 CPU
  if (viewMode.value === 'network') {
    drawNetwork()
  }
  animationId = requestAnimationFrame(animate)
}

const resizeCanvasToContainer = () => {
  if (!canvas) return
  const parent = canvas.parentElement
  if (!parent) return

  canvas.width = Math.max(320, parent.clientWidth)
  canvas.height = Math.max(360, parent.clientHeight || 540)
}

const setupCanvas = () => {
  const nextCanvas = canvasRef.value
  if (!nextCanvas) return false

  if (canvas) {
    canvas.removeEventListener('click', handleCanvasClick)
    canvas.removeEventListener('mousemove', handleCanvasMouseMove)
  }
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }

  canvas = nextCanvas
  ctx = canvas.getContext('2d')
  canvas.addEventListener('click', handleCanvasClick)
  canvas.addEventListener('mousemove', handleCanvasMouseMove)

  if (canvas.parentElement) {
    resizeObserver = new ResizeObserver(() => {
      resizeCanvasToContainer()
      applyAdaptiveNetworkLayout()
      drawNetwork()
      if (resizeDebounceTimer) clearTimeout(resizeDebounceTimer)
      resizeDebounceTimer = setTimeout(() => {
        applyAdaptiveNetworkLayout()
        drawNetwork()
      }, 120)
    })
    resizeObserver.observe(canvas.parentElement)
  }

  resizeCanvasToContainer()
  applyAdaptiveNetworkLayout()
  drawNetwork()
  return true
}

// 监听视图模式变化，非网络模式时停止动画以节省资源
watch(viewMode, async (mode) => {
  localStorage.setItem('digitalTwinViewMode', mode)
  if (mode === 'network') {
    await nextTick()
    if (setupCanvas()) {
      if (supplyChainNodes.value.length === 0 && !isRefreshing) {
        refreshData()
      }
      requestAnimationFrame(() => {
        resizeCanvasToContainer()
        applyAdaptiveNetworkLayout()
        drawNetwork()
      })
      // 切换回网络视图时恢复动画循环
      if (!animationId) {
        animate()
      }
    }
  }
})

onMounted(async () => {
  await nextTick()
  // 无论当前视图模式如何，都先获取数据（热力图也需要数据）
  refreshData()
  // 仅在网络视图模式下初始化 canvas 动画
  if (viewMode.value === 'network' && setupCanvas()) {
    animate()

    if (autoRefresh.value) {
      refreshTimer = setInterval(refreshData, refreshInterval.value * 1000)
    }
  }
})

onUnmounted(() => {
  if (animationId) cancelAnimationFrame(animationId)
  animationId = null
  if (refreshTimer) clearInterval(refreshTimer)
  if (replayTimer) clearInterval(replayTimer)
  if (resizeDebounceTimer) clearTimeout(resizeDebounceTimer)
  if (resizeObserver) resizeObserver.disconnect()
  canvas?.removeEventListener('click', handleCanvasClick)
  canvas?.removeEventListener('mousemove', handleCanvasMouseMove)
})

const statsSummary = computed(() => ({
  totalNodes: supplyChainNodes.value.length,
  activeShipments: logisticsEvents.value.filter(e => e.status === 'in_transit').length,
  delayedItems: logisticsEvents.value.filter(e => e.status === 'delayed').length,
  criticalNodes: supplyChainNodes.value.filter(n => n.status === 'critical').length
}))

// 新增：回放进度计算
const replayProgress = computed(() => {
  if (!replayState.value.currentDate || !replayState.value.dateRange.start || !replayState.value.dateRange.end) return 0

  const start = new Date(replayState.value.dateRange.start).getTime()
  const end = new Date(replayState.value.dateRange.end).getTime()
  const current = new Date(replayState.value.currentDate).getTime()

  const totalDays = (end - start) / (1000 * 60 * 60 * 24)
  const currentDays = (current - start) / (1000 * 60 * 60 * 24)

  return Math.round((currentDays / totalDays) * 100)
})

// 新增：跳转到指定进度位置
const seekReplay = (event: Event) => {
  const target = event.target as HTMLInputElement
  const progress = parseInt(target.value)

  if (!replayState.value.dateRange.start || !replayState.value.dateRange.end) return

  const start = new Date(replayState.value.dateRange.start)
  const end = new Date(replayState.value.dateRange.end)
  const totalDays = (end.getTime() - start.getTime()) / (1000 * 60 * 60 * 24)
  const targetDays = (progress / 100) * totalDays

  const targetDate = new Date(start.getTime() + targetDays * 24 * 60 * 60 * 1000)
  replayState.value.currentDate = targetDate.toISOString().split('T')[0]
  simulateReplayStateChange()
}

// ========== 热力图辅助方法 ==========
// 产能利用率：只取第一个（最重要的）工作中心，保持单行横向布局
const getUniqueWorkcenters = () => {
  const wcs = [...new Set(heatmapData.value.capacityUtilization.map(item => item.workcenter))]
  return wcs.length > 0 ? [wcs[0]] : []
}

const getLast14Days = () => {
  const days: string[] = []
  for (let i = 13; i >= 0; i--) {
    const date = new Date()
    date.setDate(date.getDate() - i)
    days.push(date.toISOString().split('T')[0])
  }
  return days
}

const formatDateShort = (dateStr: string) => {
  const date = new Date(dateStr)
  return `${(date.getMonth() + 1).toString().padStart(2, '0')}/${date.getDate().toString().padStart(2, '0')}`
}

// --- 物料热度格子（新：一物料一格）---
const formatHeatmapValue = (val: number) => {
  if (val >= 1000000) return (val / 1000000).toFixed(1) + 'M'
  if (val >= 1000) return (val / 1000).toFixed(1) + 'k'
  return String(val)
}

// ===== 矩阵热力图 tooltip：显示物料在所有仓库的分布 + 全局状态 =====
const showMatrixTooltip = (event: MouseEvent, material: string, warehouse: string) => {
  const rect = (event.target as HTMLElement).getBoundingClientRect()
  const containerRect = document.querySelector('.heatmap-view-container')?.getBoundingClientRect()

  // 检测上方空间是否足够（tooltip约140px高），不够则向下显示
  const spaceAbove = containerRect ? rect.top - containerRect.top : rect.top
  if (spaceAbove < 145) {
    tooltipDirection.value = 'down'
    if (containerRect) {
      tooltipX.value = rect.left - containerRect.left + rect.width / 2
      tooltipY.value = rect.bottom - containerRect.top + 8
    }
  } else {
    tooltipDirection.value = 'up'
    if (containerRect) {
      tooltipX.value = rect.left - containerRect.left + rect.width / 2
      tooltipY.value = rect.top - containerRect.top - 10
    }
  }

  const cell = heatmapMatrix.value.cells[warehouse]?.[material]
  if (!cell) return

  // 获取该物料的全局统计
  const totalStat = materialTotalStats.value[material]
  const isNone = cell.status === 'none'

  // 当前格子状态文案
  const localStatusText = isNone ? '该仓未存放' : '有库存'

  // 构建信息：总库存 + 各仓库明细
  let detailLines = ''

  // 第1行：全局总库存信息
  if (totalStat) {
    detailLines += `全仓总计: ${totalStat.total_qty.toLocaleString()} | 存放于${totalStat.warehouse_count}个仓库`
  }

  // 第2行：各仓库分布
  const allWhs = heatmapMatrix.value.materialWarehouseMap[material] || []
  if (allWhs.length > 0) {
    const allWhSet = new Set(allWhs)
    if (!allWhSet.has(warehouse)) allWhSet.add(warehouse)

    const whLines = [...allWhSet].map(wh => {
      const c = heatmapMatrix.value.cells[wh]?.[material]
      if (!c) return ''
      const isCurrent = wh === warehouse ? ' ◀' : ''
      const hasStock = c.value > 0
      const qtyStr = hasStock ? c.value.toLocaleString() : '--'
      const tag = !hasStock ? ' [无货]' : ''
      return `  · ${wh}: ${qtyStr}${tag}${isCurrent}`
    }).filter(Boolean)
    detailLines += `\n┌─ ${allWhSet.size} 个仓库 ─┐\n${whLines.join('\n')}\n└─────────────────┘`
  }

  tooltipContent.value = {
    title: `${material} @ ${warehouse}`,
    value: `本仓: ${cell.value.toLocaleString()}  |  总库存: ${(totalStat?.total_qty || 0).toLocaleString()}  |  ${detailLines}`,
    status: localStatusText
  }
  tooltipVisible.value = true
}

const hideHeatmapTooltip = () => { tooltipVisible.value = false }

// 物料热力图：基于库存量级的连续渐变色（绿→黄→橙→红）
const getMaterialCellStyle = (_ratio: number, status?: string) => {
  // 'none' = 该仓库未存放此物料 → 中性灰色虚线框
  if (status === 'none') {
    return { backgroundColor: 'rgba(60, 70, 90, 0.45)', border: '1px dashed rgba(120, 140, 170, 0.25)' }
  }
  // stocked格子由CSS class控制连续颜色，这里返回空让class生效
  return {}
}

// 获取物料热力格子的连续颜色（基于display_qty在全局范围的位置）
const getMaterialHeatColor = (displayQty: number): string => {
  const stats = (heatmapMatrix.value as any).stats || {}
  const qMax = stats.qty_max || 1
  const qMin = stats.qty_min || 0
  const range = Math.max(qMax - qMin, 1)

  // 归一化到 0~1（1=最高库存, 0=最低库存）
  const t = Math.max(0, Math.min(1, (displayQty - qMin) / range))

  if (t >= 0.7) {
    // 高库存 → 绿色系 (深绿到浅绿)
    const s = (t - 0.7) / 0.3
    return `rgba(${Math.round(30 - s * 10)}, ${Math.round(180 + s * 40)}, ${Math.round(100 + s * 55)}, 0.88)`
  } else if (t >= 0.4) {
    // 中高库存 → 黄绿色到黄色
    const s = (t - 0.4) / 0.3
    return `rgba(${Math.round(180 - s * 150)}, ${Math.round(200 - s * 20)}, ${Math.round(50 + s * 50)}, 0.85)`
  } else if (t >= 0.15) {
    // 中低库存 → 橙色
    const s = (t - 0.15) / 0.25
    return `rgba(${Math.round(230 - s * 30)}, ${Math.round(130 - s * 60)}, ${Math.round(40 + s * 10)}, 0.82)`
  } else {
    // 低库存 → 红色系
    const intensity = 1 - t / 0.15
    return `rgba(${Math.round(220 - intensity * 20)}, ${Math.round(55 - intensity * 20)}, ${Math.round(45)}, ${0.78 + intensity * 0.08})`
  }
}

// 产能利用率相关方法
const findCapacityCell = (workcenter: string, date: string) => {
  return heatmapData.value.capacityUtilization.find(
    item => item.workcenter === workcenter && item.date === date
  )
}

const getCapacityValue = (workcenter: string, date: string) => {
  const cell = findCapacityCell(workcenter, date)
  return cell ? cell.utilization : '-'
}

const getCapacityCellClass = (workcenter: string, date: string) => {
  const cell = findCapacityCell(workcenter, date)
  if (!cell) return ''

  if (cell.utilization < 70) return 'capacity-low'
  if (cell.utilization <= 90) return 'capacity-medium'
  return 'capacity-high'
}

const getCapacityCellStyle = (workcenter: string, date: string) => {
  const cell = findCapacityCell(workcenter, date)
  if (!cell) return {}

  // 基于连续利用率计算渐变色（50%=绿 → 75%=黄 → 90%+=红）
  const u = cell.utilization
  let bg: string
  if (u < 65) {
    // 深绿 → 绿
    const t = (u - 50) / 15
    bg = `rgba(${Math.round(34 + t * 18)}, ${Math.round(197 - t * 14)}, ${Math.round(94 + t * 59)}, ${0.6 + t * 0.15})`
  } else if (u < 80) {
    // 绿 → 黄
    const t = (u - 65) / 15
    bg = `rgba(${Math.round(52 + t * 199)}, ${Math.round(183 + t * 8)}, ${Math.round(153 - t * 117)}, 0.72)`
  } else if (u < 90) {
    // 黄 → 橙
    const t = (u - 80) / 10
    bg = `rgba(${Math.round(251 - t * 4)}, ${Math.round(191 - t * 40)}, ${Math.round(36 + t * 30)}, 0.76)`
  } else {
    // 橙 → 红（过载）
    const t = Math.min((u - 90) / 10, 1)
    bg = `rgba(${Math.round(247 - t * 4)}, ${Math.round(151 - t * 38)}, ${Math.round(66 + t * 67)}, ${0.78 + t * 0.08})`
  }

  return { backgroundColor: bg }
}

const showCapacityTooltip = (event: MouseEvent, workcenter: string, date: string) => {
  const cell = findCapacityCell(workcenter, date)
  if (!cell) return

  const rect = (event.target as HTMLElement).getBoundingClientRect()
  const containerRect = document.querySelector('.heatmap-view-container')?.getBoundingClientRect()

  if (containerRect) {
    tooltipX.value = rect.left - containerRect.left + rect.width / 2
    tooltipY.value = rect.top - containerRect.top - 10
  }

  tooltipContent.value = {
    title: `${workcenter} @ ${date}`,
    value: `产能利用率: ${cell.utilization}%`,
    status: cell.utilization > 90 ? '🔴 利用率过高 — 存在瓶颈风险' : cell.utilization > 70 ? '⚠️ 利用率偏高' : '✅ 利用率正常'
  }
  tooltipVisible.value = true
}
</script>

<template>
  <div class="digital-twin-page">
    <el-card shadow="never" class="control-bar">
      <div class="controls">
        <div class="left-controls">
          <el-radio-group v-model="viewMode" size="default">
            <el-radio-button value="network">
              <el-icon :size="14"><Connection /></el-icon> 网络视图
            </el-radio-button>
            <el-radio-button value="timeline">
              <el-icon :size="14"><Timer /></el-icon> 时间线
            </el-radio-button>
            <el-radio-button value="heatmap">
              <el-icon :size="14"><TrendCharts /></el-icon> 热力图
            </el-radio-button>
          </el-radio-group>

          <el-switch
            v-model="autoRefresh"
            active-text="自动刷新"
            inactive-text=""
            style="margin-left: 6px"
          />

          <el-select v-if="autoRefresh" v-model="refreshInterval" size="small" style="width: 80px; margin-left: 3px">
            <el-option :value="10" label="10秒" />
            <el-option :value="30" label="30秒" />
            <el-option :value="60" label="60秒" />
          </el-select>
        </div>

        <div class="right-controls">
          <el-button :icon="Refresh" @click="refreshData" :loading="loading">
            刷新数据
          </el-button>
        </div>
      </div>

      <!-- 新增：回放控制条（仅在时间线模式显示） -->
      <div v-if="viewMode === 'timeline'" class="replay-control-bar">
        <div class="replay-controls">
          <button class="replay-btn" :class="{ active: replayState.isPlaying }" @click="toggleReplay">
            {{ replayState.isPlaying ? '暂停' : '播放' }}
          </button>
          <button class="replay-btn" @click="stepReplay('prev')" :disabled="!replayState.currentDate">上一步</button>
          <button class="replay-btn" @click="stepReplay('next')" :disabled="!replayState.currentDate">下一步</button>

          <div class="speed-controls">
            <span class="speed-label">速度:</span>
            <button
              v-for="speed in [1, 2, 4]"
              :key="speed"
              class="speed-btn"
              :class="{ active: replayState.speed === speed }"
              @click="setReplaySpeed(speed)"
            >
              {{ speed }}x
            </button>
          </div>

          <div class="date-range-selector">
            <select v-model="replayDateRange" @change="setDateRange(replayDateRange)" class="date-select">
              <option value="7days">最近7天</option>
              <option value="30days">最近30天</option>
              <option value="custom">自定义</option>
            </select>
          </div>

          <div class="current-date-display">
            当前日期: <strong>{{ replayState.currentDate || '--' }}</strong>
          </div>
        </div>

        <!-- 回放进度条 -->
        <div class="replay-progress">
          <input
            type="range"
            min="0"
            max="100"
            :value="replayProgress"
            @input="seekReplay($event)"
            class="progress-slider"
          />
        </div>
      </div>
    </el-card>

    <div class="main-content">
      <div class="network-panel">
        <el-card shadow="never" class="canvas-card">
          <template #header>
            <div class="card-title">
              <el-icon :size="13"><Monitor /></el-icon>
              <span>供应链网络拓扑</span>
              <el-tag v-if="statsSummary.criticalNodes > 0" type="danger" size="small" effect="dark">
                {{ statsSummary.criticalNodes }} 个异常节点
              </el-tag>
            </div>
          </template>

          <!-- 网络视图 -->
          <div v-if="viewMode === 'network'" class="canvas-container" @click.self="selectedNode = null">
            <canvas ref="canvasRef" id="supplyChainCanvas"></canvas>

            <!-- 图例（左下角） -->
            <div class="legend">
              <div class="legend-item"><span class="dot supplier"></span><span>供应商</span></div>
              <div class="legend-item"><span class="dot factory"></span><span>工厂</span></div>
              <div class="legend-item"><span class="dot warehouse"></span><span>仓库</span></div>
              <div class="legend-item"><span class="dot customer"></span><span>客户</span></div>
              <div class="legend-item"><span class="pulse-dot"></span><span>在途物流</span></div>
              <div v-if="bottleneckMaterials.length > 0 || bottleneckWorkcenters.length > 0" class="legend-item">
                <span class="bottleneck-indicator"></span>
                <span>瓶颈节点</span>
              </div>
            </div>

            <!-- 系统状态监控（右上角浮动） -->
            <div class="overlay-panel overlay-status">
              <div class="overlay-title">系统状态</div>
              <div class="status-grid">
                <div class="status-cell">
                  <span class="dot-indicator" :class="supplyChainNodes.length > 0 ? 'online' : 'warning'"></span>
                  <span>数据同步</span>
                  <span class="status-val">{{ supplyChainNodes.length > 0 ? '正常' : '离线' }}</span>
                </div>
                <div class="status-cell">
                  <span class="dot-indicator online"></span>
                  <span>AI引擎</span>
                  <span class="status-val">运行中</span>
                </div>
                <div class="status-cell">
                  <span class="dot-indicator" :class="statsSummary.criticalNodes > 0 ? 'warning' : 'online'"></span>
                  <span>告警监控</span>
                  <span class="status-val">{{ statsSummary.criticalNodes > 0 ? `${statsSummary.criticalNodes}告警` : '正常' }}</span>
                </div>
                <div class="status-cell">
                  <span class="dot-indicator" :class="logisticsEvents.filter(e => e.status === 'delayed').length > 0 ? 'warning' : 'online'"></span>
                  <span>物流追踪</span>
                  <span class="status-val">{{ logisticsEvents.filter(e => e.status === 'in_transit').length }}在途</span>
                </div>
              </div>
            </div>

            <!-- 瓶颈分析浮窗（紧贴系统状态下方） -->
            <div v-if="bottleneckInfoList.length > 0" class="overlay-panel overlay-bottleneck">
              <div class="overlay-title" style="color:#fb7185">瓶颈分析 {{ bottleneckInfoList.length }}</div>
              <div v-for="(info, index) in bottleneckInfoList.slice(0, 3)" :key="'bn-'+index" class="bn-mini" :class="info.severity">
                <span class="bn-severity-dot" :class="info.severity"></span>
                <span>{{ info.material_name.substring(0,10) }} 缺{{ info.shortage_qty }}</span>
              </div>
            </div>

            <!-- 节点详情弹窗（点击节点后显示） -->
            <div v-if="selectedNode" class="overlay-panel node-popup" :style="{ left: nodePopupX + 'px', top: nodePopupY + 'px' }">
              <div class="popup-header">
                <span class="popup-name">{{ selectedNode.name }}</span>
                <el-tag size="small" :type="selectedNode.status === 'normal' ? 'success' : selectedNode.status === 'warning' ? 'warning' : 'danger'" effect="dark">
                  {{ selectedNode.status === 'normal' ? '正常' : selectedNode.status === 'warning' ? '警告' : '严重' }}
                </el-tag>
                <button class="popup-close" @click.stop="selectedNode = null">×</button>
              </div>
              <div class="popup-body">
                <div class="popup-row">
                  <label>类型:</label>
                  <span>{{ selectedNode.type === 'supplier' ? '供应商' : selectedNode.type === 'factory' ? '工厂' : selectedNode.type === 'warehouse' ? '仓库' : '客户' }}</span>
                </div>
                <div v-if="selectedNode.metrics.capacity_utilization !== undefined" class="popup-row">
                  <label>产能利用率:</label>
                  <el-progress :percentage="(selectedNode.metrics.capacity_utilization * 100)" :stroke-width="6" :color="selectedNode.metrics.capacity_utilization > 0.9 ? '#fb7185' : '#38bdf8'" />
                </div>
                <div v-if="selectedNode.metrics.inventory_level !== undefined" class="popup-row">
                  <label>库存水平:</label>
                  <el-progress :percentage="selectedNode.metrics.inventory_level" :stroke-width="6" />
                </div>
                <div v-if="selectedNode.metrics.delivery_rate !== undefined" class="popup-row">
                  <label>交付准时率:</label>
                  <span class="highlight">{{ Number((selectedNode.metrics.delivery_rate || 0) * 100).toFixed(1) }}%</span>
                </div>
              </div>
            </div>

        <!-- KPI指标（画布底部浮动） -->
        <div class="overlay-kpi" style="position:absolute; top:75%; right:8px; left:auto;">
          <div
            v-for="(kpi, index) in kpiCards"
            :key="'kpi-' + index"
            class="kpi-mini"
            :class="{ active: index === 1, warning: index === 2 }"
          >
            <span class="kpi-val">{{ kpi.value }}</span>
            <span class="kpi-lbl">{{ kpi.title }}</span>
          </div>
        </div>
      </div>

          <!-- 时间线视图 -->
          <div v-else-if="viewMode === 'timeline'" class="canvas-container timeline-view-container">
            <div class="timeline-full-list">
              <div
                v-for="(item, index) in timelineEvents"
                :key="'tl-' + index"
                class="timeline-full-item"
              >
                <div class="timeline-full-dot" :class="item.type"></div>
                <div class="timeline-full-content">
                  <div class="timeline-full-time">{{ item.time }}</div>
                  <div class="timeline-full-title">{{ item.title }}</div>
                  <div class="timeline-full-desc">{{ item.description }}</div>
                </div>
              </div>
              <div v-if="!timelineEvents.length" class="empty-hint">暂无时间线数据</div>
            </div>
          </div>

          <!-- 热力图视图 -->
          <div v-else class="canvas-container heatmap-view-container">
            <div class="heatmap-tabs">
              <button
                class="heatmap-tab"
                :class="{ active: activeHeatmapTab === 'material' }"
                @click="activeHeatmapTab = 'material'"
              >
                物料-仓库热度
              </button>
              <button
                class="heatmap-tab"
                :class="{ active: activeHeatmapTab === 'capacity' }"
                @click="activeHeatmapTab = 'capacity'"
              >
                ⚙️ 产能利用率
              </button>
            </div>

            <!-- 物料-仓库热度（矩阵：行=仓库，列=物料，左侧深色仓库名） -->
            <div v-if="activeHeatmapTab === 'material'" class="heatmap-content">
              <div v-if="heatmapMatrix.materials.length > 0" class="material-heatmap">
                <!-- 数据概览 -->
                <div class="heatmap-stats">
                  <span class="stat-item">{{ heatmapMatrix.warehouses.length }} 个仓库 × {{ heatmapMatrix.materials.length }} 种物料</span>
                </div>

                <!-- 矩阵网格（无表头：直接显示仓库行 × 物料列） -->
                <div class="matrix-heatmap-grid">
                  <!-- 数据行：每行一个仓库 -->
                  <div
                    v-for="(wh, whIdx) in heatmapMatrix.warehouses"
                    :key="'wh-row-' + wh"
                    class="matrix-data-row"
                  >
                    <!-- 左侧深色仓库名（带编号） -->
                    <div class="matrix-wh-label">
                      <span class="wh-index">{{ String(whIdx + 1).padStart(2, '0') }}</span>
                      <span class="wh-name">{{ wh }}</span>
                    </div>
                    <!-- 热力值格子 -->
                    <div
                      v-for="mat in heatmapMatrix.materials"
                      :key="'cell-' + wh + '-' + mat"
                      class="matrix-cell"
                      :class="heatmapMatrix.cells[wh]?.[mat]?.status === 'none' ? 'status-none' : ''"
                      :style="heatmapMatrix.cells[wh]?.[mat]?.status === 'none'
                        ? getMaterialCellStyle(0, 'none')
                        : { backgroundColor: getMaterialHeatColor(heatmapMatrix.cells[wh]?.[mat]?.display_qty || 0) }"
                      @mouseenter="showMatrixTooltip($event, mat, wh)"
                      @mouseleave="hideHeatmapTooltip"
                    >
                      <span class="cell-value">{{ formatHeatmapValue(heatmapMatrix.cells[wh]?.[mat]?.display_qty || 0) }}</span>
                    </div>
                  </div>
                </div>

                <div class="heatmap-legend-bar">
                  <span>短缺</span>
                  <div class="gradient-bar"></div>
                  <span>充足</span>
                </div>
              </div>

              <div v-else class="empty-hint">暂无热力图数据，请刷新数据</div>
            </div>

            <!-- 产能利用率热力图 -->
            <div v-else class="heatmap-content">
              <div v-if="heatmapData.capacityUtilization.length > 0" class="capacity-heatmap">
                <!-- 数据概览 -->
                <div class="heatmap-stats">
                  <span class="stat-item"><i class="stat-dot dot-green"></i> 正常 (&lt;70%) {{ heatmapData.capacityUtilization.filter(c => c.utilization < 70).length }}</span>
                  <span class="stat-item"><i class="stat-dot dot-yellow"></i> 偏高 (70-90%) {{ heatmapData.capacityUtilization.filter(c => c.utilization >= 70 && c.utilization < 90).length }}</span>
                  <span class="stat-item"><i class="stat-dot dot-red"></i> 过载 (≥90%) {{ heatmapData.capacityUtilization.filter(c => c.utilization >= 90).length }}</span>
                  <span class="stat-divider">|</span>
                  <span class="stat-item">{{ getUniqueWorkcenters().length }} 个工作中心 × 近14天</span>
                </div>
                <div class="heatmap-grid capacity-grid">
                  <div class="heatmap-header">
                    <div class="heatmap-cell corner-cell">工作中心\\日期</div>
                    <div
                      v-for="(dateInfo, dIndex) in getLast14Days()"
                      :key="'date-' + dIndex"
                      class="heatmap-cell header-cell date-cell"
                    >
                      {{ formatDateShort(dateInfo) }}
                    </div>
                  </div>

                  <div
                    v-for="(workcenter, wcIndex) in getUniqueWorkcenters()"
                    :key="'wc-' + wcIndex"
                    class="heatmap-row"
                  >
                    <div class="heatmap-cell row-header">{{ workcenter.substring(0, 10) }}</div>
                    <div
                      v-for="(dateInfo, dIndex) in getLast14Days()"
                      :key="'cap-cell-' + wcIndex + '-' + dIndex"
                      class="heatmap-cell data-cell"
                      :class="getCapacityCellClass(workcenter, dateInfo)"
                      :style="getCapacityCellStyle(workcenter, dateInfo)"
                      @mouseenter="showCapacityTooltip($event, workcenter, dateInfo)"
                      @mouseleave="hideHeatmapTooltip"
                    >
                      <span class="cell-value">{{ getCapacityValue(workcenter, dateInfo) }}%</span>
                    </div>
                  </div>
                </div>

                <div class="heatmap-legend-bar capacity-legend">
                  <span>&lt;70%</span>
                  <div class="gradient-bar capacity-gradient"></div>
                  <span>&gt;90%</span>
                </div>
              </div>

              <div v-else class="empty-hint">暂无产能数据，请刷新数据</div>
            </div>

            <!-- Tooltip（支持多行仓库信息 + 上下方向自适应） -->
            <div
              v-if="tooltipVisible"
              class="heatmap-tooltip"
              :class="'dir-' + tooltipDirection"
              :style="{ left: tooltipX + 'px', top: tooltipY + 'px' }"
            >
              <div class="tooltip-title">{{ tooltipContent.title }}</div>
              <div class="tooltip-value">{{ tooltipContent.value }}</div>
              <div class="tooltip-status" :class="'ts-' + (tooltipContent.status === '有库存' ? 'ok' : 'default')">{{ tooltipContent.status }}</div>
            </div>
          </div>
        </el-card>
      </div>

    </div>
  </div>
</template>

<style scoped lang="scss">
.digital-twin-page {
  width: 100%;
  min-height: 0;
  color: #d8f3ff;
  --dt-cyan: #38bdf8;
  --dt-teal: #2dd4bf;
  --dt-green: #41c99b;
  --dt-amber: #d8a431;
  --dt-coral: #e86f83;
  --dt-panel: rgba(6, 13, 24, 0.82);
  --dt-border: rgba(125, 184, 212, 0.16);
  --dt-muted: rgba(190, 223, 240, 0.62);
}

// ========== 页面头部 ==========
.page-header {
  margin-bottom: 10px;
  padding: 0 2px 9px;
  border-bottom: 1px solid rgba(45, 212, 191, 0.18);
  position: relative;

  &::before {
    content: '';
    position: absolute;
    bottom: -1px;
    left: 0;
    width: 142px;
    height: 2px;
    background: linear-gradient(90deg, var(--dt-teal), var(--dt-cyan), transparent);
    border-radius: 1px;
  }

  .page-title {
    font-size: 18px;
    font-weight: 800;
    background: linear-gradient(135deg, #ffffff 0%, #7dd3fc 48%, #5eead4 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin: 0;
    letter-spacing: 0;
  }

  .page-desc {
    font-size: 11px;
    color: var(--dt-muted);
    margin: 4px 0 0;
    letter-spacing: 0;
  }
}

// ========== 控制栏 ==========
.control-bar {
  background:
    linear-gradient(135deg, rgba(45, 212, 191, 0.08), rgba(56, 189, 248, 0.04)),
    rgba(5, 12, 24, 0.72);
  border: 1px solid var(--dt-border);
  margin-bottom: 8px;
  border-radius: 8px;
  backdrop-filter: blur(14px);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 14px 34px rgba(0, 0, 0, 0.2);

  :deep(.el-card__body) {
    padding: 8px 12px;
  }

  .controls {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
  }

  .left-controls {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;

    :deep(.el-radio-group) {
      background: rgba(1, 7, 18, 0.55);
      border: 1px solid rgba(125, 211, 252, 0.12);
      border-radius: 6px;
      padding: 2px;
    }

    :deep(.el-radio-button) {
      .el-radio-button__inner {
        font-size: 11px;
        padding: 5px 11px;
        border: none;
        background: transparent;
        color: rgba(216, 243, 255, 0.6);
        transition: all 0.25s;
        box-shadow: none !important;
        border-radius: 4px !important;
      }

      &:hover .el-radio-button__inner {
        color: #7dd3fc;
        background: rgba(56, 189, 248, 0.1);
      }

      &.is-active .el-radio-button__inner {
        background: linear-gradient(135deg, rgba(45, 212, 191, 0.95), rgba(56, 189, 248, 0.9));
        color: #03101f;
        box-shadow: 0 7px 18px rgba(45, 212, 191, 0.18);
        font-weight: 600;
      }
    }

    :deep(.el-switch) {
      --el-switch-on-color: #2dd4bf;
    }

    :deep(.el-select) {
      .el-input__wrapper {
        background: rgba(1, 7, 18, 0.55);
        border: 1px solid rgba(125, 211, 252, 0.16);
        box-shadow: none;
        font-size: 11px;
      }
    }
  }

  .right-controls {
    :deep(.el-button) {
      font-size: 11px;
      padding: 6px 13px;
      border-radius: 6px;
      background: linear-gradient(135deg, rgba(45, 212, 191, 0.14), rgba(56, 189, 248, 0.07));
      border: 1px solid rgba(45, 212, 191, 0.32);
      color: #7dd3fc;
      transition: all 0.25s;

      &:hover {
        background: linear-gradient(135deg, rgba(45, 212, 191, 0.22), rgba(56, 189, 248, 0.12));
        border-color: #2dd4bf;
        box-shadow: 0 8px 22px rgba(45, 212, 191, 0.18);
      }
    }
  }
}

// ========== 主布局 ==========
.main-content {
  display: block;
}

// ========== 左侧面板 ==========
.network-panel {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;

  .canvas-card {
    background:
      linear-gradient(145deg, rgba(7, 14, 28, 0.96), rgba(2, 7, 18, 0.98)),
      rgba(4, 10, 22, 0.94);
    border: 1px solid rgba(125, 211, 252, 0.18);
    border-radius: 8px;
    box-shadow: 0 18px 48px rgba(0, 0, 0, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.06);
    transition: border-color 0.3s;

    &:hover {
      border-color: rgba(45, 212, 191, 0.32);
    }

    :deep(.el-card__header) {
      padding: 8px 12px;
      background: linear-gradient(90deg, rgba(45, 212, 191, 0.08), rgba(56, 189, 248, 0.03), transparent);
      border-bottom: 1px solid rgba(125, 211, 252, 0.12);
    }

    :deep(.el-card__body) {
      padding: 5px 5px 0;
    }

    .card-title {
      display: flex;
      align-items: center;
      gap: 5px;
      font-size: 12px;
      font-weight: 600;
      color: #d8f3ff;
      letter-spacing: 0;

      .el-icon { color: var(--dt-teal); }
    }
  }

  // ========== Canvas画布 ==========
  .canvas-container {
    position: relative;
    width: 100%;
    height: clamp(520px, calc(100vh - 280px), 720px) !important;
    background:
      linear-gradient(180deg, #07101b 0%, #020712 100%);
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid rgba(125, 211, 252, 0.12);

    &::before {
      content: '';
      position: absolute;
      inset: 0;
      border-radius: 6px;
      pointer-events: none;
      box-shadow: inset 0 0 34px rgba(0, 0, 0, 0.5), inset 0 0 0 1px rgba(255, 255, 255, 0.025);
      z-index: 1;
    }

    canvas {
      width: 100%;
      height: 100%;
      cursor: crosshair;
      display: block;
      position: relative;
      z-index: 0;
    }

    .legend {
      position: absolute;
      top: 12px;
      left: 14px;
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      background: rgba(3, 10, 20, 0.58);
      padding: 5px 8px;
      border-radius: 7px;
      backdrop-filter: blur(12px);
      border: 1px solid rgba(125, 211, 252, 0.16);
      box-shadow: 0 8px 18px rgba(0, 0, 0, 0.18);
      z-index: 10;

      .legend-item {
        display: flex;
        align-items: center;
        gap: 5px;
        font-size: 9px;
        color: rgba(216, 236, 246, 0.66);
        white-space: nowrap;

        .dot {
          width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
          &.supplier { background: var(--dt-green); }
          &.factory { background: var(--dt-cyan); }
          &.warehouse { background: var(--dt-amber); }
          &.customer { background: var(--dt-coral); }
        }

        .pulse-dot {
          width: 6px; height: 6px; border-radius: 50%;
          background: var(--dt-teal); animation: pulse 1.5s infinite;
        }
      }
    }

    // 时间线全屏视图
    &.timeline-view-container {
      background: linear-gradient(180deg, #0a0e14 0%, #111822 100%);
      overflow-y: auto;

      .timeline-full-list { padding: 8px; }

      .timeline-full-item {
        display: flex; gap: 8px; padding: 5px 0; position: relative;
        &:not(:last-child)::after {
          content: ''; position: absolute; left: 4px; top: 14px; bottom: -2px;
          width: 1.5px; background: linear-gradient(180deg, rgba(64, 158, 255, 0.15), transparent);
        }
        .timeline-full-dot {
          width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; margin-top: 1px;
          border: 2px solid transparent;
          &.primary { background: #409eff; box-shadow: 0 0 6px rgba(64, 158, 255, 0.5); }
          &.success { background: #67c23a; box-shadow: 0 0 6px rgba(103, 194, 58, 0.5); }
          &.warning { background: #e6a23c; box-shadow: 0 0 6px rgba(230, 162, 60, 0.5); }
          &.danger { background: #f56c6c; box-shadow: 0 0 6px rgba(245, 108, 108, 0.5); }
          &.info { background: #78849e; box-shadow: 0 0 4px rgba(120, 132, 158, 0.3); }
        }
        .timeline-full-content { flex: 1; min-width: 0;
          .timeline-full-time { font-size: 9px; color: #5a6478; font-family: 'Courier New', monospace; }
          .timeline-full-title { font-size: 12px; font-weight: 600; color: #d0d7e0; margin: 1px 0; }
          .timeline-full-desc { font-size: 10px; color: #78849e; line-height: 1.3; }
        }
      }
    }

    // 热力图占位视图
    &.heatmap-view-container {
      display: flex; align-items: center; justify-content: center;
      background: linear-gradient(180deg, #0a0e14 0%, #111822 100%);
      .empty-hint { text-align: center; color: #4a5568;
        p { font-size: 12px; margin: 4px 0; }
        .sub-hint { font-size: 10px; color: #5a6478; }
      }
    }

    .empty-hint { text-align: center; color: #4a5568; padding: 12px; }

    // ========== 画布内浮动面板 ==========
    .overlay-panel {
      position: absolute;
      background: var(--dt-panel);
      border: 1px solid var(--dt-border);
      border-radius: 8px;
      backdrop-filter: blur(16px);
      z-index: 10;
      font-size: 11px;
      color: #d8f3ff;
      box-shadow: 0 10px 24px rgba(0, 0, 0, 0.24), inset 0 1px 0 rgba(255, 255, 255, 0.05);
      overflow: hidden;

      &::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(94, 234, 212, 0.75), transparent);
        opacity: 0.75;
      }
    }

    .overlay-title {
      font-size: 11px;
      font-weight: 700;
      color: #7dd3fc;
      padding: 7px 9px 4px;
      border-bottom: 1px solid rgba(125, 211, 252, 0.12);
      display: flex;
      align-items: center;
      gap: 4px;
    }

    // 系统状态（右上角）
    .overlay-status {
      top: 12px;
      right: 12px;
      width: 210px;

      .status-grid {
        padding: 7px 8px 8px;
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px 10px;
      }
      .status-cell {
        display: flex;
        align-items: center;
        gap: 5px;
        font-size: 9.5px;
        color: rgba(216, 243, 255, 0.68);
        .dot-indicator {
          width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
          &.online { background: var(--dt-green); box-shadow: 0 0 8px rgba(52,211,153,0.6); }
          &.warning { background: var(--dt-amber); box-shadow: 0 0 8px rgba(251,191,36,0.6); }
        }
        .status-val { margin-left: auto; color: #5eead4; font-weight: 700; font-size: 9px; }
      }
    }

    // 节点详情弹窗
    .node-popup {
      min-width: 220px;
      max-width: 260px;
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.42);
      border-color: rgba(45, 212, 191, 0.32);

      .popup-header {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 9px 6px;
        border-bottom: 1px solid rgba(125, 211, 252, 0.12);
        .popup-name { font-weight: 700; font-size: 12px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .popup-close {
          width: 20px;
          height: 20px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          background: rgba(255,255,255,0.04);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 5px;
          color: rgba(216, 243, 255, 0.62);
          font-size: 14px;
          cursor: pointer;
          line-height: 1;
          &:hover { color: #fff; border-color: rgba(251,113,133,0.45); background: rgba(251,113,133,0.14); }
        }
      }
      .popup-body {
        padding: 8px 9px;
        .popup-row {
          display: flex; align-items: center; gap: 7px; margin-bottom: 6px; font-size: 10.5px;
          label { color: rgba(190,223,240,0.58); min-width: 58px; }
          .highlight { color: var(--dt-teal); font-weight: 700; }
          :deep(.el-progress) { flex: 1; margin: 0; }
        }
      }
    }

    // 瓶颈分析浮窗
    .overlay-bottleneck {
      top: 130px;
      right: 12px;
      width: 222px;
      .bn-mini {
        display: flex; align-items: center; gap: 6px; padding: 5px 9px; font-size: 10px;
        border-top: 1px solid rgba(255,255,255,0.04);
        &.critical { color: var(--dt-coral); } &.major { color: var(--dt-amber); }
        .bn-severity-dot {
          width: 7px;
          height: 7px;
          border-radius: 50%;
          flex: 0 0 auto;
          background: var(--dt-amber);
          box-shadow: 0 0 6px rgba(251, 191, 36, 0.55);
          &.critical {
            background: var(--dt-coral);
            box-shadow: 0 0 6px rgba(251, 113, 133, 0.6);
          }
        }
      }
    }

    // KPI指标（底部浮动条）
    .overlay-kpi {
      position: absolute;
      top: 75%;
      right: 8px;
      left: auto;
      display: flex;
      justify-content: flex-end;
      gap: 4px;
      background: none;
      padding: 0;
      z-index: 10;

      .kpi-mini {
        display: flex;
        flex-direction: column;
        align-items: center;
        min-width: 56px;
        padding: 3px 8px 2px;
        border: 1px solid rgba(125, 184, 212, 0.13);
        border-radius: 6px;
        background: rgba(5, 14, 26, 0.75);

        .kpi-val {
          font-size: 15px;
          font-weight: 800;
          line-height: 1.1;
          font-family: 'DIN', 'Orbitron', monospace;
        }
        .kpi-lbl {
          font-size: 8px;
          color: rgba(190, 223, 240, 0.55);
          white-space: nowrap;
        }

        &:nth-child(1) .kpi-val { color: var(--dt-cyan); }
        &:nth-child(2) .kpi-val { color: var(--dt-teal); }
        &:nth-child(3) .kpi-val { color: var(--dt-amber); }
        &:nth-child(4) .kpi-val { color: var(--dt-green); }

        &.active .kpi-val { text-shadow: 0 0 10px rgba(45,212,191,0.45); }
        &.warning .kpi-val { text-shadow: 0 0 10px rgba(251,191,36,0.42); }
      }
    }
  }

  // ========== KPI指标行 ==========
  .metrics-row {
    margin-top: 0;

    :deep(.el-col) { padding: 1px !important; }

    .metric-box {
      background: linear-gradient(145deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0.01));
      border-radius: 6px;
      padding: 6px 4px;
      text-align: center;
      border: 1px solid rgba(255, 255, 255, 0.05);
      transition: all 0.25s ease;
      position: relative;
      overflow: hidden;

      &::before {
        content: '';
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.08), transparent);
      }

      &:hover {
        background: linear-gradient(145deg, rgba(64, 158, 255, 0.08), rgba(64, 158, 255, 0.02));
        transform: translateY(-1px);
        border-color: rgba(64, 158, 255, 0.15);
      }

      &.active { border-color: rgba(103, 194, 58, 0.3); }
      &.warning { border-color: rgba(230, 162, 60, 0.3); }

      .metric-icon-wrap {
        width: 24px; height: 24px; border-radius: 6px;
        display: inline-flex; align-items: center; justify-content: center;
        color: white; margin-bottom: 3px;
        position: relative;
        overflow: hidden;

        &::after {
          content: '';
          position: absolute;
          inset: 0;
          background: linear-gradient(135deg, rgba(255, 255, 255, 0.15), transparent);
          border-radius: inherit;
        }

        :deep(.el-icon) { font-size: 11px !important; --el-icon-size: 11px !important; position: relative; z-index: 1; }
      }

      .metric-value {
        font-size: 18px;
        font-weight: 800;
        color: #e8ecf2;
        line-height: 1.1;
        letter-spacing: 0;
        text-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
      }

      .metric-label {
        font-size: 9px;
        color: #6b7485;
        margin-top: 3px;
        line-height: 1;
        letter-spacing: 0.4px;
      }

      .metric-change {
        position: absolute; top: 3px; right: 4px;
        font-size: 8px; font-weight: 600; padding: 0 4px; border-radius: 3px;
        &.positive { color: #67c23a; background: rgba(103, 194, 58, 0.12); }
        &.negative { color: #f56c6c; background: rgba(245, 108, 108, 0.12); }
      }
    }
  }
}

// ========== 右侧详情面板 ==========
.detail-panel {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  overflow-y: auto;
  max-height: calc(100vh - 160px);

  &::-webkit-scrollbar { width: 3px; }
  &::-webkit-scrollbar-track { background: transparent; }
  &::-webkit-scrollbar-thumb { background: rgba(64, 158, 255, 0.2); border-radius: 2px; }

  // 统一卡片基础样式
  .info-card,
  .events-card,
  .status-card,
  .timeline-card,
  .placeholder-card {
    background: linear-gradient(145deg, rgba(20, 26, 36, 0.95), rgba(15, 20, 28, 0.98));
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 7px;
    transition: all 0.25s ease;

    &:hover {
      border-color: rgba(64, 158, 255, 0.15);
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
    }

    :deep(.el-card__header) {
      padding: 6px 10px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      background: linear-gradient(90deg, rgba(64, 158, 255, 0.04), transparent);
    }

    :deep(.el-card__body) {
      padding: 6px 8px;
    }

    .card-title {
      display: flex; align-items: center; gap: 5px;
      font-size: 11px; font-weight: 600; color: #c8d0dc;
      letter-spacing: 0.3px;
      .el-icon { color: #409eff; }
    }
  }

  // 占位卡片（未选中节点）
  .placeholder-card {
    background: linear-gradient(145deg, rgba(18, 22, 30, 0.9), rgba(12, 16, 24, 0.95));
    border-style: dashed;
    border-color: rgba(255, 255, 255, 0.06);
    flex-shrink: 0;

    :deep(.el-card__body) { padding: 6px !important; }

    .placeholder-content {
      text-align: center; padding: 8px 10px; color: #4a5568;
      .placeholder-icon { font-size: 14px; color: #3a4558; margin-bottom: 3px; opacity: 0.6; }
      p { margin: 0; font-size: 9px; color: #3d4858; }
    }
  }

  // 节点详情
  .node-details {
    .detail-item {
      margin-bottom: 5px;
      padding-bottom: 5px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);

      &:last-child { margin-bottom: 0; border-bottom: none; padding-bottom: 0; }

      label {
        display: block;
        font-size: 9px;
        color: #5e6878;
        margin-bottom: 2px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }

      span { color: #c8d0dc; font-size: 11px; font-weight: 500; }
      .highlight { color: #67c23a; font-weight: 700; font-size: 13px; text-shadow: 0 0 8px rgba(103, 194, 58, 0.3); }
    }

    :deep(.el-progress) {
      --el-progress-border-radius: 4px;
      .el-progress-bar__outer { background: rgba(255, 255, 255, 0.06); }
      .el-progress-bar__inner { background: linear-gradient(90deg, #409eff, #67c23a); }
    }
  }

  // 物流事件列表
  .events-list {
    display: flex; flex-direction: column; gap: 4px;
    max-height: 160px; overflow-y: auto;
    &::-webkit-scrollbar { width: 2px; }
    &::-webkit-scrollbar-thumb { background: rgba(64, 158, 255, 0.15); border-radius: 1px; }

    .event-item {
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.02), rgba(255, 255, 255, 0.01));
      border: 1px solid rgba(255, 255, 255, 0.04);
      border-radius: 5px; padding: 6px; cursor: pointer; transition: all 0.2s ease;
      position: relative;
      overflow: hidden;

      &::before {
        content: '';
        position: absolute;
        left: 0; top: 0; bottom: 0;
        width: 2px;
        background: transparent;
        transition: background 0.2s;
      }

      &:hover {
        background: linear-gradient(135deg, rgba(64, 158, 255, 0.06), rgba(64, 158, 255, 0.02));
        border-color: rgba(64, 158, 255, 0.15);

        &::before { background: #409eff; }
      }

      &.selected {
        background: linear-gradient(135deg, rgba(64, 158, 255, 0.1), rgba(64, 158, 255, 0.04));
        border-color: rgba(64, 158, 255, 0.25);

        &::before { background: #409eff; }
      }

      &.delayed {
        &::before { background: #f56c6c; }
        border-left-color: rgba(245, 108, 108, 0.3);
      }

      .event-header { display: flex; gap: 4px; margin-bottom: 3px; flex-wrap: wrap;
        :deep(.el-tag) {
          font-size: 9px; height: 18px; line-height: 17px; padding: 0 6px;
          border-radius: 3px;
        }
        :deep(.el-tag--info) { background: rgba(64, 158, 255, 0.12); border-color: rgba(64, 158, 255, 0.2); color: #79bbff; }
        :deep(.el-tag--success) { background: rgba(103, 194, 58, 0.12); border-color: rgba(103, 194, 58, 0.2); color: #95d475; }
        :deep(.el-tag--warning) { background: rgba(230, 162, 60, 0.12); border-color: rgba(230, 162, 60, 0.2); color: #e6a23c; }
        :deep(.el-tag--danger) { background: rgba(245, 108, 108, 0.12); border-color: rgba(245, 108, 108, 0.2); color: #f78989; }
        :deep(.el-tag--primary) { background: rgba(64, 158, 255, 0.12); border-color: rgba(64, 158, 255, 0.25); color: #79bbff; }
      }

      .event-body {
        .material-name { font-weight: 600; color: #d0d7e0; font-size: 10.5px; margin-bottom: 2px; }
        .route-info { font-size: 9px; color: #6b7585; margin-bottom: 1px; .arrow { margin: 0 2px; color: #505868; } }
        .quantity { font-size: 9px; color: #8892a4; }
        .progress-section { margin-top: 3px;
          :deep(.el-progress) {
            .el-progress-bar__outer { background: rgba(255, 255, 255, 0.06); height: 4px !important; border-radius: 2px; }
            .el-progress-bar__inner { border-radius: 2px; }
            .el-progress__text { font-size: 9px !important; }
          }
          .eta { font-size: 8px; color: #5a6478; margin-top: 2px; }
        }
      }
    }
  }

  // 系统状态
  .system-status {
    .status-item {
      display: flex; align-items: center; gap: 6px;
      padding: 4px 0; font-size: 10px; color: #8892a4;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);
      transition: all 0.2s;

      &:last-child { border-bottom: none; }
      &:hover { color: #b0b8c8; background: rgba(255, 255, 255, 0.02); margin: 0 -4px; padding-left: 4px; padding-right: 4px; border-radius: 3px; }

      .indicator {
        width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
        &.online {
          background: #67c23a;
          box-shadow: 0 0 6px rgba(103, 194, 58, 0.6), 0 0 12px rgba(103, 194, 58, 0.2);
          animation: glow-pulse 2s infinite;
        }
        &.warning { background: #e6a23c; animation: blink 2s infinite; box-shadow: 0 0 4px rgba(230, 162, 60, 0.4); }
        &.offline { background: #4a5568; }
      }

      .status-label { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .time { margin-left: auto; font-size: 8px; color: #5a6478; font-family: 'Courier New', monospace; flex-shrink: 0; }
    }
  }

  // 时间线
  .timeline-list {
    max-height: 150px; overflow-y: auto;
    &::-webkit-scrollbar { width: 2px; }
    &::-webkit-scrollbar-thumb { background: rgba(64, 158, 255, 0.15); border-radius: 1px; }

    .timeline-item {
      display: flex; gap: 7px; padding: 4px 0; position: relative;
      transition: all 0.2s;

      &:hover { .timeline-title { color: #fff; } }

      &:not(:last-child)::after {
        content: ''; position: absolute; left: 3.5px; top: 14px; bottom: -2px;
        width: 1.5px; background: linear-gradient(180deg, rgba(64, 158, 255, 0.12), transparent);
      }

      .timeline-dot {
        width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; margin-top: 2px;
        border: 2px solid transparent;
        transition: transform 0.2s;
        &.info { background: #409eff; box-shadow: 0 0 5px rgba(64, 158, 255, 0.5); }
        &.success { background: #67c23a; box-shadow: 0 0 5px rgba(103, 194, 58, 0.5); }
        &.warning { background: #e6a23c; box-shadow: 0 0 5px rgba(230, 162, 60, 0.5); }
        &.danger { background: #f56c6c; box-shadow: 0 0 5px rgba(245, 108, 108, 0.5); }
      }

      &:hover .timeline-dot { transform: scale(1.2); }

      .timeline-content { flex: 1; min-width: 0;
        .timeline-time { font-size: 8px; color: #4e5768; font-family: 'Courier New', monospace; margin-bottom: 1px; letter-spacing: 0.3px; }
        .timeline-title { font-size: 10px; font-weight: 600; color: #b8c2d0; margin-bottom: 1px; transition: color 0.2s; }
        .timeline-desc { font-size: 9px; color: #606978; line-height: 1.3; }
      }
    }
  }
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(1.2); }
}
@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
@keyframes glow-pulse {
  0%, 100% {
    box-shadow: 0 0 6px rgba(103, 194, 58, 0.6), 0 0 12px rgba(103, 194, 58, 0.2);
  }
  50% {
    box-shadow: 0 0 10px rgba(103, 194, 58, 0.8), 0 0 20px rgba(103, 194, 58, 0.35);
  }
}

// ========== 响应式断点 ==========
@media (max-width: 1400px) {
  .canvas-container { height: 480px !important; }
}

@media (max-width: 1200px) {
  .main-content { display: block; }
}

@media (max-width: 900px) {
  .canvas-container { height: 420px !important; }
  .network-panel .canvas-container {
    .overlay-status { width: 200px; }
    .overlay-bottleneck { display: none; }
  }
  .metrics-row .metric-box { padding: 4px 3px; }
  .metrics-row .metric-value { font-size: 15px; }
}

@media (max-width: 767px) {
  .page-header { margin-bottom: 4px; padding-bottom: 4px; }
  .page-title { font-size: 14px; }
  .control-bar {
    margin-bottom: 4px;
    border-radius: 6px;

    :deep(.el-card__body) { padding: 7px 8px !important; }

    .controls {
      align-items: flex-start;
      flex-wrap: wrap !important;
      gap: 6px;
    }

    .left-controls {
      width: 100%;
      display: grid !important;
      grid-template-columns: 1fr auto;
      flex-wrap: wrap !important;
      gap: 6px;

      :deep(.el-radio-group) {
        grid-column: 1 / -1;
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        width: 100%;
      }

      :deep(.el-radio-button) {
        min-width: 0;

        .el-radio-button__inner {
          width: 100%;
          padding: 5px 3px;
          font-size: 10px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 3px;
        }
      }

      :deep(.el-switch) {
        margin-left: 0 !important;
      }
    }

    .right-controls {
      width: 100%;
      display: flex;
      justify-content: flex-end;

      :deep(.el-button) {
        min-height: 28px;
        padding: 5px 10px;
      }
    }
  }
  .canvas-container { height: 360px !important; }
  .main-content { gap: 5px; }

  .network-panel .canvas-card {
    :deep(.el-card__header) { padding: 5px 8px; }
  }

  .network-panel .canvas-container {
    .legend {
      top: 8px;
      left: 8px;
      max-width: calc(100% - 16px);
    }

    .overlay-status {
      display: none;
    }

    .overlay-kpi {
      gap: 4px;
      padding: 11px 8px 8px;

      .kpi-mini {
        min-width: 0;
        flex: 1;
        padding: 5px 4px;

        .kpi-val { font-size: 16px; }
        .kpi-lbl { font-size: 8px; }
      }
    }
  }
}

// ========== 新增：回放控制条样式 ==========
.replay-control-bar {
  margin-top: 8px;
  padding: 10px;
  background: linear-gradient(135deg, rgba(45, 212, 191, 0.08) 0%, rgba(255, 255, 255, 0.02) 100%);
  border-radius: 6px;
  border: 1px solid rgba(125, 211, 252, 0.14);

  .replay-controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 8px;

    .replay-btn {
      min-width: 44px;
      height: 32px;
      padding: 0 9px;
      border: 1px solid rgba(45, 212, 191, 0.32);
      background: rgba(255, 255, 255, 0.05);
      color: var(--dt-teal);
      border-radius: 6px;
      cursor: pointer;
      font-size: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.25s ease;

      &:hover {
        background: rgba(45, 212, 191, 0.15);
        border-color: var(--dt-teal);
        transform: scale(1.05);
      }

      &:disabled {
        opacity: 0.4;
        cursor: not-allowed;
        transform: none;
      }

      &.active {
        background: linear-gradient(135deg, var(--dt-teal), var(--dt-cyan));
        color: #03101f;
        border-color: transparent;
        box-shadow: 0 2px 8px rgba(45, 212, 191, 0.3);
      }
    }

    .speed-controls {
      display: flex;
      align-items: center;
      gap: 5px;

      .speed-label {
        font-size: 10px;
        color: rgba(190, 223, 240, 0.62);
        font-weight: 600;
      }

      .speed-btn {
        padding: 3px 8px;
        border: 1px solid rgba(52, 211, 153, 0.32);
        background: rgba(255, 255, 255, 0.03);
        color: var(--dt-green);
        border-radius: 4px;
        cursor: pointer;
        font-size: 11px;
        font-weight: 600;
        transition: all 0.2s ease;

        &:hover {
          background: rgba(52, 211, 153, 0.12);
          border-color: var(--dt-green);
        }

        &.active {
          background: linear-gradient(135deg, var(--dt-green), #5eead4);
          color: #03101f;
          border-color: transparent;
          box-shadow: 0 2px 6px rgba(52, 211, 153, 0.28);
        }
      }
    }

    .date-range-selector {
      .date-select {
        padding: 4px 8px;
        border: 1px solid rgba(251, 191, 36, 0.32);
        background: rgba(3, 10, 22, 0.95);
        color: var(--dt-amber);
        border-radius: 4px;
        font-size: 11px;
        cursor: pointer;
        outline: none;

        option {
          background: #1a1f2e;
          color: var(--dt-amber);
        }
      }
    }

    .current-date-display {
      font-size: 11px;
      color: rgba(216, 243, 255, 0.72);
      margin-left: auto;

      strong {
        color: var(--dt-cyan);
        font-weight: 700;
        font-family: 'Courier New', monospace;
      }
    }
  }

  .replay-progress {
    .progress-slider {
      width: 100%;
      height: 6px;
      -webkit-appearance: none;
      appearance: none;
      background: linear-gradient(90deg,
        rgba(45, 212, 191, 0.15) 0%,
        rgba(56, 189, 248, 0.25) 50%,
        rgba(45, 212, 191, 0.15) 100%
      );
      border-radius: 3px;
      outline: none;
      cursor: pointer;

      &::-webkit-slider-thumb {
        -webkit-appearance: none;
        appearance: none;
        width: 16px;
        height: 16px;
        background: linear-gradient(135deg, var(--dt-teal), var(--dt-cyan));
        border-radius: 50%;
        cursor: pointer;
        box-shadow: 0 2px 8px rgba(45, 212, 191, 0.36);
        transition: transform 0.2s ease;

        &:hover {
          transform: scale(1.2);
        }
      }

      &::-moz-range-thumb {
        width: 16px;
        height: 16px;
        background: linear-gradient(135deg, var(--dt-teal), var(--dt-cyan));
        border-radius: 50%;
        cursor: pointer;
        border: none;
        box-shadow: 0 2px 8px rgba(45, 212, 191, 0.36);
      }
    }
  }
}

// ========== 热力图视图样式（优化版） ==========
.heatmap-view-container {
  position: relative !important;
  width: 100% !important;
  // 继承父容器高度（canvas-container 已设 clamp(520px, calc(100vh-280px), 720px)）
  height: 100% !important;
  min-height: unset !important;
  max-height: unset !important;
  overflow-y: auto;
  padding: 20px 16px;
  display: flex;
  flex-direction: column;
  align-items: center;

  &::-webkit-scrollbar { width: 5px; }
  &::-webkit-scrollbar-track { background: rgba(255, 255, 255, 0.02); }
  &::-webkit-scrollbar-thumb { background: rgba(45, 212, 191, 0.3); border-radius: 3px; }

  .heatmap-tabs {
    display: flex;
    gap: 10px;
    margin-bottom: 14px;
    justify-content: center;

    .heatmap-tab {
      padding: 7px 20px;
      border: 1px solid rgba(125, 211, 252, 0.25);
      background: rgba(255, 255, 255, 0.03);
      color: rgba(216, 243, 255, 0.65);
      border-radius: 8px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.5px;
      transition: all 0.25s ease;

      &:hover {
        background: rgba(45, 212, 191, 0.1);
        color: #7dd3fc;
        border-color: rgba(45, 212, 191, 0.45);
        transform: translateY(-1px);
      }

      &.active {
        background: linear-gradient(135deg, rgba(45, 212, 191, 0.2), rgba(56, 189, 248, 0.12));
        color: #7dd3fc;
        border-color: var(--dt-teal);
        box-shadow: 0 2px 14px rgba(45, 212, 191, 0.2), inset 0 1px 0 rgba(255,255,255,0.05);
      }
    }
  }

  .heatmap-content {
    animation: fadeInUp 0.35s ease-out;
    width: 100%;
    max-width: 1200px;
    flex: 1;
  }

  @keyframes fadeInUp {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: translateY(0); }
  }

  // 物料-仓库热度矩阵 / 产能利用率矩阵
  .material-heatmap,
  .capacity-heatmap {
    overflow-x: auto;
    padding: 4px;

    // 数据统计概览条
    .heatmap-stats {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 10px;
      padding: 8px 14px;
      background: linear-gradient(90deg, rgba(45,212,191,0.05), transparent);
      border-radius: 6px;
      font-size: 10px;
      color: rgba(216,243,255,0.55);
      flex-wrap: wrap;

      .stat-item {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-weight: 550;
      }

      .stat-divider {
        color: rgba(255,255,255,0.1);
        margin: 0 4px;
      }

      .stat-dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;

        &.dot-green { background: #34d399; box-shadow: 0 0 6px rgba(52,211,153,0.5); }
        &.dot-yellow { background: #fbbf24; box-shadow: 0 0 6px rgba(251,191,36,0.5); }
        &.dot-red { background: #fb7185; box-shadow: 0 0 6px rgba(251,113,133,0.5); }
      }
    }

    &::-webkit-scrollbar { height: 5px; }
    &::-webkit-scrollbar-track { background: transparent; }
    &::-webkit-scrollbar-thumb { background: rgba(45, 212, 191, 0.2); border-radius: 3px; }

    .heatmap-grid {
      display: grid;
      gap: 4px;
      font-size: 12px;

      // 物料-仓库单行横排：表头(仓库名) + 一行数据格，自动填充
      grid-template-columns: repeat(auto-fill, minmax(70px, 1fr));

      &.capacity-grid {
        // 产能模式：首列(工作中心) + 14天日期列均分
        grid-template-columns: 120px repeat(14, 1fr);
        min-width: 900px;
      }
    }

    .heatmap-header {
      display: contents;

      .corner-cell {
        background: linear-gradient(135deg, rgba(45, 212, 191, 0.18), rgba(56, 189, 248, 0.1));
        color: #7dd3fc;
        font-weight: 700;
        font-size: 11px;
        border-radius: 5px;
        position: sticky;
        left: 0;
        z-index: 5;
        padding: 7px 6px;
      }

      .header-cell {
        background: linear-gradient(180deg, rgba(56, 189, 248, 0.12), rgba(56, 189, 248, 0.06));
        color: #bae6fd;
        font-weight: 600;
        font-size: 10px;
        text-align: center;
        padding: 7px 4px;
        border-radius: 5px;
        position: sticky;
        top: 0;
        z-index: 4;
        letter-spacing: 0.3px;

        &.date-cell {
          writing-mode: vertical-rl;
          text-orientation: mixed;
          font-size: 9px;
          letter-spacing: 1.5px;
          padding: 6px 2px;
        }
      }
    }

    .heatmap-row {
      display: contents;

      &:nth-child(even) .data-cell {
        background-color: inherit; /* 让渐变色不受影响 */
      }

      .row-header {
        background: linear-gradient(90deg, rgba(125, 211, 252, 0.1), rgba(125, 211, 252, 0.04));
        color: rgba(216, 243, 255, 0.82);
        font-weight: 580;
        font-size: 10px;
        padding: 7px 8px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        border-radius: 5px;
        position: sticky;
        left: 0;
        z-index: 3;
        border-right: 1px solid rgba(125, 211, 252, 0.1);
      }

      .data-cell {
        position: relative;
        min-width: 60px;
        min-height: 46px;
        border-radius: 6px;
        cursor: pointer;
        transition: all 0.22s ease;
        display: flex;
        align-items: center;
        justify-content: center;
        border: 1px solid rgba(255, 255, 255, 0.07);

        &:hover {
          transform: scale(1.18) translateY(-2px);
          z-index: 10;
          box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45), 0 0 0 1px rgba(255,255,255,0.15);
          border-color: rgba(255, 255, 255, 0.25);

          .cell-value {
            font-size: 10px;
            text-shadow: 0 1px 3px rgba(0,0,0,0.8);
          }
        }

        .cell-value {
          font-size: 9px;
          font-weight: 700;
          color: #fff;
          text-shadow: 0 1px 2px rgba(0, 0, 0, 0.55);
          pointer-events: none;
          line-height: 1.3;
        }

        // 状态动画类（保留用于脉冲效果）
        &.status-shortage,
        &.capacity-high {
          animation: pulse-warning 2s ease-in-out infinite;
        }
      }
    }

    // ===== 矩阵热力图（行=仓库编号，列=100种物料，无表头） =====
    .matrix-heatmap-grid {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 100%;
      overflow-x: auto;
      padding-bottom: 4px;

      .matrix-data-row {
        display: flex;
        gap: 2px;
        align-items: stretch;

        // 左侧深色仓库名（带编号：01 WH002）
        .matrix-wh-label {
          width: 90px;
          min-width: 90px;
          flex-shrink: 0;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          padding: 6px 4px;
          background: linear-gradient(135deg, rgba(12, 22, 42, 0.97), rgba(18, 32, 58, 0.93));
          border: 1px solid rgba(45, 140, 180, 0.18);
          border-radius: 4px;
          position: sticky;
          left: 0;
          z-index: 3;
          box-shadow: 2px 0 10px rgba(0, 0, 0, 0.25);

          // 编号
          .wh-index {
            font-size: 16px;
            font-weight: 900;
            font-family: 'DIN Alternate', 'Consolas', monospace;
            color: var(--dt-cyan);
            text-shadow: 0 0 10px rgba(56, 189, 248, 0.5);
            line-height: 1;
            letter-spacing: -0.5px;
          }

          // 仓名称
          .wh-name {
            font-size: 9.5px;
            font-weight: 600;
            color: #7ab8d4;
            margin-top: 3px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 100%;
            letter-spacing: 0.3px;
          }
        }

        // 热力值格子（紧凑型，适配100列）
        .matrix-cell {
          flex: 1;
          min-width: 56px;
          max-width: 80px;
          height: 44px;
          border-radius: 4px;
          cursor: pointer;
          transition: all 0.2s ease;
          display: flex;
          align-items: center;
          justify-content: center;
          border: 1px solid rgba(255, 255, 255, 0.05);

          &:hover {
            transform: scale(1.2) translateY(-2px);
            z-index: 10;
            box-shadow: 0 6px 22px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255,255,255,0.2);
            border-color: rgba(255, 255, 255, 0.3);

            .cell-value {
              font-size: 9.5px;
              text-shadow: 0 1px 4px rgba(0,0,0,0.95);
            }
          }

          .cell-value {
            font-size: 8.5px;
            font-weight: 800;
            color: #fff;
            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.5);
            pointer-events: none;
            line-height: 1.25;
            font-family: 'DIN Alternate', 'Consolas', monospace;
            white-space: nowrap;
          }

          &.status-shortage {
            animation: pulse-warning 2s ease-in-out infinite;
          }

          &.status-none {
            // 未存放：虚线边框 + 无动画
            opacity: 0.7;
            .cell-value { color: rgba(180, 195, 220, 0.55); }
          }
        }
      }
    }

    .heatmap-legend-bar {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      margin-top: 16px;
      padding: 10px 16px;
      background: linear-gradient(90deg, rgba(255,255,255,0.02), rgba(255,255,255,0.04), rgba(255,255,255,0.02));
      border-radius: 6px;
      font-size: 10px;
      color: rgba(216, 243, 255, 0.65);
      border: 1px solid rgba(255, 255, 255, 0.04);

      span {
        font-weight: 650;
        min-width: 40px;
        letter-spacing: 0.5px;
      }

      .gradient-bar {
        width: 200px;
        height: 12px;
        background: linear-gradient(90deg,
          #dc2626 0%,
          #f97316 20%,
          #eab308 40%,
          #84cc16 60%,
          #22c55e 80%,
          #06b6d4 100%
        );
        border-radius: 6px;
        box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.25), 0 1px 0 rgba(255,255,255,0.05);
      }

      &.capacity-legend .gradient-bar {
        background: linear-gradient(90deg,
          #22c55e 0%,
          #84cc16 30%,
          #eab308 60%,
          #f97316 82%,
          #dc2626 100%
        );
      }
    }
  }

  // Tooltip（支持多行仓库分布信息）
  .heatmap-tooltip {
    position: absolute;
    padding: 10px 14px;
    background: linear-gradient(145deg, rgba(8, 18, 38, 0.98), rgba(15, 28, 52, 0.96));
    border: 1px solid rgba(45, 212, 191, 0.35);
    border-radius: 8px;
    color: #d8f3ff;
    font-size: 11px;
    line-height: 1.6;
    z-index: 1000;
    pointer-events: none;
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(45, 212, 191, 0.08) inset;
    white-space: pre-line;      /* 支持多行文本 */
    animation: tooltipFadeIn 0.18s ease-out;
    backdrop-filter: blur(12px);
    max-width: 360px;

    // 默认向上显示（格子下方空间不够时用 dir-down）
    &.dir-up {
      transform: translate(-50%, -100%);
      margin-top: -10px;
      // 上方小三角箭头
      &::after {
        content: '';
        position: absolute;
        bottom: -6px;
        left: 50%;
        transform: translateX(-50%);
        border-left: 6px solid transparent;
        border-right: 6px solid transparent;
        border-top: 6px solid rgba(45, 212, 191, 0.35);
        filter: drop-shadow(0 3px 3px rgba(0,0,0,0.4));
      }
    }

    // 向下显示（第一行附近，上方空间不足时）
    &.dir-down {
      transform: translate(-50%, 0);
      margin-top: 8px;
      // 下方小三角箭头
      &::after {
        content: '';
        position: absolute;
        top: -6px;
        left: 50%;
        transform: translateX(-50%);
        border-left: 6px solid transparent;
        border-right: 6px solid transparent;
        border-bottom: 6px solid rgba(45, 212, 191, 0.35);
        filter: drop-shadow(0 -2px 2px rgba(0,0,0,0.3));
      }
    }

    .tooltip-title {
      color: #7dd3fc;
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 6px;
      letter-spacing: 0.3px;
      white-space: nowrap;
      text-shadow: 0 0 10px rgba(45, 212, 191, 0.25);
    }

    .tooltip-value {
      color: rgba(216, 243, 255, 0.85);
      margin-bottom: 5px;
      word-break: break-all;
    }

    .tooltip-status {
      display: inline-block;
      padding: 2px 10px;
      border-radius: 4px;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.5px;

      &.ts-ok {
        color: #34d399;
        background: rgba(52, 211, 153, 0.12);
        border: 1px solid rgba(52, 211, 153, 0.25);
      }
      &.ts-warn {
        color: #fbbf24;
        background: rgba(251, 191, 36, 0.12);
        border: 1px solid rgba(251, 191, 36, 0.25);
      }
      &.ts-danger {
        color: #fb7185;
        background: rgba(251, 113, 133, 0.12);
        border: 1px solid rgba(251, 113, 133, 0.25);
      }
    }

    strong {
      color: #7dd3fc;
      display: block;
      margin-bottom: 5px;
      font-size: 12px;
      font-weight: 700;
    }

    small {
      color: rgba(216, 243, 255, 0.55);
      display: block;
      margin-top: 5px;
      padding-top: 4px;
      border-top: 1px solid rgba(255,255,255,0.06);
    }
  }

  @keyframes tooltipFadeIn {
    from { opacity: 0; transform: translate(-50%, -100%) scale(0.92) translateY(4px); }
    to { opacity: 1; transform: translate(-50%, -100%) scale(1) translateY(0); }
  }

  @keyframes pulse-warning {
    0%, 100% { box-shadow: 0 0 0 0 rgba(251, 113, 133, 0.35); }
    50% { box-shadow: 0 0 0 5px rgba(251, 113, 133, 0); }
  }
}

// ========== 新增：瓶颈分析卡片样式 ==========
.bottleneck-card {
  background: linear-gradient(145deg, rgba(45, 20, 20, 0.95), rgba(35, 15, 15, 0.98)) !important;
  border: 1px solid rgba(245, 108, 108, 0.25) !important;
  animation: bottleneckPulse 2s infinite;

  :deep(.el-card__header) {
    background: linear-gradient(90deg, rgba(245, 108, 108, 0.08), transparent);
    border-bottom: 1px solid rgba(245, 108, 108, 0.15);
  }

  .bottleneck-title {
    .el-icon { color: #f56c6c !important; }
  }

  .bottleneck-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    max-height: 200px;
    overflow-y: auto;

    &::-webkit-scrollbar { width: 2px; }
    &::-webkit-scrollbar-thumb { background: rgba(245, 108, 108, 0.3); border-radius: 1px; }
  }

  .bottleneck-item {
    background: linear-gradient(135deg, rgba(255, 255, 255, 0.02), rgba(255, 255, 255, 0.01));
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: 5px;
    padding: 8px;
    transition: all 0.25s ease;

    &:hover {
      background: linear-gradient(135deg, rgba(245, 108, 108, 0.06), rgba(245, 108, 108, 0.02));
      border-color: rgba(245, 108, 108, 0.2);
      transform: translateX(3px);
    }

    &.critical {
      border-left: 3px solid #f56c6c;
    }

    &.major {
      border-left: 3px solid #e6a23c;
    }

    &.minor {
      border-left: 3px solid #e6a23c;
      opacity: 0.85;
    }

    .bottleneck-header {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 6px;

      .severity-icon {
        font-size: 14px;
        flex-shrink: 0;
      }

      .material-name {
        flex: 1;
        font-size: 10.5px;
        color: #d0d7e0;
        font-weight: 600;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
    }

    .bottleneck-details {
      .detail-row {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 9px;
        margin-bottom: 3px;
        color: #8892a4;

        &:last-child { margin-bottom: 0; }

        label {
          color: #5e6878;
          font-weight: 600;
          min-width: 60px;
          text-transform: uppercase;
          font-size: 8px;
          letter-spacing: 0.3px;
        }

        .shortage-value {
          color: #f56c6c;
          font-weight: 700;
          font-family: 'Courier New', monospace;
        }

        &.suggestion {
          margin-top: 4px;
          padding-top: 4px;
          border-top: 1px dashed rgba(255, 255, 255, 0.05);

          label { color: #67c23a; }
          span { color: #95d475; font-style: italic; }
        }
      }
    }
  }

  @keyframes bottleneckPulse {
    0%, 100% {
      box-shadow: 0 0 0 0 rgba(245, 108, 108, 0);
    }
    50% {
      box-shadow: 0 0 0 4px rgba(245, 108, 108, 0.1);
    }
  }
}

// ========== 新增：瓶颈指示器（图例用） ==========
.bottleneck-indicator {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #f56c6c;
  animation: bottleneckBlink 1s infinite;
  box-shadow: 0 0 6px rgba(245, 108, 108, 0.8), 0 0 12px rgba(245, 108, 108, 0.3);
}

@keyframes bottleneckBlink {
  0%, 100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.5;
    transform: scale(1.3);
  }
}
</style>
