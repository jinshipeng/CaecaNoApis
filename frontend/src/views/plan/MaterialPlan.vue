<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, onActivated } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  DataAnalysis,
  Refresh,
  VideoPlay,
  Warning,
  Check,
  CircleClose,
  Box,
  TrendCharts,
  BellFilled,
  Document,
  Connection,
  Cpu
} from '@element-plus/icons-vue'
import { getPlanningSummary, getMaterialPlanDetail, runPlanning, getShortageReport } from '@/api'
import type { PlanningSummary, ShortageItem } from '@/types/api'
import request from '@/api/request'

const router = useRouter()

interface RootCauseAnalysis {
  category: string
  material_id: number
  cause: string
  impact: string
  recommendation: string
}

interface ProcurementRecommendation {
  type: string
  material_id: number
  message: string
  priority: string
  estimated_saving?: number
}

interface AIAnalysis {
  allocation_quality: number
  inventory_utilization: number
  expiry_risk: number
  supplier_risk: number
  stagnation_risk: number
  root_cause_analysis: RootCauseAnalysis[]
  procurement_recommendations: ProcurementRecommendation[]
  quality_breakdown: Record<string, number>
}

interface PlanLog {
  id: number
  log_type: string
  message: string
  created_at: string
}

interface ReleaseRecord {
  source_order_no: string
  target_order_no: string
  material_id: number
  material_code: string
  released_quantity: number
  original_priority: number
  new_priority: number
  reason: string
}

interface MaterialPlanDetail {
  id: number
  material_code: string
  material_name: string
  demand: number
  stock: number
  shortage: number
  status: 'shortage' | 'warning' | 'sufficient'
  priority: 'high' | 'normal' | 'low'
  safety_stock: number
  latest_purchase_date?: string
  days_to_latest_purchase?: number
  urgency_level?: 'critical' | 'urgent' | 'normal' | 'relaxed'
  urgency_label?: string
  recommended_action?: string
  // v4-BOM 替代料字段
  original_shortage?: number
  alternative_materials?: Array<{
    original_material: string
    substitute_material: string
    substitute_name: string
    quantity: number
    priority: number
    strategy: string
    reason: string
  }>
}

interface ShortageReportItem {
  order_no: string
  order_id: number
  shortage_items: ShortageItemEnhanced[]
}

interface ShortageItemEnhanced extends ShortageItem {
  latest_purchase_date?: string
  urgency_level?: 'critical' | 'urgent' | 'normal' | 'relaxed'
  urgency_label?: string
  recommended_action?: string
  recommended_supplier?: string
  // v4-BOM 替代料字段
  original_shortage?: number
  alternative_materials?: Array<{
    original_material: string
    substitute_material: string
    substitute_name: string
    quantity: number
    priority: number
    strategy: string
    reason: string
  }>
}

const loading = ref(false)
const planningLoading = ref(false)
const planningProgress = ref(0)
const planningElapsedSeconds = ref(0)
const planningEstimatedSeconds = ref(600)
const planningBackgroundMode = ref(false)
const planningTimer = ref<number | null>(null)
const hasPlanned = ref(false)
const selectedStrategy = ref('delivery_first')
const enableAIAnalysis = ref(false)
// 页面内联状态提示（不依赖 ElMessage，确保可见）
const planningStatusText = ref('')
const planningStatusType = ref<'success' | 'error' | 'warning' | ''>('')
let statusAutoHideTimer: ReturnType<typeof setTimeout> | null = null

/** 显示状态提示条（5秒后自动消失） */
function showStatus(type: 'success' | 'error' | 'warning', msg: string) {
  // 清除之前的自动消失定时器
  if (statusAutoHideTimer) clearTimeout(statusAutoHideTimer)
  planningStatusType.value = type
  planningStatusText.value = msg
  // 20秒后自动淡出消失
  statusAutoHideTimer = setTimeout(() => {
    planningStatusText.value = ''
    planningStatusType.value = ''
  }, 20000)
}

const strategyOptions = [
  { label: '交付优先', value: 'delivery_first' },
  { label: '库存优先', value: 'inventory_first' },
  { label: '供应商优先', value: 'supplier_first' },
  { label: '稳定优先', value: 'stability_first' },
  { label: '成本优先', value: 'cost_first' },
  { label: '临期优先', value: 'expiry_first' }
]

const summary = ref<PlanningSummary>({
  total_orders: 0,
  complete_orders: 0,
  partial_orders: 0,
  pending_orders: 0,
  avg_complete_rate: 0,
  complete_rate: 0,
  total_shortage_orders: 0,
  total_promise_changes: 0,
  stable_orders: 0,
  avg_supplier_reliability: 0,
  total_safety_stock_usage: 0,
  failure_analysis: { total_failed: 0, by_reason: {}, details: {} }
})

const planData = ref<MaterialPlanDetail[]>([])
const shortageReport = ref<ShortageReportItem[]>([])
const aiAnalysis = ref<AIAnalysis | null>(null)
const rootCauseData = ref<RootCauseAnalysis[]>([])
const planLogs = ref<PlanLog[]>([])
const releaseRecords = ref<ReleaseRecord[]>([])

// 缺料报表弹窗 + 分页
const shortageDialogVisible = ref(false)
const shortagePageSize = 15
const shortageCurrentPage = ref(1)

// 扁平化缺料数据（按订单分组展开为平铺列表）
interface FlatShortageItem {
  order_no: string
  order_id: number
  material_id: number | string
  material_code: string
  material_name: string
  required: number
  allocated: number
  shortage: number
  latest_purchase_date: string | null | undefined
  urgency_level: string
  urgency_label: string
  recommended_action: string | null | undefined
  recommended_supplier: string | null | undefined
}

const flatShortageList = computed<FlatShortageItem[]>(() => {
  const list: FlatShortageItem[] = []
  for (const group of shortageReport.value) {
    for (const item of group.shortage_items) {
      list.push({
        order_no: group.order_no,
        order_id: group.order_id,
        material_id: item.material_id,
        material_code: item.material_code,
        material_name: item.material_name,
        required: item.required,
        allocated: item.allocated,
        shortage: item.shortage,
        latest_purchase_date: item.latest_purchase_date,
        urgency_level: item.urgency_level || 'normal',
        urgency_label: item.urgency_label || '',
        recommended_action: item.recommended_action,
        recommended_supplier: item.recommended_supplier
      })
    }
  }
  return list
})

const shortageTotal = computed(() => flatShortageList.value.length)
const paginatedShortageList = computed<FlatShortageItem[]>(() => {
  const start = (shortageCurrentPage.value - 1) * shortagePageSize
  return flatShortageList.value.slice(start, start + shortagePageSize)
})

const openShortageDialog = () => {
  shortageCurrentPage.value = 1
  shortageDialogVisible.value = true
}

// 物料需求明细弹窗 + 分页
const planDataDialogVisible = ref(false)
const planDataPageSize = 15
const planDataCurrentPage = ref(1)

const planDataTotal = computed(() => planData.value.length)
const paginatedPlanData = computed<MaterialPlanDetail[]>(() => {
  const start = (planDataCurrentPage.value - 1) * planDataPageSize
  return planData.value.slice(start, start + planDataPageSize)
})

const openPlanDataDialog = () => {
  planDataCurrentPage.value = 1
  planDataDialogVisible.value = true
}

const planningProgressText = computed(() => {
  const minutes = Math.floor(planningElapsedSeconds.value / 60)
  const seconds = planningElapsedSeconds.value % 60
  const elapsed = minutes > 0 ? `${minutes}分${seconds.toString().padStart(2, '0')}秒` : `${seconds}秒`
  const estimate = Math.ceil(planningEstimatedSeconds.value / 60)
  return `已执行 ${elapsed}，预计首次执行约 ${estimate} 分钟`
})

const stopPlanningTimer = () => {
  if (planningTimer.value !== null) {
    window.clearInterval(planningTimer.value)
    planningTimer.value = null
  }
}

