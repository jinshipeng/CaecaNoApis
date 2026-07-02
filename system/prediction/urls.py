from django.urls import path, include
from . import views
from .views.auth_views import register, captcha
from .views.dashboard_views import dashboard_data
from .views.report_views import (
    report_dashboard,
    order_fulfillment_report,
    inventory_turnover_report,
    supplier_performance_report,
    production_capacity_report,
    export_shortage_detail,
    export_management_summary,
    export_procurement_plan,
    export_full_package,
)
from .views.screen_views import visual_screen
from .views.data_init_views import data_init
from .views.audit_views import audit_log_list, audit_log_export
from .views.backup_views import backup_list, create_backup, restore_backup, delete_backup
from .views.notification_views import (
    notification_list,
    notification_read,
    notification_read_all,
    notification_count,
    notification_check_alerts,
)
from .views.help_views import help_center, help_search

# 四大核心KPI统计API
from .views.core_kpi_views import (
    CoreKPIDashboardAPI,
    KPITrendAPI,
    KPIComparisonAPI,
    RLTrainingAPI,
    KPIBaselineComparisonAPI,
)

# AI预测与智能分析模块
from .views.ai_views import (
    ai_demand_forecast,
    ai_anomaly_detection,
    ai_comprehensive_analysis,
    whatif_simulation,
    rl_recommendation,
    rl_train,
    multi_objective_optimize,
)
from .views.scheduling_views import (
    api_generate_schedule,
    api_gantt_data,
    api_whatif_schedule,
    api_optimize_schedule,
    api_backfill_material_plan,
)

app_name = 'prediction'

