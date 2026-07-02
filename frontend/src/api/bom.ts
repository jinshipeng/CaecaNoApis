import request from './request'
import type { BOM, ApiResponse, PageParams } from '@/types/api'

type SilentRequestOptions = {
  skipErrorHandler?: boolean
}

export const getBOMList = (params?: PageParams): Promise<ApiResponse<BOM>> => {
  return request.get<ApiResponse<BOM>>('/boms/', { params, showLoading: false })
}

export const getBOM = (id: number): Promise<BOM> => {
  return request.get<BOM>(`/boms/${id}/`)
}

export const createBOM = (data: Partial<BOM>, options?: SilentRequestOptions): Promise<BOM> => {
  return request.post<BOM>('/boms/', data, options)
}

export const updateBOM = (id: number, data: Partial<BOM>, options?: SilentRequestOptions): Promise<BOM> => {
  return request.put<BOM>(`/boms/${id}/`, data, options)
}

export const deleteBOM = (id: number): Promise<void> => {
  return request.delete(`/boms/${id}/`)
}
