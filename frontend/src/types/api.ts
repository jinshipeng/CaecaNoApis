export interface LoginData {
  username: string
  password: string
}

export interface LoginResponse {
  token: string
  user_id?: number
  username?: string
  email?: string
  user?: UserInfo
}

export interface UserInfo {
  id: number
  username: string
  full_name: string
  department: string
  email?: string
  phone?: string
  permissions?: string[]
  is_active?: boolean
  last_login?: string
  date_joined?: string
}

export interface Material {
  id: number
  material_code: string
  material_name: string
  material_type: 'raw' | 'semi' | 'finished'
  unit: string
  standard_cost: number
  sales_price: number
  safety_stock: number
  is_active: boolean
  shelf_life?: number
  lead_time?: number
  min_order_qty?: number
  min_production_qty?: number
  actual_stock?: number
  created_at?: string
  updated_at?: string
}

export interface CustomerRef {
  id: number
  customer_code: string
  customer_name: string
}

export interface SalesOrder {
  id: number
  order_no: string
  customer?: { customer_name: string }
  customer_name: string
  material: Material | number
  material_id?: number | null
  material_code?: string
  material_name?: string
  quantity: number
  unit_price: number
  total_amount: number
  order_date?: string | null
  demand_date: string
  status: 'pending' | 'confirmed' | 'in_production' | 'processing' | 'allocated' | 'partial' | 'complete' | 'shipped' | 'delivered' | 'cancelled'
  priority: number
  shipping_method?: 'sea' | 'air'
  shipping_days?: number
  production_lead_time?: number
  is_forecast?: boolean
  allow_early_delivery?: boolean
  earliest_delivery_date?: string
  factory_code?: string
  created_at?: string
  updated_at?: string
}

export interface Inventory {
  id: number
  material: Material | number
  material_code?: string
  material_name?: string
  inventory_type: 'local' | 'transit' | 'supplier' | 'finished' | 'semi'
  quantity: number
  hold_quantity?: number
  available_quantity?: number
  warehouse?: string
  location?: string
  batch_no?: string
  expiry_date?: string
  is_hold: boolean
  hold_reason?: string
  hold_until?: string
  data_date?: string
  factory_code?: string
  created_at?: string
  updated_at?: string
}

export interface Supplier {
  id: number
  supplier_code: string
  supplier_name: string
  contact_person?: string
  phone?: string
  email?: string
  address?: string
  rating: 'A' | 'B' | 'C' | 'D'
  delivery_reliability?: number
  normal_lead_time?: number
  payment_terms?: string
  min_order_qty?: number
  capacity_level?: string
  cooperation_years?: number
  warranty_months?: number
  on_time_delivery_rate?: number
  is_active: boolean
  created_at?: string
  updated_at?: string
}

export interface Customer {
  id: number
  customer_code: string
  customer_name: string
  contact_person?: string
  phone?: string
  email?: string
  address?: string
  credit_limit: number
  customer_type?: string
  payment_terms?: string
  customer_level?: 'vip' | 'important' | 'normal'
  delivery_priority?: number
  is_active: boolean
  created_at?: string
  updated_at?: string
}

export interface PurchaseOrder {
  id: number
  po_no: string
  supplier?: Supplier | number
  supplier_id?: number | null
  supplier_code?: string
  supplier_name?: string
  material?: Material | number
  material_id?: number | null
  material_code?: string
  material_name?: string
  quantity: number
  unit_price: number
  total_amount: number
  order_date: string
  delivery_date: string
  actual_delivery_date?: string
  status: 'draft' | 'pending' | 'issued' | 'confirmed' | 'in_production' | 'partial' | 'partial_shipped' | 'shipped' | 'processing' | 'completed' | 'cancelled'
  remarks?: string
  created_at?: string
  updated_at?: string
}

export interface BOM {
  id: number
  parent_code: string
  parent_name: string
  child_code: string
  child_name: string
  quantity: number
  unit: string
  bom_level: number
  usage_ratio: number
  scrap_rate: number
  alternative_group: string
  alternative_priority?: number
  alternative_ratio?: number
  is_active: boolean
  factory_code?: string
  version?: number
  ecn_no?: string
  ecn_date?: string
  ecn_reason?: string
  is_configurable?: boolean
  config_group?: string
  config_options?: Record<string, unknown>
  created_at?: string
  updated_at?: string
}

export interface Capacity {
  id: number
  work_center: string
  material?: Material | number
  material_code?: string
  material_name?: string
  daily_capacity: number
  weekly_capacity: number
  is_active: boolean
  created_at?: string
  updated_at?: string
}

export interface ApiResponse<T = unknown> {
  count: number
  next?: string | null
  previous?: string | null
  results: T[]
}

export interface PaginatedResponse<T = unknown> {
  count: number
  next?: string | null
  previous?: string | null
  results: T[]
  total_pages?: number
  current_page?: number
  page_size?: number
}

export interface ApiSuccessResponse<T = unknown> {
  code: number
  message: string
  data: T
}

