import request from './request'
import type { Capacity, ApiResponse, PageParams } from '@/types/api'

export interface CapacityFormData {
  work_center: string
  material_id?: number
  daily_capacity: number
  weekly_capacity: number
  is_active?: boolean
}

export function getCapacityList(params?: PageParams): Promise<ApiResponse<Capacity>> {
  return request.get<ApiResponse<Capacity>>('/capacity/', { params, showLoading: false })
}

export function getCapacity(id: number): Promise<Capacity> {
  return request.get<Capacity>(`/capacity/${id}/`)
}

export function createCapacity(data: CapacityFormData): Promise<Capacity> {
  return request.post<Capacity>('/capacity/', data)
}

export function updateCapacity(id: number, data: Partial<CapacityFormData>): Promise<Capacity> {
  return request.put<Capacity>(`/capacity/${id}/`, data)
}

export function deleteCapacity(id: number): Promise<void> {
  return request.delete(`/capacity/${id}/`)
}

export const getCapacityStats = (): Promise<{
  total: number
  active_count: number
  inactive_count: number
  total_daily_capacity: number
  total_weekly_capacity: number
  work_center_count: number
}> => {
  return request.get('/capacity/stats/')
}
