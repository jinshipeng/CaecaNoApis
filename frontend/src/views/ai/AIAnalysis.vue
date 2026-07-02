<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  TrendCharts,
  DataAnalysis,
  VideoPlay,
  MagicStick,
  RefreshRight
} from '@element-plus/icons-vue'
import request from '@/api/request'
import { getMaterialList } from '@/api/material'
import { autoAdjustPriority, getComputationMode, setComputationMode, getRetrainStatus, triggerRetrain, getRLRecommendation, trainRLAgent, runMultiObjectiveOptimize } from '@/api/scheduling'

interface ForecastResult {
  success: boolean
  model_type?: string
  forecast?: Array<{
    date: string
    predicted_demand: number
    lower_bound: number
    upper_bound: number
    trend: number
  }>
  summary?: {
    total_predicted_demand: number
    avg_daily_demand: number
    peak_demand_day: string | null
    peak_demand_value: number
    growth_rate: number
  }
  confidence?: number
  anomalies?: Array<{
    date: string
    value: number
    severity: string
    suggestion: string
  }>
}

interface WhatIfResult {
  success: boolean
  scenario: string
  result: {
    scenario_name: string
    overall_impact_score?: number
    decision_support?: {
      can_accept: boolean
      accept_conditionally: boolean
      should_decline: boolean
      reasoning?: string
    }
    recommendations?: Array<{
      action?: string
      confidence?: string
      reason?: string
      steps?: string[]
      priority?: string
      description?: string
    }>
    risk_assessment?: {
      risk_score?: number
      risk_level?: string
      reasoning?: string
      overall_risk_level?: string
    }
    impacted_orders?: Array<{
      order_no: string
      severity: string
      remaining_buffer_days?: number
      predicted_delay_days?: number
      material_code?: string
      risk_level?: string
    }>
    // 紧急插单扩展字段
    affected_orders?: {
      total_affected: number
      high_risk_count?: number
      orders_at_risk?: Array<any>
      details?: Array<any>
    }
    reallocation_suggestions?: Array<{
      strategy: string
      description: string
      feasibility?: string
      cost_impact?: string
    }>
    // 订单取消扩展字段
    cancelled_order?: {
      order_no: string
      material_code: string
      quantity: number
    }
    released_resources?: {
      materials: Array<any>
      capacity: Record<string, any>
    }
    beneficiary_orders?: {
      count: number
      orders: Array<any>
    }
    positive_impacts?: Array<{ type: string; description: string }>
    // 产能故障扩展字段
    failure_info?: {
      work_center_code: string
      work_center_name: string
      failure_days: number
    }
    alternative_work_centers?: Array<{
      work_center_code: string
      work_center_name: string
      transfer_feasibility: string
    }>
    recommended_transfer_target?: Record<string, any> | null
    delivery_delay_prediction?: Record<string, any>
    mitigation_actions?: Array<{ step: number; action: string; timeline: string; priority: string }>
    // BOM ECN扩展字段
    ecn_details?: {
      old_material: { code: string; name: string }
      new_material: { code: string; name: string }
    }
    affected_products?: {
      total_count: number
      products: Array<any>
    }
    additional_material_needs?: Array<any>
    transition_plan?: Array<{ phase: string; duration: string; actions: string[] }>
  }
}

const forecastLoading = ref(false)
const simulationLoading = ref(false)
const priorityLoading = ref(false)
const retrainLoading = ref(false)
const computationMode = ref('serial')
const retrainStatus = ref<{ last_retrain: string | null; status: string }>({ last_retrain: null, status: 'idle' })
const backendUnavailable = ref(false)
const silentRequestOptions = { skipErrorHandler: true }

// 自动调整订单优先级
const handleAutoPriority = async () => {
  try {
    await ElMessageBox.confirm(
      '将根据交期紧迫度、客户等级等因素自动调整所有订单优先级，确认继续？',
      '自动调整优先级',
      { confirmButtonText: '确认调整', cancelButtonText: '取消', type: 'warning' }
    )
    priorityLoading.value = true
    const res = await autoAdjustPriority()
    ElMessage.success((res as any)?.message || '优先级调整完成')
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error?.message || '优先级调整失败')
    }
  } finally {
    priorityLoading.value = false
  }
}

// 计算模式切换
const handleComputationModeChange = async (val: string | number | boolean) => {
  const mode = String(val)
  const oldValue = mode === 'serial' ? 'parallel' : 'serial'
  try {
    await setComputationMode(mode)
    ElMessage.success(`已切换为${mode === 'serial' ? '串行' : '并行'}计算模式`)
  } catch (error: any) {
    computationMode.value = oldValue
    ElMessage.error('计算模式切换失败')
  }
}

// 模型重训练
const handleTriggerRetrain = async () => {
  try {
    await ElMessageBox.confirm(
      '模型重训练可能需要较长时间，期间预测功能可能暂时不可用，确认继续？',
      '模型重训练',
      { confirmButtonText: '开始训练', cancelButtonText: '取消', type: 'warning' }
    )
    retrainLoading.value = true
    await triggerRetrain()
    ElMessage.success('模型重训练任务已提交')
    loadRetrainStatus()
  } catch (error: any) {
    if (error !== 'cancel') {
      ElMessage.error(error?.message || '重训练触发失败')
    }
  } finally {
    retrainLoading.value = false
  }
}

const loadRetrainStatus = async () => {
  try {
    const res = await getRetrainStatus(silentRequestOptions)
    if (res && (res as any).data) retrainStatus.value = (res as any).data
    backendUnavailable.value = false
  } catch {
    backendUnavailable.value = true
  }
}

const loadComputationMode = async () => {
  try {
    const res = await getComputationMode(silentRequestOptions)
    if (res && (res as any).current_mode) computationMode.value = (res as any).current_mode
    backendUnavailable.value = false
  } catch {
    backendUnavailable.value = true
  }
}