const startPlanningTimer = (isFirstRun: boolean) => {
  stopPlanningTimer()
  planningProgress.value = 3
  planningElapsedSeconds.value = 0
  planningEstimatedSeconds.value = isFirstRun ? 600 : 180
  planningTimer.value = window.setInterval(() => {
    planningElapsedSeconds.value += 1
    const ratio = planningElapsedSeconds.value / Math.max(planningEstimatedSeconds.value, 1)
    planningProgress.value = Math.min(95, Math.max(3, Math.round(ratio * 95)))
  }, 1000)
}

const continuePlanningInBackground = () => {
  planningBackgroundMode.value = true
  ElMessage.info('物料计划已在后台继续执行，可稍后返回本页查看结果')
  router.push('/screen')
}

const handleRunPlanning = async () => {
  const strategyLabel = strategyOptions.find(s => s.value === selectedStrategy.value)?.label || selectedStrategy.value
  const isFirstPlanningRun = !hasPlanned.value || planDataTotal.value === 0
  try {
    await ElMessageBox.confirm(
      `确定使用「${strategyLabel}」执行物料计划？\n\n系统将依次执行以下步骤：\nBOM展开 → 库存分配 → 缺料计算 → BOM智能替换 → 让料检查 → 产能验证${enableAIAnalysis.value ? ' → 智能分析' : ''}`,
      '执行确认',
      { confirmButtonText: '确定执行', cancelButtonText: '取消', type: 'info' }
    )
  } catch {
    return
  }

  planningLoading.value = true
  planningBackgroundMode.value = false
  hasPlanned.value = false
  startPlanningTimer(isFirstPlanningRun)

  try {
    const res = await runPlanning(undefined, selectedStrategy.value, enableAIAnalysis.value)
    planningProgress.value = 100
    // 无论后端返回什么格式，只要请求成功就提示
    const msg = res?.message || (res?.status === 'skipped' ? '无待处理订单，已跳过' : '物料计划执行成功')
    showStatus('success', msg)

    hasPlanned.value = true
    // 延迟加载数据：确保状态提示先渲染完成
    setTimeout(() => {
      loadAllData().catch((e) => console.warn('[MRP] 刷新数据失败:', e))
    }, 300)
  } catch (error: any) {
    console.error('[MRP] runPlanning 失败:', error?.message, error?.response?.status)
    const detail = error?.response?.data?.detail
    if (detail) {
      showStatus('error', String(detail))
    } else if (error?.message?.includes('timeout') || error?.code === 'ECONNABORTED') {
      showStatus('warning', '物料计划计算时间较长，结果将在后台继续处理')
      hasPlanned.value = true
    } else if (error?.message?.includes('cancelled') || error?.message?.includes('Duplicate')) {
      // 请求被去重取消，忽略
    } else {
      showStatus('error', error?.message || '物料计划执行失败，请重试')
    }
  } finally {
    stopPlanningTimer()
    planningLoading.value = false
  }
}

const getRootCauseAnalysis = async () => {
  try {
    const res = await request.get<any>('/root_cause_analysis/', { showLoading: false, timeout: 300000 })
    // 后端可能返回 {success, root_cause_analysis, ...} 或 null/400错误
    if (res && res.root_cause_analysis) return res
    if (Array.isArray(res)) return { root_cause_analysis: res }
    return null
  } catch {
    return null
  }
}

const getPlanLogs = async () => {
  try {
    const res = await request.get<any>('/plan_logs/', { showLoading: false, timeout: 300000 })
    // 后端返回 {success, data: [...], total: N}，需要提取 data 数组
    if (res && Array.isArray(res.data)) return res.data
    if (Array.isArray(res)) return res
    return []
  } catch {
    return []
  }
}

const loadAllData = async () => {
  loading.value = true
  try {
    const currentStrategy = selectedStrategy.value
    const [summaryRes, planRes, reportRes, aiRes, logsRes] = await Promise.all([
      getPlanningSummary(currentStrategy),
      getMaterialPlanDetail(currentStrategy),
      getShortageReport(currentStrategy),
      getRootCauseAnalysis(),
      getPlanLogs()
    ])
    // 安全赋值：确保每个字段都有兜底值
    summary.value = (summaryRes && typeof summaryRes === 'object') ? summaryRes : {
      total_orders: 0, complete_orders: 0, partial_orders: 0, pending_orders: 0,
      avg_complete_rate: 0, complete_rate: 0, total_shortage_orders: 0,
      total_promise_changes: 0, stable_orders: 0, avg_supplier_reliability: 0,
      total_safety_stock_usage: 0,
      failure_analysis: { total_failed: 0, by_reason: {}, details: {} },
      release_records: [], delivery_violations: [],
      ai_analysis: null, procurement_plan: null
    } as any
    planData.value = Array.isArray(planRes) ? planRes : []  // 不截断，后端已返回完整数据
    aiAnalysis.value = (aiRes && typeof aiRes === 'object') ? aiRes : null
    if (aiRes && aiRes.root_cause_analysis) {
      rootCauseData.value = Array.isArray(aiRes.root_cause_analysis) ? aiRes.root_cause_analysis.slice(0, 200) : []
    } else {
      rootCauseData.value = []
    }
    planLogs.value = Array.isArray(logsRes) ? logsRes.slice(0, 100) : []

    // 缺料报表：安全处理（不截断，后端已返回完整数据）
    const reportFlat: any[] = Array.isArray(reportRes) ? reportRes : []
    const grouped: Record<string, ShortageReportItem> = {}
    reportFlat.forEach((item: any) => {
      if (!item || typeof item !== 'object') return
      const key = item.order_no || `unknown_${Math.random().toString(36).slice(2, 8)}`
      if (!grouped[key]) {
        grouped[key] = { order_no: key, order_id: item.order_id || 0, shortage_items: [] as ShortageItemEnhanced[] }
      }
      grouped[key].shortage_items.push({
        material_id: item.material_id || item.material_code || 0,
        material_code: item.material_code || '',
        material_name: item.material_name || '',
        required: Number(item.required) || 0,
        allocated: Number(item.allocated) || 0,
        shortage: Number(item.shortage) || 0,
        latest_purchase_date: item.latest_purchase_date || null,
        urgency_level: item.urgency_level || 'normal',
        urgency_label: item.urgency_label || '',
        recommended_action: item.recommended_action || null,
        recommended_supplier: item.recommended_supplier || null
      })
    })
    shortageReport.value = Object.values(grouped)

    // 让料记录
    if (summaryRes && Array.isArray(summaryRes.release_records)) {
      releaseRecords.value = summaryRes.release_records.slice(0, 200) as any
    } else {
      releaseRecords.value = []
    }
  } catch (error) {
    console.error('加载数据失败:', error)
    ElMessage.error('数据加载失败，请刷新重试')
    // 出错时重置所有数据为空，避免渲染异常数据导致崩溃
    planData.value = []
    shortageReport.value = []
    rootCauseData.value = []
    planLogs.value = []
    releaseRecords.value = []
  } finally {
    loading.value = false
  }
}

const refreshLogs = async () => {
  const logs = await getPlanLogs()
  planLogs.value = Array.isArray(logs) ? logs : []
  ElMessage.success('日志已刷新')
}

// 切换策略时，如果已执行过计划则自动重新加载该策略的数据
watch(selectedStrategy, () => {
  if (hasPlanned.value) {
    loadAllData()
  }
})

// 页面挂载时：乐观UI策略 — 立即展示页面框架，数据后台异步填充
// 重要：如果正在执行计划中（planningLoading=true），不要覆盖 hasPlanned 状态，
// 否则会导致三个 v-if 条件同时为 false，页面变成空白！
onMounted(async () => {
  // 仅在非加载状态下才标记为已计划（让页面内容区域立刻渲染）
  if (!planningLoading.value) {
    hasPlanned.value = true
  }
  // 后台加载数据（无论成功失败都不影响页面框架展示）
  loadAllDataInBackground()
})

onUnmounted(() => {
  stopPlanningTimer()
  if (statusAutoHideTimer) clearTimeout(statusAutoHideTimer)
})

// KeepAlive 激活时：从其他页面返回本页，重置后台模式让进度条重新显示
onActivated(() => {
  if (planningLoading.value) {
    // 计划还在执行中（可能在后台运行），恢复进度条显示
    planningBackgroundMode.value = false
  }
})

