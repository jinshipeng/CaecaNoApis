import request from './request'
import type { LoginData, LoginResponse, UserInfo } from '@/types/api'

export const login = (data: LoginData): Promise<LoginResponse> => {
  return request.post<LoginResponse>('/auth/login/', data, { showLoading: false, deduplication: false })
}

export const logout = (): Promise<void> => {
  return request.post('/auth/logout/', {}, { showLoading: false, deduplication: false })
}

export const getUserInfo = (): Promise<UserInfo> => {
  return request.get<UserInfo>('/auth/user/', { showLoading: false, deduplication: false })
}

export const refreshToken = (): Promise<{ token: string }> => {
  return request.post('/auth/refresh/', {}, { showLoading: false, deduplication: false })
}
