"""
系统常量定义 - 集中管理状态列表、阈值等常量，避免硬编码散落各处
"""

# ========== 订单状态常量 ==========
# 活跃订单状态（未完成）
ORDER_ACTIVE_STATUSES = ['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
# 已完成订单状态
ORDER_COMPLETED_STATUSES = ['complete', 'completed', 'shipped', 'delivered']
# 所有有效状态（排除已取消）
ORDER_VALID_STATUSES = ORDER_ACTIVE_STATUSES + ORDER_COMPLETED_STATUSES
# 待处理状态（可执行物料计划）
ORDER_PLANNABLE_STATUSES = ['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']

# ========== 采购订单状态 ==========
PO_ACTIVE_STATUSES = ['draft', 'pending', 'issued', 'confirmed', 'in_production', 'partial', 'partial_shipped', 'processing']
PO_COMPLETED_STATUSES = ['shipped', 'completed']

# ========== 物流配置 ==========
SHIPPING_DAYS = {
    'sea': 45,   # 海运默认45天
    'air': 3,    # 空运默认3天
}
PRODUCTION_LEAD_TIME = 2  # 生产提前期（天）

# ========== 性能基准 ==========
PERFORMANCE_TARGET = {
    'max_orders': 10000,      # 万级订单
    'max_time_seconds': 3600,  # 1小时内
    'parallel_threshold': 20,   # 超过此数量自动启用并行计算
    'batch_size': 1000,        # 分批处理每批大小
}

# ========== 交期约束 ==========
MAX_DELIVERY_CHANGES = 2  # 每个订单最多允许的交期变更次数

# ========== 风险等级阈值 ==========
RISK_THRESHOLDS = {
    'shortage_rate_low': 0.30,
    'shortage_rate_medium': 0.40,
    'shortage_rate_high': 0.60,
    'stress_low': 0.4,
    'stress_medium': 0.7,
}
