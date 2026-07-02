import request from './request'
import type { Inventory, ApiResponse, PageParams } from '@/types/api'

type SilentRequestOptions = {
  skipErrorHandler?: boolean
}

export const getInventoryList = (params?: PageParams, options?: SilentRequestOptions): Promise<ApiResponse<Inventory>> => {
  return request.get<ApiResponse<Inventory>>('/inventory/', {
    params: { ...params, _useCache: false },
    showLoading: false,
    ...options
  })
}

export const getInventory = (id: number): Promise<Inventory> => {
  return request.get<Inventory>(`/inventory/${id}/`)
}

export const createInventory = (data: Partial<Inventory>): Promise<Inventory> => {
  return request.post<Inventory>('/inventory/', data)
}

export const updateInventory = (id: number, data: Partial<Inventory>): Promise<Inventory> => {
  return request.put<Inventory>(`/inventory/${id}/`, data)
}

export const deleteInventory = (id: number): Promise<void> => {
  return request.delete(`/inventory/${id}/`)
}

export const batchDeleteInventory = (ids: number[]): Promise<{
  deleted_count: number
}> => {
  return request.post('/inventory/batch_delete/', { ids })
}

export const exportInventory = (params?: PageParams): Promise<Blob> => {
  return request.get('/inventory/export/', {
    params,
    responseType: 'blob'
  })
}

export const getInventoryStats = (): Promise<{
  total: number
  low_count: number
  warning_count: number
  normal_count: number
  with_hold: number
}> => {
  // 使用原生 fetch + cache:'no-store' 彻底绕过所有层级缓存（浏览器/代理/CDN）
  const token = localStorage.getItem('token')
  return fetch(`/api/inventory/stats/?_t=${Date.now()}`, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Token ${token}` } : {})
    },
    cache: 'no-store'
  }).then(res => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  })
}

// 物料-仓库热力图矩阵数据（纯真实数据库数据）
export const getMaterialWarehouseHeatmap = (): Promise<{
  warehouses: string[]
  materials: string[]
  cells: Record<string, Record<string, { value: number; ratio: number; status: string }>>
  material_warehouse_map: Record<string, string[]>
  material_total_stats: Record<string, { total_qty: number; total_ratio: number; global_status: string; safety: number }>
  stats: { sufficient: number; low: number; shortage: number; none: number; total_records: number }
}> => {
  const token = localStorage.getItem('token')
  return fetch(`/api/screen/heatmap/?_t=${Date.now()}`, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Token ${token}` } : {})
    },
    cache: 'no-store'
  }).then(res => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  })
}

// 产能利用率热力图数据（优先真实WorkCenter，否则高质量模拟）
export const getCapacityUtilizationHeatmap = (): Promise<{
  workcenters: string[]
  dates: string[]
  data: Array<{ workcenter: string; date: string; utilization: number; daily_capacity?: number; headcount?: number }>
  stats: { normal: number; high: number; over: number }
  source: 'database' | 'simulated'
}> => {
  const token = localStorage.getItem('token')
  return fetch(`/api/screen/capacity-heatmap/?_t=${Date.now()}`, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Token ${token}` } : {})
    },
    cache: 'no-store'
  }).then(res => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  })
}