/** 后台静默加载数据（不显示全屏loading） */
const loadAllDataInBackground = async () => {
  try {
    const currentStrategy = selectedStrategy.value
    const [summaryRes, planRes, reportRes, aiRes, logsRes] = await Promise.all([
      getPlanningSummary(currentStrategy),
      getMaterialPlanDetail(currentStrategy),
      getShortageReport(currentStrategy),
      getRootCauseAnalysis(),
      getPlanLogs()
    ])
    // 安全赋值
    summary.value = (summaryRes && typeof summaryRes === 'object') ? summaryRes : summary.value
    planData.value = Array.isArray(planRes) ? planRes : []  // 不截断，返回全部物料明细数据
    aiAnalysis.value = (aiRes && typeof aiRes === 'object') ? aiRes : null
    if (aiRes && aiRes.root_cause_analysis) {
      rootCauseData.value = Array.isArray(aiRes.root_cause_analysis) ? aiRes.root_cause_analysis.slice(0, 200) : []
    } else {
      rootCauseData.value = []
    }
    planLogs.value = Array.isArray(logsRes) ? logsRes.slice(0, 100) : []

    const reportFlat: any[] = Array.isArray(reportRes) ? reportRes : []
    const grouped: Record<string, ShortageReportItem> = {}
    reportFlat.forEach((item: any) => {
      if (!item || typeof item !== 'object') return
      const key = item.order_no || `unknown_${Math.random().toString(36).slice(2, 8)}`
      if (!grouped[key]) {
        grouped[key] = { order_no: key, order_id: item.order_id || 0, shortage_items: [] as ShortageItemEnhanced[] }
      }
      grouped[key].shortage_items.push({
        material_id: item.material_id || item.material_code || 0,
        material_code: item.material_code || '',
        material_name: item.material_name || '',
        required: Number(item.required) || 0,
        allocated: Number(item.allocated) || 0,
        shortage: Number(item.shortage) || 0,
        latest_purchase_date: item.latest_purchase_date || null,
        urgency_level: item.urgency_level || 'normal',
        urgency_label: item.urgency_label || '',
        recommended_action: item.recommended_action || null,
        recommended_supplier: item.recommended_supplier || null
      })
    })
    shortageReport.value = Object.values(grouped)

    if (summaryRes && Array.isArray(summaryRes.release_records)) {
      releaseRecords.value = summaryRes.release_records.slice(0, 200) as any
    } else {
      releaseRecords.value = []
    }
  } catch (error) {
    console.error('后台加载数据失败:', error)
    // 静默失败，不影响已展示的摘要数据
  }
}

const completeRate = computed(() => {
  const total = Math.max(summary.value.total_orders, 1)
  return {
    complete: Number((summary.value.complete_orders / total) * 100).toFixed(1),
    partial: Number((summary.value.partial_orders / total) * 100).toFixed(1),
    pending: Number((summary.value.pending_orders / total) * 100).toFixed(1)
  }
})

// 紧急缺料数：优先使用后端统计的真实值，其次从前端物料数据计算
const totalCriticalShortages = computed(() => {
  // 后端summary中的值是订单维度统计（权威）
  const backendCount = (summary.value as any)?.total_critical_shortages
  if (backendCount != null && backendCount > 0) {
    return backendCount
  }
  // 回退到物料明细中的实际紧急缺料数
  return planData.value.filter(p => p.urgency_level === 'critical').length
})

// 分配质量分：优先使用AI分析结果，否则从真实物料数据计算，不允许返回虚假的0
const allocationQualityScore = computed(() => {
  // 优先使用后端AI分析的真实值
  if (aiAnalysis.value?.allocation_quality != null && aiAnalysis.value.allocation_quality > 0) {
    return Math.round(aiAnalysis.value.allocation_quality)
  }
  // 无AI分析时，从物料需求数据实时计算（总满足量 / 总需求量 * 100）
  if (planData.value.length > 0) {
    let totalDemand = 0
    let totalSatisfied = 0
    for (const item of planData.value) {
      const demand = Number(item.demand) || 0
      const stock = Number(item.stock) || 0
      totalDemand += demand
      totalSatisfied += Math.min(stock, demand) // 实际可满足量取min(库存,需求)
    }
    if (totalDemand > 0) {
      return Math.round((totalSatisfied / totalDemand) * 100)
    }
  }
  // 确实无数据时返回null，前端显示"-"而非"0%"
  return null as unknown as number
})

const getStatusLabel = (status: string) => {
  const map: Record<string, string> = { shortage: '缺料', warning: '预警', sufficient: '充足' }
  return map[status] || '-'
}

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

const getStatusType = (status: string): TagType => {
  const map: Record<string, TagType> = { shortage: 'danger', warning: 'warning', sufficient: 'success' }
  return map[status] || 'info'
}

const getPriorityLabel = (priority: string) => {
  const map: Record<string, string> = { high: '高', normal: '中', low: '低' }
  return map[priority] || '普通'
}

const getPriorityType = (priority: string): TagType => {
  const map: Record<string, TagType> = { high: 'danger', normal: 'warning', low: 'info' }
  return map[priority] || 'info'
}

const getUrgencyType = (level: string): TagType => {
  const map: Record<string, TagType> = { critical: 'danger', urgent: 'warning', normal: 'info', relaxed: 'success' }
  return map[level] || 'info'
}

const getUrgencyLabel = (level: string) => {
  const map: Record<string, string> = { critical: '紧急', urgent: '加急', normal: '正常', relaxed: '宽松' }
  return map[level] || '正常'
}