urlpatterns = [
    # 系统首页
    path('', views.dashboard, name='dashboard'),
    
    # 用户认证
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),
    path('register/', register, name='register'),
    path('captcha/', captcha, name='captcha'),

    # 数据导入
    path('import_data/', views.import_data, name='import_data'),
    path('import_data/batch/', views.batch_import_all, name='batch_import_all'),
    path('import_data/refresh/', views.refresh_csv_list, name='refresh_csv_list'),
    path('import_data/history/', views.import_history, name='import_history'),
    
    # 数据初始化
    path('data_init/', data_init, name='data_init'),
    
    # 系统日志
    path('logs/', views.view_logs, name='logs'),

    path('audit/', audit_log_list, name='audit_log_list'),
    path('audit/export/', audit_log_export, name='audit_log_export'),
    
    # 可视化大屏
    path('screen/', visual_screen, name='visual_screen'),
    
    # 报表中心
    path('reports/', report_dashboard, name='report_dashboard'),
    path('reports/order_fulfillment/', order_fulfillment_report, name='order_fulfillment_report'),
    path('reports/inventory_turnover/', inventory_turnover_report, name='inventory_turnover_report'),
    path('reports/supplier_performance/', supplier_performance_report, name='supplier_performance_report'),
    path('reports/production_capacity/', production_capacity_report, name='production_capacity_report'),

    # 增强报表导出API
    path('reports/export/shortage-detail/', export_shortage_detail, name='export_shortage_detail'),
    path('reports/export/management-summary/', export_management_summary, name='export_management_summary'),
    path('reports/export/procurement-plan/', export_procurement_plan, name='export_procurement_plan'),
    path('reports/export/full-package/', export_full_package, name='export_full_package'),
    
    # 排产计划
    path('scheduling/', views.scheduling, name='scheduling'),
    path('scheduling/export/', views.export_schedule, name='export_schedule'),
    # 高级排程引擎API (AdvancedScheduler)
    path('api/scheduling/generate/', api_generate_schedule, name='api_generate_schedule'),
    path('api/scheduling/gantt-data/', api_gantt_data, name='api_gantt_data'),
    path('api/scheduling/whatif/', api_whatif_schedule, name='api_whatif_schedule'),
    path('api/scheduling/optimize/', api_optimize_schedule, name='api_optimize_schedule'),
    path('api/scheduling/backfill/', api_backfill_material_plan, name='api_backfill_material_plan'),

    # ========== 主数据管理模块 ==========
    # 物料管理
    path('master/material/', views.material_list, name='material_list'),
    path('master/material/add/', views.material_add, name='material_add'),
    path('master/material/edit/<int:pk>/', views.material_edit, name='material_edit'),
    path('master/material/delete/<int:pk>/', views.material_delete, name='material_delete'),
    
    # 供应商管理
    path('master/supplier/', views.supplier_list, name='supplier_list'),
    path('master/supplier/add/', views.supplier_add, name='supplier_add'),
    path('master/supplier/edit/<int:pk>/', views.supplier_edit, name='supplier_edit'),
    path('master/supplier/delete/<int:pk>/', views.supplier_delete, name='supplier_delete'),
    
    # 客户管理
    path('master/customer/', views.customer_list, name='customer_list'),
    path('master/customer/add/', views.customer_add, name='customer_add'),
    path('master/customer/edit/<int:pk>/', views.customer_edit, name='customer_edit'),
    path('master/customer/delete/<int:pk>/', views.customer_delete, name='customer_delete'),
    
    # BOM管理
    path('master/bom/', views.bom_list, name='bom_list'),
    path('master/bom/add/', views.bom_add, name='bom_add'),
    path('master/bom/edit/<int:pk>/', views.bom_edit, name='bom_edit'),
    path('master/bom/delete/<int:pk>/', views.bom_delete, name='bom_delete'),

    # ========== 供应链管理模块 ==========
    # 库存管理
    path('supply/inventory/', views.inventory_list, name='inventory_list'),
    path('supply/inventory/add/', views.inventory_add, name='inventory_add'),
    path('supply/inventory/edit/<int:pk>/', views.inventory_edit, name='inventory_edit'),
    path('supply/inventory/delete/<int:pk>/', views.inventory_delete, name='inventory_delete'),
    
    # 采购订单
    path('supply/purchase/', views.purchase_order_list, name='purchase_order_list'),
    path('supply/purchase/add/', views.purchase_order_add, name='purchase_order_add'),
    path('supply/purchase/edit/<int:pk>/', views.purchase_order_edit, name='purchase_order_edit'),
    path('supply/purchase/delete/<int:pk>/', views.purchase_order_delete, name='purchase_order_delete'),
    
    # 销售订单
    path('supply/sales/', views.sales_order_list, name='sales_order_list'),
    path('supply/sales/add/', views.sales_order_add, name='sales_order_add'),
    path('supply/sales/edit/<int:pk>/', views.sales_order_edit, name='sales_order_edit'),
    path('supply/sales/delete/<int:pk>/', views.sales_order_delete, name='sales_order_delete'),
    
    # 供应商承诺
    path('supply/commitment/', views.commitment_list, name='commitment_list'),

    # 生产计划模块 ==========
    path('plan/material/', views.material_planning_dashboard, name='material_planning_dashboard'),
    path('plan/material/run/', views.run_material_planning, name='run_material_planning'),
    path('plan/material/result/', views.material_planning_result, name='material_planning_result'),
    path('plan/material/shortage/', views.shortage_report, name='shortage_report'),
    path('plan/material/export/', views.export_planning_result, name='export_planning_result'),
    path('plan/material/logs/', views.planning_logs, name='planning_logs'),
    path('plan/material/orders/', views.order_list, name='order_list'),
    path('plan/material/orders/<int:order_id>/', views.order_detail, name='order_detail'),
    
    # 产能管理
    path('plan/capacity/', views.capacity_list, name='capacity_list'),
    path('plan/capacity/add/', views.capacity_add, name='capacity_add'),
    path('plan/capacity/edit/<int:pk>/', views.capacity_edit, name='capacity_edit'),
    path('plan/capacity/delete/<int:pk>/', views.capacity_delete, name='capacity_delete'),

    # 数据备份
    path('backup/', backup_list, name='backup_list'),
    path('backup/create/', create_backup, name='create_backup'),
    path('backup/restore/<str:filename>/', restore_backup, name='restore_backup'),
    path('backup/delete/<str:filename>/', delete_backup, name='delete_backup'),

    # 通知消息
    path('notifications/', notification_list, name='notification_list'),
    path('notifications/read/<int:pk>/', notification_read, name='notification_read'),
    path('notifications/read-all/', notification_read_all, name='notification_read_all'),
    path('notifications/count/', notification_count, name='notification_count'),
    path('notifications/check-alerts/', notification_check_alerts, name='notification_check_alerts'),

    path('help/', help_center, name='help_center'),
    path('help/search/', help_search, name='help_search'),

    # ========== API V1 版本控制 ==========
    path('api/v1/', include([
        # Dashboard API
        path('dashboard_data/', dashboard_data, name='dashboard_data'),
        
        # AI预测与智能分析模块
        path('ai/demand-forecast/', ai_demand_forecast, name='ai_demand_forecast'),
        path('ai/anomaly-detection/', ai_anomaly_detection, name='ai_anomaly_detection'),
        path('ai/comprehensive-analysis/', ai_comprehensive_analysis, name='ai_comprehensive_analysis'),
        path('ai/whatif-simulation/', whatif_simulation, name='whatif_simulation'),
        # RL强化学习智能体
        path('ai/rl/recommendation/', rl_recommendation, name='rl_recommendation'),
        path('ai/rl/train/', rl_train, name='rl_train'),
        # NSGA-II多目标优化
        path('ai/multi-objective-optimize/', multi_objective_optimize, name='multi_objective_optimize'),
        
        # 审计日志 API
        path('audit/', audit_log_list, name='api_audit_log_list'),
        path('audit/export/', audit_log_export, name='api_audit_log_export'),
        
        # 备份 API
        path('backup/', backup_list, name='api_backup_list'),
        path('backup/create/', create_backup, name='api_create_backup'),
        path('backup/restore/<str:filename>/', restore_backup, name='api_restore_backup'),
        path('backup/delete/<str:filename>/', delete_backup, name='api_delete_backup'),
        
        # 通知 API
        path('notifications/', notification_list, name='api_notification_list'),
        path('notifications/read/<int:pk>/', notification_read, name='api_notification_read'),
        path('notifications/read-all/', notification_read_all, name='api_notification_read_all'),
        path('notifications/count/', notification_count, name='api_notification_count'),
        path('notifications/check-alerts/', notification_check_alerts, name='api_notification_check_alerts'),

        # 四大核心KPI统计API
        path('kpi/dashboard/', CoreKPIDashboardAPI.as_view(), name='kpi_dashboard'),
        path('kpi/trend/', KPITrendAPI.as_view(), name='kpi_trend'),
        path('kpi/comparison/', KPIComparisonAPI.as_view(), name='kpi_comparison'),
        # RL训练与管理API (ViewSet-based)
        path('rl/', RLTrainingAPI.as_view({'post': 'train', 'get': 'status'}), name='rl_train_status'),
        path('rl/recommend/', RLTrainingAPI.as_view({'post': 'recommend'}), name='rl_recommend'),
        path('rl/sensitivity/', RLTrainingAPI.as_view({'get': 'sensitivity'}), name='rl_sensitivity'),
        path('rl/export-model/', RLTrainingAPI.as_view({'post': 'export_model'}), name='rl_export_model'),
        # KPI基线对比API (ViewSet-based)
        path('kpi/baseline-comparison/', KPIBaselineComparisonAPI.as_view({'get': 'baseline_comparison'}), name='kpi_baseline_comparison'),
    ])),
]