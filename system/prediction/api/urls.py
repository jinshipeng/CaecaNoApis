from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    MaterialViewSet, SupplierViewSet, CustomerViewSet, OrderViewSet,
    InventoryViewSet, BillOfMaterialsViewSet,
    SupplierCommitmentViewSet, MaterialPlanResultViewSet,
    OrderAllocationViewSet, DashboardStatsView,
    SupplierPerformanceView, InventoryAlertView,
    OrderAnalyticsView, SystemHealthView, SystemConfigView, ValidationView,
    OrderPriorityView, OrderDeliveryRiskView, OptimizationStrategyView,
    PurchaseOrderViewSet, CapacityViewSet, WorkCenterViewSet,
    RootCauseAnalysisView, PlanLogListView,
    AuditLogListView, AutoPriorityAdjustView,
    AutoRetrainView, ComputationModeView,
    DeliveryChangeAlertsView,
    login_view, logout_view, user_info_view, refresh_token_view,
    GanttChartView, SimulationHistoryView,
    HoldAuditLogListView, BOMChangeHistoryListView
)
from ..views.import_views import (
    ImportDataView, RefreshCSVListView, ImportHistoryListView,
    FieldRecognitionView, SmartImportPreviewView
)
from ..views import ai_views
from ..views import scheduling_views
from .screen_views import ScreenDataView, MaterialWarehouseHeatmapView, CapacityUtilizationHeatmapView
# 新增：因果分析、采购智能辅助、主动换料
from ..views.causal_analysis_views import order_causal_analysis, causal_root_chain_analysis, causal_batch_analysis
from ..views.procurement_intelligence_views import (
    procurement_chase_alerts, procurement_obsolescence_warning, procurement_timeline,
    procurement_intelligent_recommendations, procurement_risk_dashboard, procurement_one_click_purchase
)
# 新增：RL训练管理、KPI基线对比
from ..views.core_kpi_views import RLTrainingAPI, KPIBaselineComparisonAPI
# 新增：蒙特卡洛概率仿真
from ..what_if_scenarios import monte_carlo_api_view as monte_carlo_simulation_view

