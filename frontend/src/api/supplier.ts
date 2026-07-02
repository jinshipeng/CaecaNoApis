import request from './request'
import type { Supplier, ApiResponse, PageParams } from '@/types/api'

type SilentRequestOptions = {
  skipErrorHandler?: boolean
}

export const getSupplierList = (params?: PageParams, options?: SilentRequestOptions): Promise<ApiResponse<Supplier>> => {
  return request.get<ApiResponse<Supplier>>('/suppliers/', {
    params: { ...params, _useCache: false },
    showLoading: false,
    ...options
  })
}

export const getSupplier = (id: number): Promise<Supplier> => {
  return request.get<Supplier>(`/suppliers/${id}/`)
}

export const createSupplier = (data: Partial<Supplier>): Promise<Supplier> => {
  return request.post<Supplier>('/suppliers/', data)
}

export const updateSupplier = (id: number, data: Partial<Supplier>): Promise<Supplier> => {
  return request.put<Supplier>(`/suppliers/${id}/`, data)
}

export const deleteSupplier = (id: number): Promise<void> => {
  return request.delete(`/suppliers/${id}/`)
}
