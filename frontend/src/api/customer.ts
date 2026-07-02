import request from './request'
import type { Customer, ApiResponse, PageParams } from '@/types/api'

export const getCustomerList = (params?: PageParams): Promise<ApiResponse<Customer>> => {
  return request.get<ApiResponse<Customer>>('/customers/', { params, showLoading: false })
}

export const getCustomer = (id: number): Promise<Customer> => {
  return request.get<Customer>(`/customers/${id}/`)
}

export const createCustomer = (data: Partial<Customer>): Promise<Customer> => {
  return request.post<Customer>('/customers/', data)
}

export const updateCustomer = (id: number, data: Partial<Customer>): Promise<Customer> => {
  return request.put<Customer>(`/customers/${id}/`, data)
}

export const deleteCustomer = (id: number): Promise<void> => {
  return request.delete(`/customers/${id}/`)
}