// 需求预测相关
const forecastResult = ref<ForecastResult | null>(null)
const predictionDays = ref(30)
const selectedMaterialId = ref<number | null>(null)
const materialList = ref<Array<{ id: number; material_code: string; material_name: string }>>([])

// What-If情景模拟相关
const getActionLabel = (action: string) => {
  const map: Record<string, string> = { ACCEPT: '接受', DECLINE: '拒绝', NEGOTIATE: '协商' }
  return map[action] || '待定'
}

const simulationResult = ref<WhatIfResult | null>(null)
const selectedScenario = ref('urgent_insert')
const scenarioParams = ref({
  quantity: 100,
  demand_date: '',
  priority: 1,
  supplier_id: '',
  delay_days: 7,
  cancel_order_id: '',        // 要取消的订单ID
  ecn_material_id: '',       // ECN变更的原物料ID
  ecn_new_material_id: '',   // 替换为新物料ID
  failure_work_center: '',   // 故障产线
  failure_days: 3,           // 故障持续天数
  work_center: '',
  change_type: 'increase',
  change_percentage: 20,
  surge_percentage: 50,
  duration_days: 14
})

const scenarioOptions = [
  { label: '紧急插单影响分析', value: 'urgent_insert', icon: '!' },
  { label: '订单取消影响', value: 'order_cancel', icon: 'X' },
  { label: '供应商延期评估', value: 'supplier_delay', icon: 'S' },
  { label: '产能故障模拟', value: 'capacity_failure', icon: 'C' },
  { label: 'BOM工程变更影响', value: 'bom_ecn', icon: 'B' },
  { label: '产能变化分析', value: 'capacity_change', icon: 'W' },
  { label: '需求激增压力测试', value: 'demand_surge', icon: 'D' }
]

// ============================================================
// RL 强化学习智能推荐
// ============================================================
const rlResult = ref<any>(null)
const rlLoading = ref(false)
const rlTrainResult = ref<any>(null)
const rlTrainLoading = ref(false)

/** 获取RL智能推荐 */
const handleGetRLRecommendation = async () => {
  rlLoading.value = true
  try {
    const res = await getRLRecommendation()
    if (res && (res as any).success) {
      rlResult.value = (res as any).data
      ElMessage.success('RL 智能推荐完成')
    } else {
      ElMessage.error((res as any)?.error || '获取推荐失败')
    }
  } catch (error: any) {
    ElMessage.error(error?.message || 'RL 推荐请求失败')
  } finally {
    rlLoading.value = false
  }
}

/** 训练RL Agent */
const handleTrainRLAgent = async () => {
  try {
    await ElMessageBox.confirm(
      '基于历史数据训练强化学习 Agent，可能需要较长时间，确认继续？',
      'RL Agent 训练',
      { confirmButtonText: '开始训练', cancelButtonText: '取消', type: 'info' }
    )
    rlTrainLoading.value = true
    const res = await trainRLAgent(30)
    if (res && (res as any).success) {
      rlTrainResult.value = (res as any).data
      ElMessage.success(`训练完成: ${(res as any).data?.episodes || 0}轮`)
    } else {
      ElMessage.error((res as any)?.error || '训练失败')
    }
  } catch (error: any) {
    if (error !== 'cancel') ElMessage.error(error?.message || '训练请求失败')
  } finally {
    rlTrainLoading.value = false
  }
}

// ============================================================
// NSGA-II 多目标优化
// ============================================================
const optimizeResult = ref<any>(null)
const optimizeLoading = ref(false)
const optimizeParams = ref({
  preference: 'delivery_first' as string,
  population_size: 50,
  generations: 100
})

const preferenceOptions = [
  { label: '交付优先（推荐）', value: 'delivery_first' },
  { label: '库存优先', value: 'inventory_first' },
  { label: '稳定性优先', value: 'stability_first' },
  { label: '成本优先', value: 'cost_first' },
  { label: '临期优先', value: 'expiry_first' },
  { label: '完整帕累托前沿', value: 'direct' }
]

/** 运行NSGA-II多目标优化 */
const handleRunOptimization = async () => {
  optimizeLoading.value = true
  try {
    const res = await runMultiObjectiveOptimize({
      preference: optimizeParams.value.preference,
      population_size: optimizeParams.value.population_size,
      generations: optimizeParams.value.generations
    })
    if (res && (res as any).success) {
      optimizeResult.value = (res as any).data
      ElMessage.success('NSGA-II多目标优化完成')
    } else {
      ElMessage.error((res as any)?.error || '优化失败')
    }
  } catch (error: any) {
    ElMessage.error(error?.message || '优化请求失败')
  } finally {
    optimizeLoading.value = false
  }
}

// 运行需求预测
const handleRunForecast = async () => {
  forecastLoading.value = true
  try {
    const res = await request.post<any>('/ai/demand-forecast/', {
      days: predictionDays.value,
      // 不传material_id，走全局预测（predict），返回forecast/summary/confidence标准格式
      // 传material_id会走物料级预测（predict_material_requirements），返回不同的数据结构
      force_retrain: false
    }, { showLoading: false, timeout: 120000 })

    if (res && res.success) {
      forecastResult.value = (res && typeof res === 'object') ? res : null
      ElMessage.success('需求预测完成')
    } else {
      ElMessage.error(res.error || '预测失败')
    }
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.error || error?.message || '预测请求失败')
    forecastResult.value = null
  } finally {
    forecastLoading.value = false
  }
}

