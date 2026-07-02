import request from './request'
import type { SalesOrder, ApiResponse, PageParams } from '@/types/api'

type SilentRequestOptions = {
  skipErrorHandler?: boolean
}

export interface MaterialPlanDetail {
  id: number
  material_code: string
  material_name: string
  demand: number
  stock: number
  shortage: number
  status: 'shortage' | 'warning' | 'sufficient'
  priority: 'high' | 'normal' | 'low'
  safety_stock: number
}

export interface AIAllocationAnalysis {
  allocation_quality: number
  inventory_utilization: number
  expiry_risk: number
  supplier_risk: number
  potential_risks: Array<{
    material_id: number
    risk_type: string
    description: string
    severity: string
  }>
  suggestions: Array<{
    material_id: number
    suggestion_type: string
    description: string
  }>
  expiring_items: Array<{
    material_id: number
    expiry_date: string
    allocated_quantity: number
    days_until_expiry: number
  }>
}

export interface AIAllocationResponse {
  analysis: AIAllocationAnalysis
  summary: {
    total_orders: number
    total_allocations: number
    allocation_quality: number
    inventory_utilization: number
    potential_risk_count: number
    suggestion_count: number
  }
}

export const getOrderList = (params?: PageParams, options?: SilentRequestOptions): Promise<ApiResponse<SalesOrder>> => {
  return request.get<ApiResponse<SalesOrder>>('/orders/', {
    params: { ...params, _useCache: false },
    showLoading: false,
    ...options
  })
}

export const getOrder = (id: number): Promise<SalesOrder> => {
  return request.get<SalesOrder>(`/orders/${id}/`)
}

export const createOrder = (data: Partial<SalesOrder>): Promise<SalesOrder> => {
  return request.post<SalesOrder>('/orders/', data)
}

export const updateOrder = (id: number, data: Partial<SalesOrder>): Promise<SalesOrder> => {
  return request.put<SalesOrder>(`/orders/${id}/`, data)
}

export const deleteOrder = (id: number): Promise<void> => {
  return request.delete(`/orders/${id}/`)
}

export const runPlanning = (orderIds?: number[], strategy?: string, enableAIAnalysis?: boolean): Promise<{
  message: string
  processed_orders: number
}> => {
  return request.post('/orders/run_planning/', { order_ids: orderIds, strategy: strategy || 'delivery_first', enable_ai_analysis: enableAIAnalysis || false }, { timeout: 900000, showLoading: false })
}

export const getShortageReport = (strategy?: string): Promise<Array<{
  order_id: number
  order_no: string
  material_code: string
  material_name: string
  required: number
  allocated: number
  shortage: number
  latest_purchase_date: string | null
  days_to_latest_purchase: number | null
  urgency_level: string
  urgency_label: string
  recommended_action: string | null
  recommended_supplier: string | null
  safety_stock: number
  lead_time: number
  suppliers: Array<Record<string, unknown>>
  alternative_materials: Array<Record<string, unknown>>
}>> => {
  const params = strategy ? { strategy } : {}
  return request.get('/orders/shortage_report/', { showLoading: false, timeout: 300000, params })
}

export const getMaterialPlanDetail = (strategy?: string): Promise<MaterialPlanDetail[]> => {
  const params = strategy ? { strategy } : {}
  return request.get<MaterialPlanDetail[]>('/orders/material_plan_detail/', { showLoading: false, timeout: 300000, params })
}

export const getAIAllocationAnalysis = (): Promise<AIAllocationResponse> => {
  return request.get<AIAllocationResponse>('/orders/ai_allocation_analysis/', { showLoading: false })
}
