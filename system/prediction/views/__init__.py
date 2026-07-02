# 视图模块初始化文件
from .dashboard_views import dashboard, dashboard_data
from .scheduling_views import scheduling, export_schedule
from .utils_views import view_logs
from .auth_views import user_login, user_logout
from .import_views import ImportDataView, BatchImportAllView, RefreshCSVListView, ImportHistoryListView

import_data = ImportDataView.as_view()
batch_import_all = BatchImportAllView.as_view()
refresh_csv_list = RefreshCSVListView.as_view()
import_history = ImportHistoryListView.as_view()
from .material_plan_views import (
    material_planning_dashboard,
    run_material_planning,
    material_planning_result,
    order_list,
    order_detail,
    inventory_list,
    bom_list,
    shortage_report,
    export_planning_result,
    planning_logs
)
from .master_data_views import (
    material_list,
    material_add,
    material_edit,
    material_delete,
    supplier_list,
    supplier_add,
    supplier_edit,
    supplier_delete,
    customer_list,
    customer_add,
    customer_edit,
    customer_delete,
    bom_add,
    bom_edit,
    bom_delete
)
from .supply_chain_views import (
    inventory_add,
    inventory_edit,
    inventory_delete,
    purchase_order_list,
    purchase_order_add,
    purchase_order_edit,
    purchase_order_delete,
    sales_order_list,
    sales_order_add,
    sales_order_edit,
    sales_order_delete,
    commitment_list
)
from .production_plan_views import (
    capacity_list,
    capacity_add,
    capacity_edit,
    capacity_delete
)
from .backup_views import (
    backup_list,
    create_backup,
    restore_backup,
    delete_backup
)
from .notification_views import (
    notification_list,
    notification_read,
    notification_read_all,
    notification_count,
    notification_check_alerts
)
from .help_views import (
    help_center,
    help_search
)

__all__ = [
    'dashboard',
    'dashboard_data',
    'scheduling',
    'export_schedule',
    'view_logs',
    'user_login',
    'user_logout',
    'import_data',
    'batch_import_all',
    'refresh_csv_list',
    'material_planning_dashboard',
    'run_material_planning',
    'material_planning_result',
    'order_list',
    'order_detail',
    'inventory_list',
    'bom_list',
    'shortage_report',
    'export_planning_result',
    'planning_logs',
    'material_list',
    'material_add',
    'material_edit',
    'material_delete',
    'supplier_list',
    'supplier_add',
    'supplier_edit',
    'supplier_delete',
    'customer_list',
    'customer_add',
    'customer_edit',
    'customer_delete',
    'bom_add',
    'bom_edit',
    'bom_delete',
    'inventory_add',
    'inventory_edit',
    'inventory_delete',
    'purchase_order_list',
    'purchase_order_add',
    'purchase_order_edit',
    'purchase_order_delete',
    'sales_order_list',
    'sales_order_add',
    'sales_order_edit',
    'sales_order_delete',
    'commitment_list',
    'capacity_list',
    'capacity_add',
    'capacity_edit',
    'capacity_delete',
    'backup_list',
    'create_backup',
    'restore_backup',
    'delete_backup',
    'notification_list',
    'notification_read',
    'notification_read_all',
    'notification_count',
    'notification_check_alerts',
    'help_center',
    'help_search',
]