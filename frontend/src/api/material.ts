import request from './request'
import type { Material, ApiResponse, PageParams } from '@/types/api'

type SilentRequestOptions = {
  skipErrorHandler?: boolean
}

export const getMaterialList = (params?: PageParams, options?: SilentRequestOptions): Promise<ApiResponse<Material>> => {
  return request.get<ApiResponse<Material>>('/materials/', {
    params: { ...params, _useCache: false },
    showLoading: false,
    ...options
  })
}

export const getMaterial = (id: number): Promise<Material> => {
  return request.get<Material>(`/materials/${id}/`)
}

export const createMaterial = (data: Partial<Material>, options?: SilentRequestOptions): Promise<Material> => {
  return request.post<Material>('/materials/', data, options)
}

export const updateMaterial = (id: number, data: Partial<Material>, options?: SilentRequestOptions): Promise<Material> => {
  return request.put<Material>(`/materials/${id}/`, data, options)
}

export const deleteMaterial = (id: number): Promise<void> => {
  return request.delete(`/materials/${id}/`)
}

export const importMaterials = (file: File): Promise<{
  success_count: number
  error_count: number
  errors?: Array<{ row: number; errors: string[] }>
}> => {
  const formData = new FormData()
  formData.append('file', file)
  return request.post('/materials/import/', formData, {
    headers: {
      'Content-Type': 'multipart/form-data'
    }
  })
}

export const exportMaterials = (params?: PageParams): Promise<Blob> => {
  return request.get('/materials/export/', {
    params,
    responseType: 'blob'
  })
}