router = DefaultRouter()
router.register(r'materials', MaterialViewSet)
router.register(r'suppliers', SupplierViewSet)
router.register(r'customers', CustomerViewSet)
router.register(r'orders', OrderViewSet)
router.register(r'inventory', InventoryViewSet)
router.register(r'boms', BillOfMaterialsViewSet)
router.register(r'commitments', SupplierCommitmentViewSet)
router.register(r'plan-results', MaterialPlanResultViewSet)
router.register(r'allocations', OrderAllocationViewSet)
router.register(r'purchase-orders', PurchaseOrderViewSet)
router.register(r'capacity', CapacityViewSet)
router.register(r'workcenters', WorkCenterViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('auth/login/', login_view, name='login'),
    path('auth/logout/', logout_view, name='logout'),
    path('auth/user/', user_info_view, name='user_info'),
    path('auth/refresh/', refresh_token_view, name='refresh_token'),
    path('dashboard/stats/', DashboardStatsView.as_view(), name='dashboard_stats'),
    path('analytics/supplier-performance/', SupplierPerformanceView.as_view(), name='supplier_performance'),
    path('analytics/inventory-alerts/', InventoryAlertView.as_view(), name='inventory_alerts'),
    path('analytics/order-analytics/', OrderAnalyticsView.as_view(), name='order_analytics'),
    path('analytics/priority/', OrderPriorityView.as_view(), name='order_priority'),
    path('analytics/auto-priority/', OrderPriorityView.as_view(), name='auto_priority_adjust'),
    path('analytics/delivery-change-alerts/', DeliveryChangeAlertsView.as_view(), name='delivery_change_alerts'),
    path('analytics/delivery-risk/', OrderDeliveryRiskView.as_view(), name='delivery_risk'),
    path('optimization/strategy/', OptimizationStrategyView.as_view(), name='optimization_strategy'),
    path('system/health/', SystemHealthView.as_view(), name='system_health'),
    path('system/config/', SystemConfigView.as_view(), name='system_config'),
    path('validation/', ValidationView.as_view(), name='validation'),
    path('import_data/', ImportDataView.as_view(), name='api_import_data'),
    path('import_data/refresh/', RefreshCSVListView.as_view(), name='api_refresh_csv_list'),
    path('import_data/history/', ImportHistoryListView.as_view(), name='import_history_list'),
    path('import_data/recognize/', FieldRecognitionView.as_view(), name='field_recognition'),
    path('import_data/preview/', SmartImportPreviewView.as_view(), name='smart_import_preview'),
    path('screen/data/', ScreenDataView.as_view(), name='screen_data'),
    path('screen/heatmap/', MaterialWarehouseHeatmapView.as_view(), name='material_warehouse_heatmap'),
    path('screen/capacity-heatmap/', CapacityUtilizationHeatmapView.as_view(), name='capacity_utilization_heatmap'),
    path('root_cause_analysis/', RootCauseAnalysisView.as_view(), name='root_cause_analysis'),
    path('plan_logs/', PlanLogListView.as_view(), name='plan_logs'),
    path('audit/', AuditLogListView.as_view(), name='audit_log_list'),
    path('ai/forecast-to-orders/', ai_views.forecast_to_orders, name='forecast-to-orders'),
    path('ai/auto-retrain/', AutoRetrainView.as_view(), name='auto_retrain'),
    path('planning/computation-mode/', ComputationModeView.as_view(), name='computation_mode'),
    path('scheduling/gantt/', GanttChartView.as_view(), name='gantt_chart'),
    path('simulation/history/', SimulationHistoryView.as_view(), name='simulation_history'),

    # ========== AI预测与智能分析模块 (REST API) ==========
    path('ai/demand-forecast/', ai_views.ai_demand_forecast, name='ai_demand_forecast'),
    path('ai/anomaly-detection/', ai_views.ai_anomaly_detection, name='ai_anomaly_detection'),
    path('ai/comprehensive-analysis/', ai_views.ai_comprehensive_analysis, name='ai_comprehensive_analysis'),
    path('ai/whatif-simulation/', ai_views.whatif_simulation, name='whatif_simulation'),
    # RL强化学习智能体
    path('ai/rl/recommendation/', ai_views.rl_recommendation, name='rl_recommendation'),
    path('ai/rl/train/', ai_views.rl_train, name='rl_train'),
    # NSGA-II多目标优化
    path('ai/multi-objective-optimize/', ai_views.multi_objective_optimize, name='multi_objective_optimize'),

    # ========== 高级排程引擎API ==========
    path('scheduling/gantt-data/', scheduling_views.api_gantt_data, name='api_gantt_data'),
    path('scheduling/generate/', scheduling_views.api_generate_schedule, name='api_generate_schedule'),
    path('scheduling/whatif/', scheduling_views.api_whatif_schedule, name='api_whatif_schedule'),
    path('scheduling/optimize/', scheduling_views.api_optimize_schedule, name='api_optimize_schedule'),
    path('scheduling/backfill/', scheduling_views.api_backfill_material_plan, name='api_backfill_material_plan'),

    # ========== 订单因果分析与决策追溯 (P5) ==========
    path('analysis/causal/', order_causal_analysis, name='order_causal_analysis'),
    path('analysis/causal/root-chain/', causal_root_chain_analysis, name='causal_root_chain'),
    path('analysis/causal/batch/', causal_batch_analysis, name='causal_batch'),

    # ========== 蒙特卡洛概率仿真 ==========
    path('simulation/monte-carlo/', monte_carlo_simulation_view, name='monte_carlo_simulation'),

    # ========== 采购智能辅助 (P6) ==========
    path('procurement/chase-alerts/', procurement_chase_alerts, name='procurement_chase_alerts'),
    path('procurement/obsolescence/', procurement_obsolescence_warning, name='procurement_obsolescence'),
    path('procurement/timeline/', procurement_timeline, name='procurement_timeline'),
    path('procurement/intelligent-recommendations/', procurement_intelligent_recommendations, name='procurement_intelligent_recommendations'),
    path('procurement/risk-dashboard/', procurement_risk_dashboard, name='procurement_risk_dashboard'),
    path('procurement/one-click-purchase/', procurement_one_click_purchase, name='procurement_one_click_purchase'),

    # ========== Hold审计日志与BOM变更历史 ==========
    path('hold-audit-logs/', HoldAuditLogListView.as_view(), name='hold_audit_log_list'),
    path('bom-change-history/', BOMChangeHistoryListView.as_view(), name='bom_change_history_list'),

    # ========== RL训练管理 (新增) ==========
    path('rl/train/', RLTrainingAPI.as_view({'post': 'train'}), name='rl_train'),
    path('rl/status/', RLTrainingAPI.as_view({'get': 'status'}), name='rl_status'),
    path('rl/recommend/', RLTrainingAPI.as_view({'post': 'recommend'}), name='rl_recommend_api'),
    path('rl/sensitivity/', RLTrainingAPI.as_view({'get': 'sensitivity'}), name='rl_sensitivity_api'),
    path('rl/export-model/', RLTrainingAPI.as_view({'post': 'export_model'}), name='rl_export_model_api'),

    # ========== KPI基线对比 (新增) ==========
    path('kpi/baseline-comparison/', KPIBaselineComparisonAPI.as_view({'get': 'baseline_comparison'}), name='kpi_baseline_comparison_api'),
]