export interface ApiErrorResponse {
  code: number
  message: string
  detail?: string
  errors?: Record<string, string[]>
}

export interface PageParams {
  page?: number
  page_size?: number
  search?: string
  ordering?: string
  status?: string
  material_type?: string
  inventory_type?: string
  [key: string]: unknown
}

export interface FailureAnalysis {
  total_failed: number
  by_reason: Record<string, number>
  details: Record<string, Array<Record<string, unknown>>>
}

export interface PlanningSummary {
  total_orders: number
  complete_orders: number
  partial_orders: number
  pending_orders: number
  avg_complete_rate: number
  complete_rate: number
  total_shortage_orders: number
  total_promise_changes: number
  stable_orders: number
  avg_supplier_reliability: number
  total_safety_stock_usage: number
  failure_analysis: FailureAnalysis
  total_critical_shortages?: number
  total_urgent_shortages?: number
  in_progress_orders?: number
  kit_rate?: number
  jit_optimization?: Record<string, unknown>
  ai_analysis?: Record<string, unknown> | null
  procurement_plan?: Record<string, unknown> | null
  release_records?: Array<Record<string, unknown>>
  delivery_violations?: Array<Record<string, unknown>>
  // v4-BOM 替换统计
  substitution_applied?: boolean
  substitution_stats?: {
    checked: number
    found: number
    applied: number
    shortage_reduced: number
    orders_affected: number
  }
}

export interface ShortageItem {
  material_id: number
  material_code: string
  material_name: string
  required: number
  allocated: number
  shortage: number
}

export interface DashboardStats {
  total_orders: number
  completed_orders: number
  in_progress_orders: number
  kit_rate: number
  complete_orders?: number
  partial_orders?: number
  pending_orders?: number
  capacity_utilization?: number
  total_materials?: number
  total_suppliers?: number
  total_inventory?: number
  total_boms?: number
  delivery_rate?: number
  inventory_alerts?: InventoryAlerts
  recent_orders?: RecentOrderItem[]
  recent_plans?: number
}

export interface InventoryAlerts {
  low_inventory?: unknown[]
  expiring?: unknown[]
  overstock?: unknown[]
  total_alerts?: number
}

export interface RecentOrderItem {
  id: number
  order_no: string
  customer_name?: string
  material_code?: string
  material_name?: string
  quantity: number
  demand_date: string
  status: string
}

export interface KpiItem {
  title: string
  value: string | number
  change: string
  color: string
}

export interface InventoryKpiItem {
  title: string
  value: number | string
  color?: string
  unit?: string
}

export interface MaterialStatusItem {
  name: string
  value: number
  color?: string
}

export interface QualityRadarData {
  indicators: Array<{ name: string; max: number }>
  current_month: number[]
  last_month: number[]
}

export interface AlertItem {
  id: number
  type: 'primary' | 'success' | 'warning' | 'info' | 'danger'
  message: string
  time: string
}

export interface CapacityData {
  utilization: number
  total_capacity: number
  workcenter_count: number
}

export interface ScreenRecentOrderItem {
  order_no: string
  customer: string
  status: string
  amount: number
  progress: number
}

export interface ScreenData {
  kpi_data?: KpiItem[]
  inventory_kpi?: InventoryKpiItem[]
  order_trend?: OrderTrendData
  order_status?: Record<string, number>
  // 物料计划后的真实齐套状态（来自Cache或MaterialPlanResult）
  planning_status?: {
    total: number
    complete: number
    partial: number
    none: number
    avg_complete_rate: number
    has_data: boolean
  }
  material_status?: MaterialStatusItem[]
  supplier_distribution?: { cities: string[]; values: number[] }
  capacity?: CapacityData
  quality_radar?: QualityRadarData
  recent_orders?: ScreenRecentOrderItem[]
  alerts?: AlertItem[]
  last_updated?: string
}

export interface OrderTrendData {
  categories: string[]
  sales?: number[]
  purchase?: number[]
  order_data?: number[]
  complete_data?: number[]
  total?: number[]
  completed?: number[]
}

export interface OrderStatusData {
  pending: number
  confirmed?: number
  in_production?: number
  processing?: number
  allocated: number
  partial: number
  complete: number
  shipped?: number
  delivered?: number
  cancelled?: number
}

export interface SupplierPerformance {
  supplier_id: number
  supplier_name: string
  on_time_delivery_rate: number
  quality_rate: number
  total_orders: number
  rating: string
}

export interface InventoryAlert {
  id: number
  material_id: number
  material_code: string
  material_name: string
  current_quantity: number
  safety_stock: number
  alert_type: 'low_stock' | 'overstock' | 'expiry_risk'
  severity: 'high' | 'medium' | 'low'
  message: string
}

export interface OrderAnalytics {
  total_orders: number
  completed_orders: number
  pending_orders: number
  completion_rate: number
  average_lead_time: number
  monthly_trend: Array<{
    month: string
    count: number }>
}