// 运行What-If情景模拟
const handleRunSimulation = async () => {
  // 设置默认日期（如果未填写）
  if (!scenarioParams.value.demand_date) {
    const today = new Date()
    today.setDate(today.getDate() + 7) // 默认7天后
    scenarioParams.value.demand_date = today.toISOString().split('T')[0]
  }

  simulationLoading.value = true
  try {
    let params: Record<string, any> = {}
    
    switch (selectedScenario.value) {
      case 'urgent_insert':
        params = {
          quantity: scenarioParams.value.quantity,
          demand_date: scenarioParams.value.demand_date,
          priority: scenarioParams.value.priority
        }
        break
      case 'order_cancel':
        params = {
          cancel_order_id: scenarioParams.value.cancel_order_id || ''
        }
        break
      case 'supplier_delay':
        params = {
          supplier_id: scenarioParams.value.supplier_id || '',
          delay_days: scenarioParams.value.delay_days
        }
        break
      case 'capacity_failure':
        params = {
          failure_work_center: scenarioParams.value.failure_work_center || '',
          failure_days: scenarioParams.value.failure_days || 3
        }
        break
      case 'bom_ecn':
        params = {
          ecn_material_id: scenarioParams.value.ecn_material_id || '',
          ecn_new_material_id: scenarioParams.value.ecn_new_material_id || ''
        }
        break
      case 'capacity_change':
        params = {
          work_center: scenarioParams.value.work_center || '',
          change_type: scenarioParams.value.change_type || 'increase',
          change_percentage: scenarioParams.value.change_percentage || 20,
          duration_days: scenarioParams.value.duration_days || 30
        }
        break
      case 'demand_surge':
        params = {
          surge_percentage: scenarioParams.value.surge_percentage || 50,
          duration_days: scenarioParams.value.duration_days || 14
        }
        break
    }
    
    const res = await request.post<any>('/ai/whatif-simulation/', {
      scenario: selectedScenario.value,
      parameters: params
    }, { showLoading: false })

    if (res && res.success) {
      simulationResult.value = (res && typeof res === 'object') ? res : null
      ElMessage.success('情景模拟完成')
    } else {
      ElMessage.error('模拟执行失败')
    }
  } catch (error: any) {
    ElMessage.error(error?.message || '模拟请求失败')
    simulationResult.value = null
  } finally {
    simulationLoading.value = false
  }
}

// 格式化数字
const formatNumber = (num: number | undefined | null) => {
  if (num === undefined || num === null) return '-'
  return num.toLocaleString('zh-CN', { maximumFractionDigits: 1 })
}

// 获取影响评分颜色
const getImpactColor = (score: number | undefined) => {
  if (!score) return '#909399'
  if (score < 0.4) return '#67c23a'   // 绿色 - 低风险
  if (score < 0.7) return '#e6a23c'   // 橙色 - 中等风险
  return '#f56c6c'                     // 红色 - 高风险
}

// 获取决策建议图标
const getDecisionIcon = (result: WhatIfResult | null) => {
  if (!result?.result?.decision_support) return ''
  const ds = result.result.decision_support
  if (ds.can_accept) return 'OK'
  if (ds.accept_conditionally) return '◇'
  if (ds.should_decline) return 'NO'
  return ''
}

onMounted(async () => {
  const defaultDate = new Date()
  defaultDate.setDate(defaultDate.getDate() + 7)
  scenarioParams.value.demand_date = defaultDate.toISOString().split('T')[0]

  try {
    const res = await getMaterialList({ page_size: 100 }, silentRequestOptions)
    materialList.value = res.results || []
    if (materialList.value.length > 0) {
      selectedMaterialId.value = materialList.value[0].id
    }
    backendUnavailable.value = false
  } catch {
    backendUnavailable.value = true
  }

  loadComputationMode()
  loadRetrainStatus()
})
</script>