const formatDate = (dateStr?: string) => {
  if (!dateStr) return '-'
  const d = new Date(dateStr)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const formatTime = (dateStr: string) => {
  if (!dateStr) return '-'
  const d = new Date(dateStr)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

const getImpactType = (impact: string): TagType => {
  const map: Record<string, TagType> = { high: 'danger', medium: 'warning', low: 'info' }
  return map[impact] || 'info'
}

const getImpactLabel = (impact: string) => {
  const map: Record<string, string> = { high: '高', medium: '中', low: '低' }
  return map[impact] || '低'
}

const getLogTypeTag = (type: string): TagType => {
  const map: Record<string, TagType> = { INFO: 'success', WARNING: 'warning', ERROR: 'danger', PLANNING: 'primary' }
  return map[type] || 'info'
}

const tableRowClassName = ({ row }: { row: MaterialPlanDetail }) => {
  return row.urgency_level === 'critical' ? 'danger-row' : ''
}

const getBreakdownLabel = (key: string): string => {
  const map: Record<string, string> = {
    utilization_score: '库存利用率',
    expiry_factor: '临期因子',
    supplier_factor: '供应商因子',
    stagnation_factor: '滞留风险',
    diversity_bonus: '多样性加分'
  }
  return map[key] || '其他'
}

const getProcurementTypeColor = (type: string): 'primary' | 'success' | 'warning' | 'info' | 'danger' => {
  const map: Record<string, 'primary' | 'success' | 'warning' | 'info' | 'danger'> = {
    urgent_采购: 'danger',
    expedite: 'danger',
    normal: 'info',
    alternative: 'warning',
    batch_merge: 'success'
  }
  return map[type] || 'info'
}

const getProcurementTypeLabel = (type: string): string => {
  const map: Record<string, string> = {
    urgent_采购: '紧急采购',
    expedite: '加急采购',
    normal: '常规采购',
    alternative: '替代方案',
    batch_merge: '合并批次'
  }
  return map[type] || '其他'
}

const getCategoryLabel = (category: string): string => {
  const map: Record<string, string> = {
    supply_shortage: '供应短缺',
    quality_issue: '质量问题',
    demand_change: '需求变更',
    capacity_limit: '产能限制',
    logistics_delay: '物流延迟',
    inventory_issue: '库存问题',
    supplier_issue: '供应商问题',
    production_issue: '生产问题'
  }
  return map[category] || '其他'
}

const getLogColor = (type: string): string => {
  const map: Record<string, string> = { INFO: '#67c23a', WARNING: '#e6a23c', ERROR: '#f56c6c', PLANNING: '#409eff' }
  return map[type] || '#409eff'
}
</script>

<template>
  <div class="plan-page">
    <div class="page-header">
      <div>
        <h1 class="page-title">物料计划</h1>
        <p class="page-desc">基于多约束优化的智能物料计划系统</p>
      </div>
      <div class="header-actions">
        <el-tooltip content="选择策略后需点击「执行物料计划」生效" placement="bottom">
          <el-select v-model="selectedStrategy" class="strategy-select" placeholder="选择策略">
            <el-option
              v-for="item in strategyOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-tooltip>
        <div class="ai-toggle">
          <span class="toggle-label">智能分析</span>
          <el-switch
            v-model="enableAIAnalysis"
            active-text="开启"
            inactive-text="关闭"
            size="small"
          />
        </div>
        <el-button
          type="primary"
          :icon="VideoPlay"
          size="large"
          class="run-btn"
          :loading="planningLoading"
          @click="handleRunPlanning"
        >
          {{ planningLoading ? '正在执行...' : '执行物料计划' }}
        </el-button>
        <el-button
          v-if="hasPlanned"
          :icon="Refresh"
          size="large"
          class="refresh-btn"
          :loading="loading"
          @click="loadAllData"
        >
          刷新数据
        </el-button>
      </div>
    </div>

    <!-- 内联状态提示：不依赖 ElMessage，确保执行结果始终可见 -->
    <div v-if="planningStatusText" class="inline-status-bar" :class="'status-' + planningStatusType">
      <span class="status-icon">{{ planningStatusType === 'error' ? '✗' : planningStatusType === 'warning' ? '⏳' : '✓' }}</span>
      <span>{{ planningStatusText }}</span>
      <button class="status-close" @click="planningStatusText = ''">✕</button>
    </div>

    <div v-if="planningLoading && !planningBackgroundMode" class="progress-section">
      <el-alert title="正在执行物料计划计算" type="info" show-icon :closable="false">
        <template #default>
          <div class="progress-content">
            <div class="progress-copy">
              <div class="progress-title">首次执行可能需要约 10 分钟，系统会完成 BOM 展开、库存分配、缺料计算和产能验证。</div>
              <div class="progress-subtitle">{{ planningProgressText }}</div>
            </div>
            <el-progress
              :percentage="planningProgress"
              :stroke-width="12"
              striped
              striped-flow
              :duration="12"
              status="success"
            />
            <div class="progress-actions">
              <el-button :icon="Connection" @click="continuePlanningInBackground">
                后台继续并返回总览
              </el-button>
            </div>
          </div>
        </template>
      </el-alert>
    </div>

    <div v-if="!hasPlanned && !planningLoading" class="empty-state">
      <el-icon class="empty-icon"><DataAnalysis /></el-icon>
      <p class="empty-text">点击上方「执行物料计划」按钮开始计划运算</p>
      <p class="empty-subtext">系统将自动分析所有订单并生成物料需求与缺料报告</p>
      <el-button type="primary" :icon="VideoPlay" size="large" class="empty-run-btn" @click="handleRunPlanning">
        立即执行
      </el-button>
    </div>

    <template v-if="hasPlanned && !planningLoading">
      <el-row :gutter="16" class="stats-row">
        <el-col :xs="12" :sm="8" :md="4" class="stat-col">
          <el-card shadow="never" class="kpi-card">
            <div class="kpi-body">
              <div class="kpi-icon primary">
                <el-icon><Box /></el-icon>
              </div>
              <div class="kpi-info">
                <span class="kpi-value">{{ summary.total_orders }}</span>
                <span class="kpi-label">总订单数</span>
              </div>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4" class="stat-col">
          <el-card shadow="never" class="kpi-card">
            <div class="kpi-body">
              <div class="kpi-icon success">
                <el-icon><Check /></el-icon>
              </div>
              <div class="kpi-info">
                <span class="kpi-value">{{ summary.complete_orders }}</span>
                <span class="kpi-label">完全齐套</span>
              </div>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4" class="stat-col">
          <el-card shadow="never" class="kpi-card">
            <div class="kpi-body">
              <div class="kpi-icon warning">
                <el-icon><Warning /></el-icon>
              </div>
              <div class="kpi-info">
                <span class="kpi-value">{{ summary.partial_orders }}</span>
                <span class="kpi-label">部分齐套</span>
              </div>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4" class="stat-col">
          <el-card shadow="never" class="kpi-card">
            <div class="kpi-body">
              <div class="kpi-icon danger">
                <el-icon><CircleClose /></el-icon>
              </div>
              <div class="kpi-info">
                <span class="kpi-value">{{ summary.pending_orders }}</span>
                <span class="kpi-label">未齐套</span>
              </div>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4" class="stat-col">
          <el-card shadow="never" class="kpi-card kpi-critical">
            <div class="kpi-body">
              <div class="kpi-icon danger pulse-icon-wrapper">
                <el-icon><BellFilled /></el-icon>
              </div>
              <div class="kpi-info">
                <span class="kpi-value text-critical">{{ totalCriticalShortages }}</span>
                <span class="kpi-label">紧急缺料</span>
              </div>
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4" class="stat-col">
          <el-card shadow="never" class="kpi-card">
            <div class="kpi-body">
              <div class="kpi-icon primary">
                <el-icon><TrendCharts /></el-icon>
              </div>
              <div class="kpi-info">
                <span class="kpi-value">{{ allocationQualityScore != null ? `${allocationQualityScore}%` : '-' }}</span>
                <span class="kpi-label">分配质量分</span>
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- v4-BOM: 替换统计横幅（有替换数据时显示） -->
      <el-alert
        v-if="summary.substitution_applied && summary.substitution_stats"
        type="success"
        :closable="false"
        show-icon
        class="bom-substitution-banner"
      >
        <template #title>
          <span class="bom-banner-title">BOM智能替换生效</span>
        </template>
        <template #default>
          <span class="bom-banner-detail">
            检查 {{ summary.substitution_stats.checked }} 条缺料记录，
            找到 {{ summary.substitution_stats.found }} 条可替代，
            成功替换 {{ summary.substitution_stats.applied }} 次，
            累计减少缺料
            <strong class="text-saving">{{ Math.round(summary.substitution_stats.shortage_reduced || 0).toLocaleString() }} 件</strong>，
            影响 {{ summary.substitution_stats.orders_affected }} 个订单
          </span>
        </template>
      </el-alert>

      <el-card shadow="never" class="section-card">
        <template #header>
          <div class="card-header">
            <el-icon><TrendCharts /></el-icon>
            <span>物料需求明细</span>
            <el-tag type="primary" size="small" effect="dark" round>{{ planDataTotal }} 条记录</el-tag>
            <el-button
              v-if="planDataTotal > 0"
              type="primary"
              size="small"
              round
              @click="openPlanDataDialog"
            >
              查看详情
            </el-button>
          </div>
        </template>
        <!-- 摘要：显示前5条预览 -->
        <div v-if="planDataTotal > 0" class="shortage-summary">
          <div v-for="(item, idx) in paginatedPlanData.slice(0, 5)" :key="idx" class="summary-row">
            <span class="summary-order">{{ item.material_code }}</span>
            <span class="summary-material">{{ item.material_name }}</span>
            <el-tag
              :type="getUrgencyType(item.urgency_level || 'normal')"
              size="small"
            >
              {{ item.urgency_label || getUrgencyLabel(item.urgency_level || 'normal') }}
            </el-tag>
            <span class="summary-shortage text-danger">缺 {{ Math.round(Number(item.shortage || 0)).toLocaleString() }}</span>
            <el-tag :type="getStatusType(item.status)" size="small">{{ getStatusLabel(item.status) }}</el-tag>
          </div>
          <div v-if="planDataTotal > 5" class="summary-more">
            还有 {{ planDataTotal - 5 }} 条记录，点击「<span class="summary-more-link" @click="openPlanDataDialog">查看详情</span>」查看全部
          </div>
        </div>
        <el-empty v-else description="暂无物料需求数据" :image-size="60" />
      </el-card>

      <!-- 物料需求明细弹窗（分页表格） -->
      <el-dialog
        v-model="planDataDialogVisible"
        width="95%"
        destroy-on-close
        class="plan-data-dialog"
      >
        <template #header>
          <span class="dialog-center-title">物料需求明细</span>
        </template>
        <el-table border stripe :data="paginatedPlanData" size="default" class="plan-data-detail-table" :row-class-name="tableRowClassName">
          <el-table-column prop="material_code" label="物料代码" width="100" show-overflow-tooltip />
          <el-table-column prop="material_name" label="物料名称" min-width="140" show-overflow-tooltip />
          <el-table-column prop="demand" label="需求量" width="90" align="right">
            <template #default="{ row }">{{ Math.round(Number(row.demand || 0)).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column prop="stock" label="当前库存" width="90" align="right">
            <template #default="{ row }">{{ Math.round(Number(row.stock || 0)).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column prop="shortage" label="缺料量" min-width="180" align="right">
            <template #default="{ row }">
              <div class="shortage-cell">
                <span :class="{ 'text-danger': Number(row.shortage || 0) > 0 }">{{ Math.round(Number(row.shortage || 0)).toLocaleString() }}</span>
                <!-- v4-BOM: 替换减少量 -->
                <span v-if="row.original_shortage && row.original_shortage > row.shortage + 1"
                  class="bom-reduced-text"
                  :title="'BOM替换前原缺料: ' + Math.round(row.original_shortage).toLocaleString() + '件'">
                  -{{ Math.round(row.original_shortage - row.shortage).toLocaleString() }}
                </span>
                <!-- v4-BOM: 替代料内联文本 -->
                <template v-if="row.alternative_materials && row.alternative_materials.length > 0">
                  <span class="alt-sep">→</span>
                  <template v-for="(alt, _ai) in row.alternative_materials.slice(0, 2)" :key="_ai">
                    <span class="alt-mat-text"
                      :title="`${alt.substitute_name || alt.substitute_material} (P${alt.priority}) 可补${Math.round(alt.quantity || 0).toLocaleString()}件`">
                      {{ alt.substitute_material }}
                    </span>
                  </template>
                  <span v-if="row.alternative_materials.length > 2" class="alt-more-text">+{{ row.alternative_materials.length - 2 }}</span>
                </template>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="safety_stock" label="安全库存" width="85" align="right">
            <template #default="{ row }">{{ Math.round(Number(row.safety_stock || 0)).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column prop="latest_purchase_date" label="最晚采购日期" width="115" align="center">
            <template #default="{ row }">{{ formatDate(row.latest_purchase_date) }}</template>
          </el-table-column>
          <el-table-column label="紧急程度" width="85" align="center">
            <template #default="{ row }">
              <el-tag
                v-if="row.urgency_level"
                :type="getUrgencyType(row.urgency_level)"
                size="small"
                :class="{ 'urgency-pulse': row.urgency_level === 'critical' }"
              >
                {{ row.urgency_label || getUrgencyLabel(row.urgency_level) }}
              </el-tag>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column label="推荐行动" min-width="260" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.recommended_action" :class="{ 'action-critical': row.urgency_level === 'critical' }">
                {{ row.recommended_action }}
              </span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="65" align="center">
            <template #default="{ row }">
              <el-tag :type="getStatusType(row.status)" size="small">{{ getStatusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="优先级" width="65" align="center">
            <template #default="{ row }">
              <el-tag :type="getPriorityType(row.priority)" size="small">{{ getPriorityLabel(row.priority) }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
        <div class="dialog-pagination">
          <el-pagination
            v-model:current-page="planDataCurrentPage"
            :page-size="planDataPageSize"
            :total="planDataTotal"
            layout="total, prev, pager, next, jumper"
            small
            background
          />
        </div>
      </el-dialog>

      <el-card shadow="never" class="section-card">
        <template #header>
          <div class="card-header">
            <el-icon><Warning /></el-icon>
            <span>缺料报表</span>
            <el-tag type="danger" size="small" effect="dark" round>{{ shortageTotal }} 条缺料记录</el-tag>
            <el-button
              v-if="shortageTotal > 0"
              type="danger"
              size="small"
              round
              @click="openShortageDialog"
            >
              查看详情
            </el-button>
          </div>
        </template>
        <!-- 摘要：显示前5条预览 -->
        <div v-if="shortageTotal > 0" class="shortage-summary">
          <div v-for="(item, idx) in paginatedShortageList.slice(0, 5)" :key="idx" class="summary-row">
            <span class="summary-order">{{ item.order_no }}</span>
            <span class="summary-material">{{ item.material_code }} {{ item.material_name }}</span>
            <el-tag :type="getUrgencyType(item.urgency_level)" size="small">{{ item.urgency_label || getUrgencyLabel(item.urgency_level) }}</el-tag>
            <span class="summary-shortage text-danger">缺 {{ Math.round(item.shortage) }}</span>
          </div>
          <div v-if="shortageTotal > 5" class="summary-more">
            还有 {{ shortageTotal - 5 }} 条记录，点击「<span class="summary-more-link" @click="openShortageDialog">查看详情</span>」查看全部
          </div>
        </div>
        <el-empty v-else description="暂无缺料信息" :image-size="60" />
      </el-card>

      <!-- 缺料报表弹窗（分页表格） -->
      <el-dialog
        v-model="shortageDialogVisible"
        width="95%"
        destroy-on-close
        class="shortage-dialog"
      >
        <template #header>
          <span class="dialog-center-title">缺料报表明细</span>
        </template>
        <el-table border stripe :data="paginatedShortageList" size="default" class="shortage-detail-table">
          <el-table-column prop="order_no" label="订单号" width="150" show-overflow-tooltip fixed />
          <el-table-column prop="material_code" label="物料代码" width="100" show-overflow-tooltip />
          <el-table-column prop="material_name" label="物料名称" min-width="130" show-overflow-tooltip />
          <el-table-column prop="required" label="需求数量" width="90" align="right">
            <template #default="{ row }">{{ Math.round(Number(row.required || 0)).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column prop="allocated" label="已分配" width="80" align="right">
            <template #default="{ row }">{{ Math.round(Number(row.allocated || 0)).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column prop="shortage" label="缺料数量" min-width="180" align="right">
            <template #default="{ row }">
              <div class="shortage-cell">
                <span class="text-danger font-bold">{{ Math.round(Number(row.shortage || 0)).toLocaleString() }}</span>
                <!-- v4-BOM: 替换减少量 -->
                <span v-if="row.original_shortage && row.original_shortage > row.shortage + 1"
                  class="bom-reduced-text"
                  :title="'BOM替换前原缺料: ' + Math.round(row.original_shortage).toLocaleString() + '件'">
                  -{{ Math.round(row.original_shortage - row.shortage).toLocaleString() }}
                </span>
                <!-- v4-BOM: 替代料内联 -->
                <template v-if="row.alternative_materials && row.alternative_materials.length > 0">
                  <span class="alt-sep">→</span>
                  <template v-for="(alt, _ai2) in row.alternative_materials.slice(0, 2)" :key="_ai2">
                    <span class="alt-mat-text"
                      :title="`${alt.substitute_name || alt.substitute_material} 可补${Math.round(alt.quantity || 0).toLocaleString()}件`">
                      {{ alt.substitute_material }}
                    </span>
                  </template>
                  <span v-if="row.alternative_materials.length > 2" class="alt-more-text">+{{ row.alternative_materials.length - 2 }}</span>
                </template>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="最晚采购日期" width="115" align="center">
            <template #default="{ row }">{{ formatDate(row.latest_purchase_date) }}</template>
          </el-table-column>
          <el-table-column label="紧急程度" width="85" align="center">
            <template #default="{ row }">
              <el-tag
                v-if="row.urgency_level"
                :type="getUrgencyType(row.urgency_level)"
                size="small"
                :class="{ 'urgency-pulse': row.urgency_level === 'critical' }"
              >
                {{ row.urgency_label || getUrgencyLabel(row.urgency_level) }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="推荐行动" min-width="260" show-overflow-tooltip>
            <template #default="{ row }">
              <span :class="{ 'action-critical': row.urgency_level === 'critical' }">
                {{ row.recommended_action || '-' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="recommended_supplier" label="推荐供应商" min-width="120" show-overflow-tooltip>
            <template #default="{ row }">{{ row.recommended_supplier || '-' }}</template>
          </el-table-column>
        </el-table>
        <div class="dialog-pagination">
          <el-pagination
            v-model:current-page="shortageCurrentPage"
            :page-size="shortagePageSize"
            :total="shortageTotal"
            layout="total, prev, pager, next, jumper"
            small
            background
          />
        </div>
      </el-dialog>

      <el-card shadow="never" class="section-card">
        <template #header>
          <div class="card-header">
            <el-icon><TrendCharts /></el-icon>
            <span>齐套率分析</span>
          </div>
        </template>
        <div class="rate-analysis">
          <div class="rate-item">
            <div class="rate-header">
              <span class="rate-label">完全齐套率</span>
              <span class="rate-percent success">{{ completeRate.complete }}%</span>
            </div>
            <el-progress
              :percentage="Number(completeRate.complete)"
              :stroke-width="18"
              color="#67c23a"
              :show-text="false"
            />
          </div>
          <div class="rate-item">
            <div class="rate-header">
              <span class="rate-label">部分齐套率</span>
              <span class="rate-percent warning">{{ completeRate.partial }}%</span>
            </div>
            <el-progress
              :percentage="Number(completeRate.partial)"
              :stroke-width="18"
              color="#e6a23c"
              :show-text="false"
            />
          </div>
          <div class="rate-item">
            <div class="rate-header">
              <span class="rate-label">未齐套率</span>
              <span class="rate-percent danger">{{ completeRate.pending }}%</span>
            </div>
            <el-progress
              :percentage="Number(completeRate.pending)"
              :stroke-width="18"
              color="#f56c6c"
              :show-text="false"
            />
          </div>
        </div>
      </el-card>

      <el-card v-if="aiAnalysis" shadow="never" class="section-card ai-section">
        <template #header>
          <div class="card-header">
            <el-icon class="ai-icon"><Cpu /></el-icon>
            <span>智能分析</span>
          </div>
        </template>

        <div class="ai-content">
          <div class="quality-section">
            <h3 class="sub-title">分配质量评分</h3>
            <div class="quality-ring-container">
              <div class="quality-ring">
                <svg viewBox="0 0 120 120" class="ring-svg">
                  <circle cx="60" cy="60" r="52" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="10" />
                  <circle
                    cx="60" cy="60" r="52"
                    fill="none"
                    :stroke="allocationQualityScore >= 80 ? '#67c23a' : allocationQualityScore >= 60 ? '#e6a23c' : '#f56c6c'"
                    stroke-width="10"
                    stroke-linecap="round"
                    :stroke-dasharray="`${allocationQualityScore * 3.27} 327`"
                    transform="rotate(-90 60 60)"
                    style="transition: stroke-dasharray 0.8s ease"
                  />
                </svg>
                <div class="ring-score">
                  <span class="score-num">{{ allocationQualityScore }}</span>
                  <span class="score-unit">分</span>
                </div>
              </div>
              <div class="breakdown-bars" v-if="aiAnalysis?.quality_breakdown">
                <div
                  v-for="(val, key) in aiAnalysis.quality_breakdown"
                  :key="key"
                  class="breakdown-item"
                >
                  <span class="bd-label">{{ getBreakdownLabel(key) }}</span>
                  <div class="bd-bar-wrap">
                    <div class="bd-bar" :style="{ width: val + '%' }"></div>
                  </div>
                  <span class="bd-val">{{ Number(val || 0).toFixed(0) }}</span>
                </div>
              </div>
            </div>
          </div>

          <div v-if="rootCauseData.length > 0" class="root-cause-section">
            <h3 class="sub-title">根因分析</h3>
            <el-table border :data="rootCauseData" size="small" class="root-cause-table" stripe>
              <el-table-column label="类别" width="70">
                <template #default="{ row }">
                  {{ getCategoryLabel(row.category) }}
                </template>
              </el-table-column>
              <el-table-column prop="material_id" label="物料ID" width="65" align="center" show-overflow-tooltip />
              <el-table-column prop="cause" label="原因" width="110" show-overflow-tooltip />
              <el-table-column label="影响程度" width="70" align="center">
                <template #default="{ row }">
                  <el-tag :type="getImpactType(row.impact)" size="small" effect="dark">
                    {{ getImpactLabel(row.impact) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="recommendation" label="建议" width="120" show-overflow-tooltip />
            </el-table>
          </div>

          <div v-if="aiAnalysis?.procurement_recommendations?.length > 0" class="procurement-section">
            <h3 class="sub-title">采购建议</h3>
            <el-table border :data="aiAnalysis.procurement_recommendations" size="small" class="procurement-table" stripe>
              <el-table-column label="类型" width="65" align="center">
                <template #default="{ row }">
                  <el-tag :type="getProcurementTypeColor(row.type)" size="small">{{ getProcurementTypeLabel(row.type) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="material_id" label="物料ID" width="65" align="center" show-overflow-tooltip />
              <el-table-column prop="message" label="建议消息" width="140" show-overflow-tooltip />
              <el-table-column label="优先级" width="60" align="center">
                <template #default="{ row }">
                  <el-tag :type="getUrgencyType(row.priority)" size="small">{{ getUrgencyLabel(row.priority) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="预估节省" width="85" align="right">
                <template #default="{ row }">
                  <span v-if="row.estimated_saving !== undefined" class="text-saving">¥{{ Number(row.estimated_saving || 0).toFixed(2) }}</span>
                  <span v-else>-</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-card>

      <el-card v-if="releaseRecords.length > 0" shadow="never" class="section-card">
        <template #header>
          <div class="card-header">
            <el-icon><Connection /></el-icon>
            <span>让料记录</span>
            <el-tag type="warning" size="small" effect="dark" round>{{ releaseRecords.length }} 条记录</el-tag>
          </div>
        </template>
        <el-table border :data="releaseRecords" size="small" class="release-table" stripe>
          <el-table-column prop="source_order_no" label="来源订单" width="115" show-overflow-tooltip />
          <el-table-column prop="target_order_no" label="目标订单" width="115" show-overflow-tooltip />
          <el-table-column prop="material_code" label="物料代码" width="100" show-overflow-tooltip />
          <el-table-column prop="released_quantity" label="释放数量" width="80" align="right">
            <template #default="{ row }">{{ Math.round(Number(row.released_quantity || 0)).toLocaleString() }}</template>
          </el-table-column>
          <el-table-column prop="original_priority" label="原优先级" width="75" align="center" show-overflow-tooltip />
          <el-table-column prop="new_priority" label="新优先级" width="75" align="center" show-overflow-tooltip />
          <el-table-column prop="reason" label="原因" width="130" show-overflow-tooltip />
        </el-table>
      </el-card>

      <el-card shadow="never" class="section-card logs-card">
        <template #header>
          <div class="card-header">
            <el-icon><Document /></el-icon>
            <span>过程日志</span>
            <el-button size="small" :icon="Refresh" link @click="refreshLogs">刷新日志</el-button>
          </div>
        </template>
        <div v-if="planLogs.length > 0" class="logs-timeline">
          <el-timeline>
            <el-timeline-item
              v-for="log in planLogs"
              :key="log.id"
              :timestamp="formatTime(log.created_at)"
              placement="top"
              :color="getLogColor(log.log_type)"
            >
              <div class="log-entry">
                <el-tag :type="getLogTypeTag(log.log_type)" size="small" effect="dark" class="log-type-tag">
                  {{ log.log_type }}
                </el-tag>
                <span class="log-message">{{ log.message }}</span>
              </div>
            </el-timeline-item>
          </el-timeline>
        </div>
        <el-empty v-else description="暂无过程日志" :image-size="80" />
      </el-card>
    </template>
  </div>
</template>

<style scoped lang="scss">
.plan-page {
  max-width: 1500px;
  margin: 0 auto;
  /* 确保不阻挡父容器滚动 */
  position: relative;
  z-index: 1;
}

// 内联状态提示条（不依赖 ElMessage）
.inline-status-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  margin-bottom: 16px;
  border-radius: 10px;
  font-size: 15px;
  font-weight: 600;
  animation: statusSlideIn 0.3s ease-out;

  &.status-success {
    background: rgba(103, 194, 58, 0.12);
    border: 1px solid rgba(103, 194, 58, 0.35);
    color: #67c23a;
  }
  &.status-error {
    background: rgba(245, 108, 108, 0.12);
    border: 1px solid rgba(245, 108, 108, 0.35);
    color: #f56c6c;
  }
  &.status-warning {
    background: rgba(230, 162, 60, 0.12);
    border: 1px solid rgba(230, 162, 60, 0.35);
    color: #e6a23c;
  }

  .status-icon {
    font-size: 18px;
    font-weight: 800;
    width: 26px;
    height: 26px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    flex-shrink: 0;
  }
  &.status-success .status-icon { background: rgba(103, 194, 58, 0.2); }
  &.status-error .status-icon { background: rgba(245, 108, 108, 0.2); }
  &.status-warning .status-icon { background: rgba(230, 162, 60, 0.2); }

  .status-close {
    margin-left: auto;
    background: none;
    border: none;
    color: inherit;
    opacity: 0.5;
    cursor: pointer;
    font-size: 14px;
    padding: 2px 6px;
    border-radius: 4px;
    &:hover { opacity: 1; background: rgba(255,255,255,0.08); }
  }
}

@keyframes statusSlideIn {
  from { opacity: 0; transform: translateY(-8px); }
  to { opacity: 1; transform: translateY(0); }
}

// Dialog 弹窗居中标题
.dialog-center-title {
  font-size: 17px;
  font-weight: 600;
  color: #E8EAED;
}

.page-header {
  margin-bottom: 28px;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 16px;

  .page-title {
    font-size: 28px;
    font-weight: 700;
    color: #e2e8f0;
    margin: 0 0 8px 0;
  }

  .page-desc {
    font-size: 14px;
    color: #909399;
    margin: 0;
  }
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;

  .strategy-select {
    width: 150px;
  }

  .ai-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    background: rgba(255,255,255,0.06);
    border-radius: 8px;
    border: 1px solid rgba(255,255,255,0.1);

    .toggle-label {
      font-size: 13px;
      color: #a0aec0;
      white-space: nowrap;
    }
  }

  .run-btn {
    height: 44px;
    padding: 0 32px;
    font-size: 16px;
    font-weight: 600;
    border-radius: 8px;
  }

  .refresh-btn {
    height: 44px;
    border-radius: 8px;
  }
}

.progress-section {
  margin-bottom: 24px;

  :deep(.el-alert) {
    background: rgba(24, 34, 54, 0.96);
    border: 1px solid rgba(110, 158, 247, 0.22);
    box-shadow: 0 16px 36px rgba(0, 0, 0, 0.18);
  }

  :deep(.el-alert__title) {
    color: #e5edf8;
    font-weight: 700;
  }

  :deep(.el-alert__description) {
    width: 100%;
  }

  .progress-content {
    display: grid;
    grid-template-columns: minmax(260px, 1.1fr) minmax(280px, 1.5fr) auto;
    align-items: center;
    gap: 18px;
    padding: 8px 0 2px;
    color: #909399;
  }

  .progress-copy {
    min-width: 0;
  }

  .progress-title {
    color: #dbeafe;
    font-size: 14px;
    line-height: 1.55;
    font-weight: 600;
  }

  .progress-subtitle {
    margin-top: 4px;
    color: #9fb3ce;
    font-size: 12px;
  }

  .progress-actions {
    display: flex;
    justify-content: flex-end;
  }
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.empty-state {
  text-align: center;
  padding: 80px 20px;

  .empty-icon {
    font-size: 72px;
    color: rgba(64, 158, 255, 0.3);
    margin-bottom: 20px;
  }

  .empty-text {
    font-size: 18px;
    color: #909399;
    margin: 0 0 8px 0;
  }

  .empty-subtext {
    font-size: 14px;
    color: #606266;
    margin: 0 0 28px 0;
  }

  .empty-run-btn {
    height: 48px;
    padding: 0 40px;
    font-size: 16px;
    border-radius: 10px;
  }
}

.stats-row {
  margin-bottom: 24px;
}

.stat-col {
  margin-bottom: 12px;
}

.kpi-card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 12px;

  :deep(.el-card__body) {
    padding: 18px;
  }
}

.kpi-body {
  display: flex;
  align-items: center;
  gap: 14px;
}

.kpi-icon {
  width: 48px;
  height: 48px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;

  &.primary { background: rgba(64, 158, 255, 0.15); color: #409eff; }
  &.success { background: rgba(103, 194, 58, 0.15); color: #67c23a; }
  &.warning { background: rgba(230, 162, 60, 0.15); color: #e6a23c; }
  &.danger { background: rgba(245, 108, 108, 0.15); color: #f56c6c; }
}

.pulse-icon-wrapper {
  animation: pulse 1.5s infinite;
}

.kpi-info {
  display: flex;
  flex-direction: column;

  .kpi-value {
    font-size: 26px;
    font-weight: 700;
    color: #e2e8f0;
    line-height: 1.2;
  }

  .kpi-label {
    font-size: 12px;
    color: #909399;
    margin-top: 4px;
  }
}

.text-critical {
  color: #f56c6c !important;
}

.section-card {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  margin-bottom: 24px;
  border-radius: 12px;

  :deep(.el-card__header) {
    padding: 16px 20px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }

  :deep(.el-card__body) {
    padding: 20px;
  }
}

.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  font-weight: 600;
  color: #e2e8f0;

  .el-icon {
    font-size: 20px;
    color: #409eff;
  }

  .count-badge {
    margin-left: auto;
    font-size: 12px;
    color: #909399;
    background: rgba(255, 255, 255, 0.06);
    padding: 2px 10px;
    border-radius: 10px;
  }
}

.data-table {
  :deep(.el-table__header-wrapper th) {
    background: rgba(64, 158, 255, 0.08) !important;
    color: #c0c4cc;
    font-weight: 600;
    font-size: 13px;
  }

  :deep(.el-table__body-wrapper td) {
    color: #e2e8f0;
    font-size: 13px;
  }

  :deep(.el-table__row:hover > td) {
    background: rgba(64, 158, 255, 0.06) !important;
  }

  :deep(.el-table--striped .el-table__body tr.el-table__row--striped td) {
    background: rgba(255, 255, 255, 0.02);
  }

  :deep(.danger-row) {
    background: rgba(245, 108, 108, 0.08);

    &:hover > td {
      background: rgba(245, 108, 108, 0.15) !important;
    }
  }
}

.text-danger {
  color: #f56c6c;
  font-weight: 600;
}

.font-bold {
  font-weight: 700;
}

.text-muted {
  color: #7a7f8a;
}

.urgency-pulse {
  animation: pulse 1.5s infinite;
}

.action-critical {
  color: #f56c6c;
  font-weight: 700;
}

.text-saving {
  color: #67c23a;
  font-weight: 600;
}

/* v4-BOM 替代料样式 - 紧凑纯文本 */
.shortage-cell {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex-wrap: nowrap;
  line-height: 24px;
  white-space: nowrap;
}
.bom-reduced-text {
  color: #67c23a;
  font-size: 12px;
  font-weight: 600;
}
.alt-sep {
  color: #909399;
  font-size: 13px;
  margin: 0 1px;
}
.alt-mat-text {
  color: #e6a23c;
  font-size: 11px;
  font-weight: 500;
  background: rgba(230,162,60,0.1);
  padding: 0 4px;
  border-radius: 3px;
  cursor: default;
}
.alt-more-text {
  color: #909399;
  font-size: 10px;
}

/* v4-BOM 替换统计横幅 */
.bom-substitution-banner {
  margin: 12px 0 0 0;
  border-radius: 8px;
}
.bom-banner-title {
  font-weight: 700;
  font-size: 14px;
}
.bom-banner-detail {
  font-size: 13px;
  color: #606266;
  line-height: 1.6;
}

.rate-analysis {
  display: flex;
  flex-direction: column;
  gap: 22px;
}

.rate-item {
  .rate-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;

    .rate-label {
      font-size: 14px;
      color: #c0c4cc;
      font-weight: 500;
    }

    .rate-percent {
      font-size: 16px;
      font-weight: 700;

      &.success { color: #67c23a; }
      &.warning { color: #e6a23c; }
      &.danger { color: #f56c6c; }
    }
  }

  :deep(.el-progress-bar__outer) {
    background: rgba(255, 255, 255, 0.08);
    border-radius: 9px;
  }

  :deep(.el-progress-bar__inner) {
    border-radius: 9px;
  }
}

.ai-section {
  background: linear-gradient(135deg, rgba(64, 158, 255, 0.03), rgba(103, 194, 58, 0.03));

  .card-header .ai-icon {
    color: #a855f7;
  }
}

.ai-content {
  display: flex;
  flex-direction: column;
  gap: 32px;
}

.sub-title {
  font-size: 16px;
  font-weight: 600;
  color: #e2e8f0;
  margin: 0 0 16px 0;
  padding-bottom: 10px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.quality-section {
  .quality-ring-container {
    display: flex;
    align-items: center;
    gap: 48px;
    flex-wrap: wrap;
  }
}

.quality-ring {
  position: relative;
  width: 140px;
  height: 140px;
  flex-shrink: 0;

  .ring-svg {
    width: 100%;
    height: 100%;
  }

  .ring-score {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    text-align: center;

    .score-num {
      font-size: 36px;
      font-weight: 800;
      color: #e2e8f0;
      line-height: 1;
    }

    .score-unit {
      font-size: 13px;
      color: #909399;
      display: block;
      margin-top: 2px;
    }
  }
}

.breakdown-bars {
  flex: 1;
  min-width: 280px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.breakdown-item {
  display: flex;
  align-items: center;
  gap: 10px;

  .bd-label {
    width: 90px;
    font-size: 12px;
    color: #909399;
    flex-shrink: 0;
  }

  .bd-bar-wrap {
    flex: 1;
    height: 14px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 7px;
    overflow: hidden;
  }

  .bd-bar {
    height: 100%;
    border-radius: 7px;
    background: linear-gradient(90deg, #409eff, #67c23a);
    transition: width 0.6s ease;
  }

  .bd-val {
    width: 36px;
    font-size: 12px;
    color: #c0c4cc;
    text-align: right;
    flex-shrink: 0;
  }
}

.root-cause-section,
.procurement-section {
  .root-cause-table,
  .procurement-table {
    :deep(.el-table__header-wrapper th) {
      background: rgba(168, 85, 247, 0.08) !important;
      color: #c0c4cc;
      font-weight: 600;
      font-size: 13px;
    }

    :deep(.el-table__body-wrapper td) {
      color: #e2e8f0;
      font-size: 13px;
    }
  }
}

.release-table {
  :deep(.el-table__header-wrapper th) {
    background: rgba(230, 162, 60, 0.08) !important;
    color: #c0c4cc;
    font-weight: 600;
    font-size: 13px;
  }

  :deep(.el-table__body-wrapper td) {
    color: #e2e8f0;
    font-size: 13px;
  }
}

.logs-card {
  .logs-timeline {
    max-height: 400px;
    overflow-y: auto;
    padding-right: 8px;

    &::-webkit-scrollbar {
      width: 5px;
    }

    &::-webkit-scrollbar-track {
      background: rgba(255, 255, 255, 0.03);
      border-radius: 3px;
    }

    &::-webkit-scrollbar-thumb {
      background: rgba(255, 255, 255, 0.12);
      border-radius: 3px;
    }
  }

  .log-entry {
    display: flex;
    align-items: center;
    gap: 8px;

    .log-type-tag {
      flex-shrink: 0;
    }

    .log-message {
      font-size: 13px;
      color: #c0c4cc;
      word-break: break-all;
    }
  }

  :deep(.el-timeline-item__timestamp) {
    font-size: 11px;
    color: #7a7f8a;
  }
}

@media (max-width: 767px) {
  .plan-page { max-width: 100%; }

  .page-header {
    flex-direction: column;
  }

  .header-actions {
    width: 100%;
    flex-wrap: wrap;

    .strategy-select { flex: 1; min-width: 120px; }
    .run-btn { flex: 2; }
  }

  .page-title { font-size: 24px; }

  .stats-row .stat-col { margin-bottom: 8px; }

  .kpi-icon { width: 38px; height: 38px; font-size: 20px; }
  .kpi-value { font-size: 22px; }

  .card-header { font-size: 15px; }

  .quality-ring-container {
    flex-direction: column;
    align-items: center;
    gap: 24px;
  }

  .breakdown-bars {
    width: 100%;
  }
}

// 缺料报表摘要预览
.shortage-summary {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.summary-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 10px;
  border-radius: 6px;
  background: rgba(245, 108, 108, 0.05);
  font-size: 13px;

  .summary-order {
    font-weight: 600;
    color: #e2e8f0;
    min-width: 130px;
  }

  .summary-material {
    color: #909399;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .summary-shortage {
    font-weight: 700;
    min-width: 60px;
    text-align: right;
  }
}

.summary-more {
  text-align: center;
  color: #909399;
  font-size: 12px;
  padding: 8px 0 4px;
  cursor: pointer;

  &:hover {
    color: #409eff;
  }
}

.summary-more-link {
  color: #409eff;
  cursor: pointer;
  font-weight: 600;

  &:hover {
    text-decoration: underline;
    color: #66b1ff;
  }
}

// 缺料报表弹窗
.shortage-dialog {
  :deep(.el-dialog__body) {
    padding: 16px 20px 8px;
  }
}

.dialog-pagination {
  display: flex;
  justify-content: flex-end;
  padding: 12px 0 4px;

  :deep(.el-pagination) {
    --el-pagination-bg-color: transparent;
    --el-pagination-text-color: #e2e8f0;
    --el-pagination-button-bg-color: #1a1d29;
    --el-pagination-button-color: #e2e8f0;
    --el-pagination-hover-color: #409eff;
  }

  :deep(.el-pager li) {
    background: #1a1d29 !important;
    color: #e2e8f0;
    border: 1px solid rgba(255, 255, 255, 0.08);

    &.is-active {
      background: #409eff !important;
      color: #fff !important;
      border-color: #409eff;
    }

    &:hover:not(.is-active) {
      background: rgba(64, 158, 255, 0.15) !important;
      color: #409eff;
    }
  }

  :deep(.btn-prev),
  :deep(.btn-next) {
    background: #1a1d29 !important;
    color: #e2e8f0;
    border: 1px solid rgba(255, 255, 255, 0.08);

    &:hover {
      background: rgba(64, 158, 255, 0.15) !important;
      color: #409eff;
    }
  }
}

.shortage-detail-table {
  :deep(.el-table__header-wrapper th) {
    background: rgba(245, 108, 108, 0.08) !important;
    color: #e2e8f0;
    font-weight: 600;
    font-size: 13px;
  }

  :deep(.el-table__body-wrapper td) {
    color: #e2e8f0;
    font-size: 13px;
  }

  :deep(.el-table--striped .el-table__body tr.el-table__row--striped td) {
    background: rgba(255, 255, 255, 0.02);
  }
}

/* 物料需求明细弹窗 */
.plan-data-dialog {
  :deep(.el-dialog__body) {
    padding: 16px 20px 8px;
  }
}

.plan-data-detail-table {
  :deep(.el-table__header-wrapper th) {
    background: rgba(64, 158, 255, 0.08) !important;
    color: #e2e8f0;
    font-weight: 600;
    font-size: 13px;
  }

  :deep(.el-table__body-wrapper td) {
    color: #e2e8f0;
    font-size: 13px;
  }

  :deep(.el-table--striped .el-table__body tr.el-table__row--striped td) {
    background: rgba(255, 255, 255, 0.02);
  }
}
</style>
