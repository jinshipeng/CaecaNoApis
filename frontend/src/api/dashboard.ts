import request from './request'
import type { DashboardStats, ScreenData, PlanningSummary, SupplierPerformance, InventoryAlert, OrderAnalytics } from '@/types/api'

type SilentRequestOptions = {
  skipErrorHandler?: boolean
}

export const getDashboardStats = (options?: SilentRequestOptions): Promise<DashboardStats> => {
  return request.get<DashboardStats>('/dashboard/stats/', { showLoading: false, ...options })
}

export const getPlanningSummary = (strategy?: string): Promise<PlanningSummary> => {
  const params = strategy ? { strategy } : {}
  return request.get<PlanningSummary>('/orders/planning_summary/', { showLoading: false, timeout: 300000, params })
}

export const getSupplierPerformance = (): Promise<SupplierPerformance[]> => {
  return request.get<SupplierPerformance[]>('/analytics/supplier-performance/', { showLoading: false })
}

export const getInventoryAlerts = (): Promise<InventoryAlert[]> => {
  return request.get<InventoryAlert[]>('/analytics/inventory-alerts/', { showLoading: false })
}

export const getOrderAnalytics = (): Promise<OrderAnalytics> => {
  return request.get<OrderAnalytics>('/analytics/order-analytics/', { showLoading: false })
}

export const getOrderPriority = (): Promise<Array<{
  id: number
  order_no: string
  priority: number
  score: number
}>> => {
  return request.get('/analytics/priority/', { showLoading: false })
}

export const getDeliveryRisk = (): Promise<Array<{
  id: number
  order_no: string
  risk_level: 'high' | 'medium' | 'low'
  risk_score: number
  factors: string[]
}>> => {
  return request.get('/analytics/delivery-risk/', { showLoading: false })
}

export const getOptimizationStrategy = (): Promise<Record<string, unknown>> => {
  return request.get('/optimization/strategy/', { showLoading: false })
}

export const getSystemHealth = (): Promise<{
  status: 'healthy' | 'degraded' | 'down'
  database: boolean
  cache: boolean
  uptime: number
}> => {
  return request.get('/system/health/', { showLoading: false })
}

export const getSystemConfig = (): Promise<Record<string, unknown>> => {
  return request.get('/system/config/', { showLoading: false })
}

export const getScreenData = (options?: SilentRequestOptions): Promise<ScreenData> => {
  return request.get<ScreenData>('/screen/data/', { showLoading: false, ...options })
}

export const getImportHistory = (): Promise<{
  count: number
  results: Array<{
    id: number
    filename: string
    type: string
    status: string
    count: number
    time: string
    imported_by: string
  }>
}> => {
  return request.get('/import_data/history/', { showLoading: false })
}

export const getDeliveryChangeAlerts = (): Promise<{
  count: number
  orders: Array<{
    id: number
    order_no: string
    customer_name: string
    change_count: number
    latest_change_date: string
  }>
}> => {
  return request.get('/analytics/delivery-change-alerts/', { showLoading: false })
}