<template>
  <div class="ai-analysis-page">
    <el-alert
      v-if="backendUnavailable"
      class="backend-alert"
      type="warning"
      :closable="false"
      show-icon
      title="后端服务暂不可用，当前页面已进入离线展示模式"
      description="请确认 Django 服务已在 127.0.0.1:8000 启动；若 Clash Verge 开启 TUN，请确保 127.0.0.1、localhost 和 ::1 走 DIRECT。"
    />

    <div class="page-header">
      <div>
        <h1 class="page-title">智能分析与决策</h1>
        <p class="page-desc">集成机器学习的需求预测、异常检测与情景模拟系统</p>
      </div>
      <div class="header-actions">
        <el-button
          type="warning"
          :icon="MagicStick"
          :loading="priorityLoading"
          @click="handleAutoPriority"
        >
          自动调整优先级
        </el-button>
        <el-button
          type="info"
          :icon="RefreshRight"
          :loading="retrainLoading"
          @click="handleTriggerRetrain"
        >
          模型重训练
        </el-button>
        <div class="mode-switch">
          <span class="mode-label">计算模式：</span>
          <el-switch
            v-model="computationMode"
            active-value="parallel"
            inactive-value="serial"
            active-text="并行"
            inactive-text="串行"
            @change="handleComputationModeChange"
          />
        </div>
        <span v-if="retrainStatus.last_retrain" class="retrain-info">
          上次训练: {{ retrainStatus.last_retrain?.split('T')[0] || '-' }}
        </span>
      </div>
    </div>

    <!-- 需求预测模块 -->
    <el-card shadow="never" class="section-card">
      <template #header>
        <div class="card-header">
          <el-icon><TrendCharts /></el-icon>
          <span>需求预测引擎</span>
          <el-tag type="success" size="small" effect="dark">Prophet时序模型</el-tag>
        </div>
      </template>

      <div class="forecast-controls">
        <div class="control-row">
          <div class="control-item">
            <label>预测天数:</label>
            <el-input-number v-model="predictionDays" :min="7" :max="90" :step="7" />
            <span class="unit">天</span>
          </div>
          
          <el-button 
            type="primary" 
            :icon="VideoPlay"
            :loading="forecastLoading"
            @click="handleRunForecast"
            size="large"
          >
            {{ forecastLoading ? '正在训练模型...' : '运行预测' }}
          </el-button>
        </div>
      </div>

      <!-- 预测结果展示 -->
      <div v-if="forecastResult && forecastResult.success" class="forecast-result">
        <!-- 汇总指标 -->
        <el-row :gutter="16" class="summary-row">
          <el-col :xs="12" :sm="6">
            <div class="metric-card">
              <div class="metric-value">{{ formatNumber(forecastResult.summary?.total_predicted_demand) }}</div>
              <div class="metric-label">总预测需求</div>
            </div>
          </el-col>
          <el-col :xs="12" :sm="6">
            <div class="metric-card">
              <div class="metric-value">{{ formatNumber(forecastResult.summary?.avg_daily_demand) }}</div>
              <div class="metric-label">日均需求</div>
            </div>
          </el-col>
          <el-col :xs="12" :sm="6">
            <div class="metric-card">
              <div class="metric-value" :style="{ color: (forecastResult.summary?.growth_rate ?? 0) > 0 ? '#67c23a' : '#f56c6c' }">
                {{ (forecastResult.summary?.growth_rate ?? 0) > 0 ? '+' : '' }}{{ Number((forecastResult.summary?.growth_rate || 0) * 100).toFixed(1) }}%
              </div>
              <div class="metric-label">增长率</div>
            </div>
          </el-col>
          <el-col :xs="12" :sm="6">
            <div class="metric-card">
              <div class="metric-value">{{ Number((forecastResult.confidence || 0) * 100).toFixed(0) }}%</div>
              <div class="metric-label">置信度</div>
            </div>
          </el-col>
        </el-row>

        <!-- 预测趋势表格 -->
        <el-table border
          :data="forecastResult.forecast?.slice(0, 14)" 
          stripe 
          size="small"
          max-height="400"
          class="forecast-table"
        >
          <el-table-column prop="date" label="日期" width="95" />
          <el-table-column prop="predicted_demand" label="预测需求" width="85" align="right">
            <template #default="{ row }">
              {{ formatNumber(row.predicted_demand) }}
            </template>
          </el-table-column>
          <el-table-column label="95%置信区间" width="140">
            <template #default="{ row }">
              <div class="confidence-interval">
                <span class="lower">{{ formatNumber(row.lower_bound) }}</span>
                <span class="separator">~</span>
                <span class="upper">{{ formatNumber(row.upper_bound) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="trend" label="趋势分量" width="75" align="right">
            <template #default="{ row }">
              <span :style="{ color: row.trend > 0 ? '#67c23a' : '#f56c6c' }">
                {{ row.trend > 0 ? '+' : '' }}{{ formatNumber(row.trend) }}
              </span>
            </template>
          </el-table-column>
        </el-table>

        <!-- 异常点提示 -->
        <el-alert
          v-if="forecastResult.anomalies && forecastResult.anomalies.length > 0"
          :title="`检测到 ${forecastResult.anomalies.length} 个预测异常点`"
          type="warning"
          show-icon
          :closable="false"
          class="anomaly-alert"
        >
          <template #default>
            <div class="anomaly-list">
              <div v-for="(anomaly, idx) in forecastResult.anomalies.slice(0, 3)" :key="idx" class="anomaly-item">
                <el-tag :type="anomaly.severity === 'high' ? 'danger' : 'warning'" size="small">
                  {{ anomaly.date }}
                </el-tag>
                <span>{{ anomaly.suggestion }}</span>
              </div>
            </div>
          </template>
        </el-alert>
      </div>

      <el-empty v-else-if="!forecastLoading" description="点击「运行预测」开始 AI 预测分析" />
    </el-card>

    <!-- What-If情景模拟模块 -->
    <el-card shadow="never" class="section-card simulation-card">
      <template #header>
        <div class="card-header">
          <el-icon><DataAnalysis /></el-icon>
          <span>What-If情景模拟器</span>
          <el-tag type="warning" size="small" effect="dark">决策支持</el-tag>
        </div>
      </template>

      <div class="simulation-layout">
        <!-- 左侧：参数配置 -->
        <div class="sim-config-panel">
          <h3 class="panel-title">场景选择</h3>
          <el-radio-group v-model="selectedScenario" class="scenario-selector">
            <el-button
              v-for="opt in scenarioOptions"
              :key="opt.value"
              :type="selectedScenario === opt.value ? 'primary' : 'default'"
              @click="selectedScenario = opt.value"
              class="scenario-btn"
            >
              <span class="scenario-icon">{{ opt.icon }}</span>
              {{ opt.label }}
            </el-button>
          </el-radio-group>

          <h3 class="panel-title mt-4">参数配置</h3>
          
          <!-- 紧急插单参数 -->
          <div v-if="selectedScenario === 'urgent_insert'" class="param-group">
            <div class="param-item">
              <label>插单数量:</label>
              <el-input-number v-model="scenarioParams.quantity" :min="1" :max="10000" />
            </div>
            <div class="param-item">
              <label>需求日期</label>
              <el-date-picker
                v-model="scenarioParams.demand_date"
                type="date"
                placeholder="选择日期"
                style="width: 100%"
              />
            </div>
            <div class="param-item">
              <label>优先级</label>
              <el-select v-model="scenarioParams.priority" style="width: 100%">
                <el-option :value="1" label="P1 - 最高（紧急）" />
                <el-option :value="2" label="P2 - 高" />
                <el-option :value="3" label="P3 - 中" />
                <el-option :value="5" label="P5 - 普通" />
              </el-select>
            </div>
          </div>

          <!-- 订单取消参数 -->
          <div v-if="selectedScenario === 'order_cancel'" class="param-group">
            <div class="param-item">
              <label>要取消的订单ID:</label>
              <el-input
                v-model="scenarioParams.cancel_order_id"
                placeholder="请输入要取消的订单ID"
                clearable
              />
            </div>
            <p class="param-hint">模拟取消该订单后释放的物料和产能如何重新分配</p>
          </div>

          <!-- 供应商延期参数 -->
          <div v-if="selectedScenario === 'supplier_delay'" class="param-group">
            <div class="param-item">
              <label>延期天数:</label>
              <el-input-number v-model="scenarioParams.delay_days" :min="1" :max="60" />
            </div>
          </div>

          <!-- 产能故障参数 -->
          <div v-if="selectedScenario === 'capacity_failure'" class="param-group">
            <div class="param-item">
              <label>故障产线:</label>
              <el-input
                v-model="scenarioParams.failure_work_center"
                placeholder="输入产线编码，如 WC-001"
                clearable
              />
            </div>
            <div class="param-item">
              <label>故障持续天数:</label>
              <el-input-number v-model="scenarioParams.failure_days" :min="1" :max="30" />
            </div>
            <p class="param-hint">模拟产线停机对生产订单和交付的影响</p>
          </div>

          <!-- BOM工程变更参数 -->
          <div v-if="selectedScenario === 'bom_ecn'" class="param-group">
            <div class="param-item">
              <label>原物料ID（被替换）</label>
              <el-input
                v-model="scenarioParams.ecn_material_id"
                placeholder="请输入被替换的原物料ID"
                clearable
              />
            </div>
            <div class="param-item">
              <label>新物料ID（替换为）</label>
              <el-input
                v-model="scenarioParams.ecn_new_material_id"
                placeholder="请输入替换后的新物料ID"
                clearable
              />
            </div>
            <p class="param-hint">模拟 BOM 子件变更对产品范围、物料需求的影响</p>
          </div>

          <!-- 产能变化参数 -->
          <div v-if="selectedScenario === 'capacity_change'" class="param-group">
            <div class="param-item">
              <label>工作中心:</label>
              <el-input
                v-model="scenarioParams.work_center"
                placeholder="输入工作中心编码，如 WC-001"
                clearable
              />
            </div>
            <div class="param-item">
              <label>变化类型:</label>
              <el-select v-model="scenarioParams.change_type" style="width: 100%">
                <el-option value="increase" label="产能增加" />
                <el-option value="decrease" label="产能减少" />
              </el-select>
            </div>
            <div class="param-item">
              <label>变化幅度(%):</label>
              <el-input-number v-model="scenarioParams.change_percentage" :min="5" :max="100" :step="5" />
            </div>
            <div class="param-item">
              <label>持续天数:</label>
              <el-input-number v-model="scenarioParams.duration_days" :min="1" :max="90" />
            </div>
            <p class="param-hint">模拟产能增减对排产和交付的影响</p>
          </div>

          <!-- 需求激增参数 -->
          <div v-if="selectedScenario === 'demand_surge'" class="param-group">
            <div class="param-item">
              <label>激增比例(%):</label>
              <el-input-number v-model="scenarioParams.surge_percentage" :min="10" :max="200" :step="10" />
            </div>
            <div class="param-item">
              <label>持续天数:</label>
              <el-input-number v-model="scenarioParams.duration_days" :min="3" :max="60" />
            </div>
            <p class="param-hint">模拟订单量突然增加对供应链的压力测试</p>
          </div>

          <el-button 
            type="primary" 
            :icon="VideoPlay"
            :loading="simulationLoading"
            @click="handleRunSimulation"
            size="large"
            class="run-sim-btn"
            block
          >
            {{ simulationLoading ? '正在模拟...' : '运行模拟' }}
          </el-button>
        </div>

        <!-- 右侧：结果展示 -->
        <div class="sim-result-panel">
          <div v-if="simulationResult && simulationResult.success" class="simulation-results">
            <!-- 决策支持卡片 -->
            <div 
              v-if="simulationResult.result.decision_support"
              class="decision-card"
              :style="{ borderColor: getImpactColor(simulationResult.result.overall_impact_score) }"
            >
              <h3 class="decision-title">
                {{ getDecisionIcon(simulationResult) }} 决策建议
              </h3>
              <div class="decision-content">
                <div class="impact-score">
                  <span class="score-label">综合影响评分:</span>
                  <span 
                    class="score-value" 
                    :style="{ 
                      color: getImpactColor(simulationResult.result.overall_impact_score),
                      fontSize: '28px',
                      fontWeight: 'bold'
                    }"
                  >
                    {{ Number((simulationResult.result.overall_impact_score || 0) * 100).toFixed(0) }}分
                  </span>
                  <div class="score-bar">
                    <el-progress 
                      :percentage="(simulationResult.result.overall_impact_score || 0) * 100"
                      :color="getImpactColor(simulationResult.result.overall_impact_score)"
                      :show-text="false"
                      :stroke-width="10"
                    />
                  </div>
                </div>
                
                <div class="decision-verdict">
                  <el-tag 
                    v-if="simulationResult.result.decision_support.can_accept"
                    type="success" 
                    size="large"
                    effect="dark"
                    class="verdict-tag"
                  >
                    ✓ 建议接受
                  </el-tag>
                  <el-tag 
                    v-else-if="simulationResult.result.decision_support.accept_conditionally"
                    type="warning" 
                    size="large"
                    effect="dark"
                    class="verdict-tag"
                  >
                ◇ 有条件接受
                  </el-tag>
                  <el-tag 
                    v-else-if="simulationResult.result.decision_support.should_decline"
                    type="danger" 
                    size="large"
                    effect="dark"
                    class="verdict-tag"
                  >
                ✗ 建议拒绝
                  </el-tag>
                  
                  <p class="reasoning-text">
                    {{ simulationResult.result.decision_support.reasoning }}
                  </p>
                </div>
              </div>
            </div>

            <!-- 璇︾粏寤鸿鍒楄〃 -->
            <div v-if="simulationResult.result.recommendations" class="recommendations-list">
              <h4 class="sub-title">详细建议:</h4>
              <div 
                v-for="(rec, idx) in simulationResult.result.recommendations" 
                :key="idx" 
                class="recommendation-item"
              >
                <el-tag
                  :type="rec.action === 'ACCEPT' ? 'success' : rec.action === 'DECLINE' ? 'danger' : 'warning'"
                  size="small"
                >
                  {{ getActionLabel(rec.action || '') }}
                </el-tag>
                <div class="rec-content">
                  <p class="rec-reason">{{ rec.reason }}</p>
                  <ul v-if="rec.steps" class="rec-steps">
                    <li v-for="(step, stepIdx) in rec.steps" :key="stepIdx">{{ step }}</li>
                  </ul>
                </div>
              </div>
            </div>

            <!-- 受影响订单（如果有的话） -->
            <div v-if="simulationResult.result && simulationResult.result.impacted_orders && simulationResult.result.impacted_orders.length > 0" class="impacted-orders">
              <h4 class="sub-title">
                ◇ 受影响订单 ({{ simulationResult.result?.impacted_orders?.length ?? 0 }})
              </h4>
              <el-table border :data="simulationResult.result?.impacted_orders?.slice(0, 8) || []" size="small" stripe>
                <el-table-column prop="order_no" label="订单号" width="110" show-overflow-tooltip />
                <el-table-column label="严重程度" width="70" align="center">
                  <template #default="{ row }">
                    <el-tag 
                      :type="row.severity === 'critical' ? 'danger' : row.severity === 'high' ? 'warning' : 'info'"
                      size="small"
                    >
                      {{ row.severity === 'critical' ? '严重' : row.severity === 'high' ? '高' : '中' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="remaining_buffer_days" label="剩余缓冲(天)" width="100" align="right" show-overflow-tooltip />
              </el-table>
            </div>
          </div>

          <el-empty v-else-if="!simulationLoading" description="配置参数后点击运行模拟查看影响分析" />
        </div>
      </div>
    </el-card>

    <!-- ============================================================ -->
    <!-- RL 强化学习智能推荐 -->
    <!-- ============================================================ -->
    <el-card shadow="never" class="section-card rl-card">
      <template #header>
        <div class="card-header">
          <el-icon><MagicStick /></el-icon>
          <span>RL 强化学习智能推荐</span>
          <el-tag type="primary" size="small" effect="dark">Q-Learning</el-tag>
        </div>
      </template>
      <div class="rl-layout">
        <div class="rl-controls">
          <el-button
            type="primary"
            :icon="VideoPlay"
            :loading="rlLoading"
            @click="handleGetRLRecommendation"
          >
            {{ rlLoading ? '分析中...' : '获取智能推荐' }}
          </el-button>
          <el-button
            :icon="RefreshRight"
            :loading="rlTrainLoading"
            @click="handleTrainRLAgent"
          >
            {{ rlTrainLoading ? '训练中...' : '训练 Agent' }}
          </el-button>
        </div>

        <div v-if="rlResult" class="rl-result-panel">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="推荐动作">
              <el-tag type="success" size="large">{{ rlResult.action_name || rlResult.recommended_action || '-' }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="置信度">
              <el-progress
                :percentage="Number((rlResult.confidence || 0) * 100)"
                :color="'#67c23a'"
                :stroke-width="14"
                :format="(p: number) => `${p}%`"
              />
            </el-descriptions-item>
            <el-descriptions-item label="异常检测">
              <el-tag :type="rlResult.anomaly_detected ? 'danger' : 'success'" size="small">
                {{ rlResult.anomaly_detected ? '检测到异常' : '状态正常' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="当前库存利用率">
              {{ rlResult.current_state ? ((rlResult.current_state[2] || 0) * 100).toFixed(1) + '%' : '-' }}
            </el-descriptions-item>
          </el-descriptions>

          <div v-if="rlResult.db_effects && rlResult.db_effects.length > 0" class="db-effects mt-4">
            <h4 class="sub-title">数据库执行效果</h4>
            <el-table :data="rlResult.db_effects.slice(0, 5)" size="small" stripe border>
              <el-table-column prop="action" label="操作" width="120" />
              <el-table-column prop="target" label="目标" width="150" />
              <el-table-column prop="effect" label="效果" />
            </el-table>
          </div>
        </div>

        <div v-if="rlTrainResult" class="rl-train-result mt-4">
          <el-alert :title="`训练完成: ${rlTrainResult.episodes || 0}轮，最终奖励 ${(rlTrainResult.final_reward || 0).toFixed(2)}`" type="success" show-icon :closable="false" />
        </div>

        <el-empty v-else-if="!rlLoading && !rlResult" description="点击「获取智能推荐」基于实时数据生成 RL 决策建议" :image-size="80" />
      </div>
    </el-card>

    <!-- ============================================================ -->
    <!-- NSGA-II 多目标优化 -->
    <!-- ============================================================ -->
    <el-card shadow="never" class="section-card optimize-card">
      <template #header>
        <div class="card-header">
          <el-icon><DataAnalysis /></el-icon>
          <span>NSGA-II 多目标优化</span>
          <el-tag type="warning" size="small" effect="dark">帕累托前沿</el-tag>
        </div>
      </template>
      <div class="optimize-layout">
        <div class="optimize-config-panel">
          <h3 class="panel-title">优化参数</h3>
          <div class="param-group">
            <label>偏好模式:</label>
            <el-select v-model="optimizeParams.preference" style="width: 100%">
              <el-option
                v-for="opt in preferenceOptions"
                :key="opt.value"
                :label="opt.label"
                :value="opt.value"
              />
            </el-select>
          </div>
          <div class="param-group">
            <label>种群大小:</label>
            <el-input-number v-model="optimizeParams.population_size" :min="20" :max="200" :step="10" />
          </div>
          <div class="param-group">
            <label>进化代数:</label>
            <el-input-number v-model="optimizeParams.generations" :min="20" :max="500" :step="20" />
          </div>
          <p class="param-hint">同时优化: 按时交付率 / 交期变更 / 库存水位 / 报缺精准度</p>

          <el-button
            type="primary"
            :icon="VideoPlay"
            :loading="optimizeLoading"
            @click="handleRunOptimization"
            block
            size="large"
            class="mt-4"
          >
            {{ optimizeLoading ? '优化中，可能需要较长时间...' : '运行多目标优化' }}
          </el-button>
        </div>

        <div class="optimize-result-panel">
          <div v-if="optimizeResult" class="optimize-results">
            <!-- 推荐方案 -->
            <div v-if="optimizeResult.recommended_solution" class="rec-solution">
              <h4 class="sub-title">推荐方案</h4>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="策略">{{ optimizeResult.recommended_solution.strategy_name || '均衡策略' }}</el-descriptions-item>
                <el-descriptions-item label="预期交付率">{{ (optimizeResult.recommended_solution.expected_delivery_rate || 0).toFixed(1) }}%</el-descriptions-item>
                <el-descriptions-item label="预期库存水位">{{ (optimizeResult.recommended_solution.expected_inventory_level || 0).toFixed(1) }}</el-descriptions-item>
                <el-descriptions-item label="预期交期变更次数">{{ optimizeResult.recommended_solution.expected_delivery_changes || 0 }}</el-descriptions-item>
              </el-descriptions>
            </div>

            <!-- 帕累托前沿 -->
            <div v-if="optimizeResult.pareto_front && optimizeResult.pareto_front.length > 0" class="pareto-front mt-4">
              <h4 class="sub-title">帕累托最优解 (Top{{ Math.min(optimizeResult.pareto_front.length, 20) }})</h4>
              <el-table :data="optimizeResult.pareto_front.slice(0, 10)" size="small" stripe border max-height="300">
                <el-table-column label="按时交付率" width="110" align="right">
                  <template #default="{ row }">
                    {{ ((row.objectives?.[0] || 0) * 100).toFixed(1) }}%
                  </template>
                </el-table-column>
                <el-table-column label="交期变更(越低越好)" width="110" align="right">
                  <template #default="{ row }">
                    {{ (row.objectives?.[1] || 0).toFixed(3) }}
                  </template>
                </el-table-column>
                <el-table-column label="库存水平(越低越好)" width="110" align="right">
                  <template #default="{ row }">
                    {{ (row.objectives?.[2] || 0).toFixed(3) }}
                  </template>
                </el-table-column>
                <el-table-column label="报缺精准度" width="110" align="right">
                  <template #default="{ row }">
                    {{ ((row.objectives?.[3] || 0) * 100).toFixed(1) }}%
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <!-- 优化报告摘要 -->
            <div v-if="optimizeResult.report" class="opt-report mt-4">
              <h4 class="sub-title">优化报告</h4>
              <el-row :gutter="12">
                <el-col :span="8">
                  <div class="report-metric">
                    <span class="metric-val">{{ optimizeResult.report.total_generations || optimizeParams.generations }}</span>
                    <span class="metric-lbl">进化代数</span>
                  </div>
                </el-col>
                <el-col :span="8">
                  <div class="report-metric">
                    <span class="metric-val">{{ optimizeResult.report.pareto_front_size || optimizeResult.pareto_front_count || 0 }}</span>
                    <span class="metric-lbl">帕累托解数</span>
                  </div>
                </el-col>
                <el-col :span="8">
                  <div class="report-metric">
                    <span class="metric-val">{{ (optimizeResult.report.convergence_rate || 0).toFixed(2) }}</span>
                    <span class="metric-lbl">收敛率</span>
                  </div>
                </el-col>
              </el-row>
            </div>
          </div>

          <el-empty v-else-if="!optimizeLoading" description="配置参数后点击运行多目标优化，获取帕累托最优解集" :image-size="80" />
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.ai-analysis-page {
  width: 100%;
}

.backend-alert {
  margin-bottom: 14px;
  border: 1px solid rgba(230, 162, 60, 0.28);
  background: linear-gradient(135deg, rgba(230, 162, 60, 0.12), rgba(230, 162, 60, 0.04));

  :deep(.el-alert__title) {
    color: #ffd27a;
    font-weight: 700;
  }

  :deep(.el-alert__description) {
    color: rgba(226, 232, 240, 0.72);
  }
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
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

  .header-actions {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;

    .mode-switch {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      background: rgba(255, 255, 255, 0.04);
      border-radius: 8px;
      border: 1px solid rgba(255, 255, 255, 0.08);

      .mode-label {
        font-size: 13px;
        color: #c0c4cc;
        white-space: nowrap;
      }
    }

    .retrain-info {
      font-size: 12px;
      color: #78849E;
    }
  }
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
    padding: 24px;
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
}

// 预测控制区
.forecast-controls {
  margin-bottom: 24px;
  
  .control-row {
    display: flex;
    align-items: center;
    gap: 20px;
    flex-wrap: wrap;
  }
  
  .control-item {
    display: flex;
    align-items: center;
    gap: 8px;
    
    label {
      color: #c0c4cc;
      font-size: 14px;
      white-space: nowrap;
    }
    
    .unit {
      color: #909399;
      font-size: 13px;
    }
  }
}

// 预测结果
.forecast-result {
  .summary-row {
    margin-bottom: 24px;
  }
  
  .metric-card {
    background: rgba(64, 158, 255, 0.08);
    border-radius: 10px;
    padding: 18px;
    text-align: center;
    border: 1px solid rgba(64, 158, 255, 0.15);
    
    .metric-value {
      font-size: 26px;
      font-weight: 700;
      color: #e2e8f0;
      line-height: 1.2;
    }
    
    .metric-label {
      font-size: 12px;
      color: #909399;
      margin-top: 6px;
    }
  }
  
  .forecast-table {
    :deep(.el-table__header-wrapper th) {
      background: rgba(103, 194, 58, 0.08) !important;
      color: #c0c4cc;
    }
  }
  
  .confidence-interval {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    
    .lower { color: #909399; }
    .upper { color: #e2e8f0; font-weight: 500; }
    .separator { color: #606266; }
  }
  
  .anomaly-alert {
    margin-top: 16px;
    
    .anomaly-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      
      .anomaly-item {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 13px;
        color: #e6a23c;
      }
    }
  }
}

// 妯℃嫙鍣ㄥ竷灞€
.simulation-card {
  .simulation-layout {
    display: grid;
    grid-template-columns: 350px 1fr;
    gap: 24px;
    
    @media (max-width: 1024px) {
      grid-template-columns: 1fr;
    }
  }
}

.sim-config-panel {
  background: rgba(255, 255, 255, 0.03);
  border-radius: 10px;
  padding: 20px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  
  .panel-title {
    font-size: 15px;
    font-weight: 600;
    color: #e2e8f0;
    margin: 0 0 14px 0;
    padding-bottom: 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }
  
  .mt-4 { margin-top: 20px; }
  
  .scenario-selector {
    display: flex;
    flex-direction: column;
    gap: 10px;
    
    .scenario-btn {
      justify-content: flex-start;
      height: auto;
      padding: 12px 16px;
      
      .scenario-icon {
        margin-right: 8px;
        font-size: 18px;
      }
    }
  }
  
  .param-group {
    display: flex;
    flex-direction: column;
    gap: 14px;
    
    .param-item {
      display: flex;
      flex-direction: column;
      gap: 6px;
      
      label {
        font-size: 13px;
        color: #c0c4cc;
        font-weight: 500;
      }
    }

    .param-hint {
      font-size: 12px;
      color: #909399;
      margin: 0;
      padding: 8px 10px;
      background: rgba(64, 158, 255, 0.06);
      border-radius: 6px;
      border-left: 3px solid rgba(64, 158, 255, 0.3);
      line-height: 1.5;
    }
  }
  
  .run-sim-btn {
    margin-top: 20px;
    height: 48px;
    font-size: 16px;
    font-weight: 600;
  }
}

.sim-result-panel {
  .simulation-results {
    .decision-card {
      background: linear-gradient(135deg, rgba(64, 158, 255, 0.05), rgba(103, 194, 58, 0.05));
      border: 2px solid;
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 20px;
      
      .decision-title {
        font-size: 18px;
        font-weight: 700;
        color: #e2e8f0;
        margin: 0 0 16px 0;
      }
      
      .decision-content {
        .impact-score {
          margin-bottom: 20px;
          
          .score-label {
            font-size: 14px;
            color: #909399;
            margin-right: 12px;
          }
          
          .score-bar {
            margin-top: 8px;
          }
        }
        
        .decision-verdict {
          .verdict-tag {
            font-size: 16px;
            padding: 10px 24px;
            margin-bottom: 12px;
          }
          
          .reasoning-text {
            font-size: 14px;
            color: #c0c4cc;
            line-height: 1.6;
            margin: 0;
            padding: 12px;
            background: rgba(0, 0, 0, 0.15);
            border-radius: 8px;
          }
        }
      }
    }
    
    .recommendations-list {
      .sub-title {
        font-size: 15px;
        font-weight: 600;
        color: #e2e8f0;
        margin: 0 0 12px 0;
      }
      
      .recommendation-item {
        background: rgba(255, 255, 255, 0.03);
        border-left: 3px solid #409eff;
        padding: 14px;
        margin-bottom: 12px;
        border-radius: 0 8px 8px 0;
        
        .rec-content {
          margin-top: 8px;
          
          .rec-reason {
            font-size: 14px;
            color: #e2e8f0;
            margin: 0 0 8px 0;
            font-weight: 500;
          }
          
          .rec-steps {
            margin: 0;
            padding-left: 20px;
            
            li {
              font-size: 13px;
              color: #c0c4cc;
              line-height: 1.8;
            }
          }
        }
      }
    }
    
    .impacted-orders {
      .sub-title {
        font-size: 15px;
        font-weight: 600;
        color: #f56c6c;
        margin: 20px 0 12px 0;
      }
      
      :deep(.el-table__header-wrapper th) {
        background: rgba(245, 108, 108, 0.08) !important;
      }
    }
  }
}

@media (max-width: 767px) {
  .page-header .page-title {
    font-size: 24px;
  }

  .forecast-controls .control-row {
    flex-direction: column;
    align-items: stretch;
  }
}

// ============================================================
// RL 强化学习智能推荐 鏍峰紡
// ============================================================
.rl-card {
  .rl-layout {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .rl-controls {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }

  .rl-result-panel {
    background: rgba(64, 158, 255, 0.05);
    border-radius: 8px;
    padding: 16px;

    :deep(.el-descriptions) {
      --el-descriptions-table-border: 1px solid rgba(255, 255, 255, 0.08);
    }

    :deep(.el-descriptions__label) {
      color: #909399 !important;
    }

    :deep(.el-descriptions__content) {
      color: #e2e8f0 !important;
    }
  }

  .db-effects {
      h4 { color: #e2e8f0; margin-bottom: 8px; font-size: 14px; }
    }
}

// ============================================================
// NSGA-II 多目标优化鏍峰紡
// ============================================================
.optimize-card {
  .optimize-layout {
    display: grid;
    grid-template-columns: 300px 1fr;
    gap: 24px;
  }

  .optimize-config-panel {
    .param-group {
      margin-bottom: 12px;

      label {
        display: block;
        color: #c0c4cc;
        font-size: 13px;
        margin-bottom: 4px;
      }
    }

    .param-hint {
      font-size: 12px;
      color: #78849E;
      margin-top: 4px;
    }
  }

  .optimize-result-panel {
    .rec-solution, .pareto-front, .opt-report {
      h4 { color: #e2e8f0; margin-bottom: 10px; font-size: 14px; }
    }

    .report-metric {
      text-align: center;
      padding: 12px 8px;
      background: rgba(255, 255, 255, 0.04);
      border-radius: 8px;

      .metric-val {
        display: block;
        font-size: 22px;
        font-weight: 700;
        color: #409eff;
      }

      .metric-lbl {
        display: block;
        font-size: 12px;
        color: #909399;
        margin-top: 4px;
      }
    }

    :deep(.el-table) {
      --el-table-bg-color: transparent;
      --el-table-tr-bg-color: transparent;
      --el-table-header-bg-color: rgba(255, 255, 255, 0.06);
      --el-table-row-hover-bg-color: rgba(64, 158, 255, 0.08);

      th { color: #c0c4cc !important; }
      td { color: #e2e8f0 !important; }
    }
  }

  @media (max-width: 900px) {
    .optimize-layout {
      grid-template-columns: 1fr;
    }
  }
}
</style>
