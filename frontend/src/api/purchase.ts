import request from './request'
import type { PurchaseOrder, ApiResponse, PageParams } from '@/types/api'

type SilentRequestOptions = {
  skipErrorHandler?: boolean
}

export const getPurchaseOrderList = (params?: PageParams, options?: SilentRequestOptions): Promise<ApiResponse<PurchaseOrder>> => {
  return request.get<ApiResponse<PurchaseOrder>>('/purchase-orders/', { params, showLoading: false, ...options })
}

export const getPurchaseOrder = (id: number): Promise<PurchaseOrder> => {
  return request.get<PurchaseOrder>(`/purchase-orders/${id}/`)
}

export const createPurchaseOrder = (data: Partial<PurchaseOrder>): Promise<PurchaseOrder> => {
  return request.post<PurchaseOrder>('/purchase-orders/', data)
}

export const updatePurchaseOrder = (id: number, data: Partial<PurchaseOrder>): Promise<PurchaseOrder> => {
  return request.put<PurchaseOrder>(`/purchase-orders/${id}/`, data)
}

export const deletePurchaseOrder = (id: number): Promise<void> => {
  return request.delete(`/purchase-orders/${id}/`)
}
