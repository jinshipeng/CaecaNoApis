import request from './request'

type SilentRequestOptions = {
  skipErrorHandler?: boolean
}

// 排产甘特图数据（高级排程引擎）
export function getGanttData() {
  return request.get('/scheduling/gantt-data/')
}

// 高级排程引擎 — 生成完整排程计划
export function generateSchedule(horizonDays: number = 30) {
  return request.post('/scheduling/generate/', { horizon_days: horizonDays })
}

// 高级排程引擎 — What-If模拟
export function whatifSchedule(scenarioType: string, parameters: Record<string, any>) {
  return request.post('/scheduling/whatif/', { scenario_type: scenarioType, ...parameters })
}

// 高级排程引擎 — 多目标优化
export function optimizeSchedule(objectives: string[] = ['delivery', 'utilization', 'changeover']) {
  return request.post('/scheduling/optimize/', { objectives: objectives.join(',') })
}

// 高级排程引擎 — 反哺物料计划
export function backfillMaterialPlan() {
  return request.post('/scheduling/backfill/')
}

// 模拟历史
export function getSimulationHistory() {
  return request.get('/simulation/history/')
}

export function saveSimulationResult(data: any) {
  return request.post('/simulation/history/', data)
}

// 自动优先级调整
export function autoAdjustPriority() {
  return request.post('/analytics/auto-priority/')
}

// 砍单物料释放
export function cancelOrderWithRelease(orderId: number) {
  return request.post('/orders/cancel_with_release/', { order_id: orderId })
}

// 计算模式
export function getComputationMode(options?: SilentRequestOptions) {
  return request.get('/planning/computation-mode/', options)
}

export function setComputationMode(mode: string) {
  return request.post('/planning/computation-mode/', { mode })
}

// 模型重训练
export function getRetrainStatus(options?: SilentRequestOptions) {
  return request.get('/ai/auto-retrain/', options)
}

export function triggerRetrain() {
  return request.post('/ai/auto-retrain/')
}

// ============================================================
// RL 强化学习智能体 API
// ============================================================

/** 获取基于真实数据的RL智能推荐 */
export function getRLRecommendation() {
  return request.get('/ai/rl/recommendation/')
}

/** 基于历史数据训练RL Agent */
export function trainRLAgent(days: number = 30) {
  return request.post('/ai/rl/train/', { days })
}

// ============================================================
// NSGA-II 多目标优化 API
// ============================================================

/** 运行NSGA-II多目标优化 */
export function runMultiObjectiveOptimize(params?: { population_size?: number; generations?: number; preference?: string }) {
  return request.post('/ai/multi-objective-optimize/', params || {})
}
