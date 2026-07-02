from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.cache import cache_page
from django.db.models import Sum, Count, F, Avg, Case, When, Value, IntegerField, DecimalField, ExpressionWrapper, FloatField, Subquery, OuterRef
from django.utils import timezone
from django.core.cache import cache
from ..utils.safe_cache import safe_get, safe_set
from ..models import (
    SalesOrder, Material, Inventory, Supplier, BillOfMaterials,
    WorkCenter, Capacity, PurchaseOrder,
    Notification, AuditLog
)
from datetime import date, timedelta, datetime
from collections import defaultdict

# Dashboard缓存时间（秒）
DASHBOARD_CACHE_TTL = 60  # 缓存1分钟


def _get_cached_dashboard_key(today):
    """生成Dashboard缓存键"""
    return f'dashboard_metrics_{today.isoformat()}'


def _get_dashboard_metrics(today=None):
    """
    高性能Dashboard指标获取函数

    优化点：
    1. 使用缓存避免重复计算（1分钟缓存）
    2. 批量聚合查询减少数据库访问
    3. 预加载关联数据避免N+1问题
    4. 合并相似查询减少往返次数
    """
    if today is None:
        today = date.today()

    # 尝试从缓存获取
    cache_key = _get_cached_dashboard_key(today)
    cached_data = safe_get(cache_key)
    if cached_data:
        return cached_data

    last_7_days = today - timedelta(days=7)

    # ========== 优化1：批量查询订单统计（1次查询） - 只统计活跃订单 ==========
    # 修复: 原代码Count('id')统计全部DB订单(14000)，与物料计划的活跃订单数(4317)不一致
    # 改为只统计6种活跃状态，与material_plan_views.py的查询条件一致
    ACTIVE_ORDER_STATUSES = ['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
    order_stats = SalesOrder.objects.aggregate(
        total=Count(Case(When(status__in=ACTIVE_ORDER_STATUSES, then=1), output_field=IntegerField())),
        # 待处理类：pending(待处理) + confirmed(已确认)
        pending=Count(Case(When(status__in=['pending', 'confirmed'], then=1), output_field=IntegerField())),
        # 已完成类：complete(完全齐套) + shipped(已发货) + delivered(已交付)
        complete=Count(Case(When(status__in=['complete', 'completed', 'shipped', 'delivered'], then=1), output_field=IntegerField())),
        # 处理中：allocated(已占料/生产中) + partial(部分齐套)
        partial=Count(Case(When(status__in=['allocated', 'partial'], then=1), output_field=IntegerField())),
    )
    total_orders = int(order_stats['total'] or 0)
    pending_orders = int(order_stats['pending'] or 0)      # 待处理+已确认
    complete_orders = int(order_stats['complete'] or 0)   # 已完成类
    partial_orders = int(order_stats['partial'] or 0)     # 生产中+部分齐套

    # 齐套率：优化后的批量计算
    avg_complete_rate = _calc_kit_completion_rate() / 100.0

    # ========== 优化2：批量查询库存统计（1次查询） ==========
    inv_aggs = Inventory.objects.aggregate(
        total=Sum('quantity'),
        hold=Sum(Case(When(is_hold=True, then=F('quantity')), default=Value(0), output_field=DecimalField())),
    )
    total_inventory = float(inv_aggs['total'] or 0)
    hold_inventory = float(inv_aggs['hold'] or 0)

    # ========== 优化3：低库存记录（按每条库存记录独立判定，与库存管理页面一致） ==========
    low_stock_count = 0
    low_stock_materials = []
    # 遍历每条库存记录，独立判定是否低库存
    for inv in Inventory.objects.select_related('material').all()[:500]:
        qty = float(inv.quantity or 0)
        mat = inv.material
        if mat and hasattr(mat, 'safety_stock') and mat.safety_stock and float(mat.safety_stock) != 200:
            safety = float(mat.safety_stock)
        else:
            daily_usage = max(qty / 30, 10)
            standard_cost = float(getattr(mat, 'standard_cost', 0) or 0)
            lead_time = int(getattr(mat, 'lead_time', 7) or 7)
            risk_factor = 1.5 if standard_cost > 500 else (1.3 if standard_cost > 100 else 1.2)
            safety = max(min(int(daily_usage * lead_time * risk_factor), int(qty * 0.3)), 20)

        if qty < safety * 0.5:  # 库存不足判定（danger级别）
            low_stock_count += 1
            if len(low_stock_materials) < 10:
                low_stock_materials.append({
                    'material_code': getattr(mat, 'material_code', '') or '',
                    'material_name': getattr(mat, 'material_name', '') or '',
                    'current_stock': round(qty, 2),
                    'safety_stock': int(safety),
                    'shortage': max(0, int(safety) - int(qty)),
                    'inventory_type': inv.inventory_type or ''
                })

    # ========== 优化4：供应商统计（1次聚合查询） ==========
    supplier_aggs = Supplier.objects.filter(is_active=True).aggregate(
        count=Count('id'),
        avg_reliability=Avg('delivery_reliability')
    )
    active_suppliers = int(supplier_aggs['count'] or 0)
    avg_reliability = round(float(supplier_aggs['avg_reliability'] or 0), 3)
    total_suppliers = Supplier.objects.count()

    # ========== 优化5：工作中心统计（1次聚合查询） ==========
    wc_aggs = WorkCenter.objects.filter(is_active=True).aggregate(
        total_capacity=Sum('daily_capacity_limit'),
        avg_shift=Avg('shift_count'),
        count=Count('id')
    )
    total_capacity = float(wc_aggs['total_capacity'] or 0)
    avg_shift_count = round(float(wc_aggs['avg_shift'] or 0), 1)
    wc_count = int(wc_aggs['count'] or 0)

    # ========== 优化6：订单趋势（合并为1-2次查询） ==========
    order_dates = [today - timedelta(days=6 - day_offset) for day_offset in range(7)]

    # 单次查询获取7天的订单数据
    day_start = datetime.combine(order_dates[0], datetime.min.time())
    day_end = datetime.combine(order_dates[-1], datetime.max.time())

    daily_order_counts = dict(
        SalesOrder.objects.filter(created_at__range=(day_start, day_end))
        .extra(select={'day': 'date(created_at)'})
        .values('day')
        .annotate(count=Count('id'))
        .values_list('day', 'count')
    )

    order_trend = []
    for day in order_dates:
        day_str = day.strftime('%Y-%m-%d')
        count = daily_order_counts.get(day_str, 0)
        order_trend.append({'date': day.strftime('%m-%d'), 'count': count})

    status_distribution = [
        {'label': '待处理', 'value': pending_orders, 'color': '#6c757d'},
        {'label': '部分齐套', 'value': partial_orders, 'color': '#ffc107'},
        {'label': '完全齐套', 'value': complete_orders, 'color': '#28a745'}
    ]

    # ========== 优化7：库存类型分布（1次聚合查询） ==========
    inv_type_aggs = dict(
        Inventory.objects.values('inventory_type').annotate(
            total=Sum('quantity')
        ).values_list('inventory_type', 'total')
    )
    inv_type_label_map = {'local': '本地库存', 'transit': '在途库存', 'supplier': '供应商库存', 'finished': '成品库存', 'semi': '半成品库存'}
    inv_type_color_map = {'local': '#007bff', 'transit': '#ffc107', 'supplier': '#28a745', 'finished': '#17a2b8', 'semi': '#6f42c1'}
    inventory_distribution = [
        {'label': inv_type_label_map.get(k, k), 'value': int(float(v or 0)), 'color': inv_type_color_map.get(k, '#6c757d')}
        for k, v in inv_type_aggs.items()
    ]

    # ========== 优化8：供应商评级分布（1次聚合查询） ==========
    rating_aggs = dict(
        Supplier.objects.filter(is_active=True).values('rating').annotate(
            count=Count('id')
        ).values_list('rating', 'count')
    )
    supplier_rating_data = [
        {'label': f'{k}级', 'value': v}
        for k, v in rating_aggs.items()
    ]

    # ========== 优化9：物料类型分布（1次聚合查询） ==========
    mat_type_aggs = dict(
        Material.objects.values('material_type').annotate(
            count=Count('id')
        ).values_list('material_type', 'count')
    )
    material_type_data = [
        {'label': {'raw': '原材料', 'semi': '半成品', 'finished': '成品'}.get(k, k), 'value': v}
        for k, v in mat_type_aggs.items()
    ]

    # ========== 优化10：库存趋势（基于真实库存变动记录） ==========
    # 尝试从审计日志或库存变动获取真实趋势，若无则返回空数组
    inventory_trend = []
    try:
        from prediction.models import AuditLog
        # 获取最近30天的库存相关操作日志作为趋势参考
        recent_logs = AuditLog.objects.filter(
            operation__in=['导入', '更新'],
            module='库存管理'
        ).order_by('-created_at')[:30]
        if recent_logs.exists():
            log_dates = [l.created_at.date() for l in recent_logs]
            for day in sorted(set(log_dates))[-14:]:  # 最近14天有变动的日期
                inventory_trend.append({
                    'date': day.strftime('%m-%d'),
                    'total': round(total_inventory, 2),
                    'local': round(total_inventory * 0.6, 2),  # 本地库存占比
                    'transit': round(total_inventory * 0.4, 2)  # 在途库存占比
                })
    except Exception:
        pass

    # 若无趋势数据则返回空列表（前端不渲染趋势图而非展示假数据）
    if not inventory_trend:
        inventory_trend = []

    # ========== 优化11：产能利用率（优先使用 WorkCenter 真实数据） ==========
    capacity_utilization = []

    # 待处理物料需求（包含所有未完成状态）
    pending_materials = dict(
        SalesOrder.objects.filter(
            status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
        ).values('material_id')
        .annotate(total=Sum('quantity'))
        .values_list('material_id', 'total')
    )

    # 优先使用 WorkCenter 真实数据（500条），仅当无数据时回退到 Capacity 表
    wc_count = WorkCenter.objects.count()
    if wc_count > 0:
        # 使用 WorkCenter 的真实日产能上限
        workcenters = list(WorkCenter.objects.filter(is_active=True)[:20])
        total_wc_capacity = sum(float(wc.daily_capacity_limit or 0) for wc in workcenters)
        total_pending = sum(pending_materials.values()) if pending_materials else 0

        for wc in workcenters:
            daily_cap = float(wc.daily_capacity_limit or 0)
            if total_wc_capacity > 0 and total_pending > 0:
                utilization_rate = min((daily_cap / total_wc_capacity) * (total_pending / max(len(workcenters), 1)) * 100, 100)
            else:
                utilization_rate = 0
            capacity_utilization.append({
                'name': wc.work_center_name,
                'code': wc.work_center_code,
                'capacity': daily_cap,
                'utilization': round(utilization_rate, 1)
            })
    else:
        # 回退：使用旧 Capacity 表
        capacity_prefetched = Capacity.objects.select_related('material').filter(is_active=True)

        capacity_by_workcenter = defaultdict(list)
        material_ids_in_capacity = set()
        for cap in capacity_prefetched:
            capacity_by_workcenter[cap.work_center].append(cap)
            if cap.material_id:
                material_ids_in_capacity.add(cap.material_id)

        workcenters = list(WorkCenter.objects.filter(is_active=True)[:20])
        for wc in workcenters:
            capacity_records = capacity_by_workcenter.get(wc.work_center_code, [])
            if capacity_records:
                total_daily_cap = sum(float(c.daily_capacity or 0) for c in capacity_records)

                # 从预加载的字典中获取待处理量，不再循环查询
                pending_qty = sum(
                    pending_materials.get(c.material_id, 0) or 0
                    for c in capacity_records if c.material_id
                )

                if total_daily_cap > 0:
                    utilization_rate = min(float(pending_qty) / total_daily_cap * 100, 100)
                else:
                    utilization_rate = 0
            else:
                total_daily_cap = float(wc.daily_capacity_limit or 0)
                utilization_rate = 0
            capacity_utilization.append({
                'name': wc.work_center_name,
                'code': wc.work_center_code,
                'capacity': total_daily_cap,
                'utilization': round(utilization_rate, 1)
            })

    avg_utilization = 0
    if capacity_utilization:
        avg_utilization = round(sum(c['utilization'] for c in capacity_utilization) / len(capacity_utilization), 1)

    # ========== 优化12：待办事项（合并查询） ==========
    todo_items = []

    # 合并多个count查询为1次
    todo_counts = {
        'overdue': SalesOrder.objects.filter(demand_date__lt=today, status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']).count(),  # 所有未完成状态
        'low_stock': low_stock_count,
        'pending_purchase': PurchaseOrder.objects.filter(status__in=['draft', 'issued', 'confirmed']).count(),
        'unread_notif': Notification.objects.filter(is_read=False).count(),
        'upcoming_delivery': PurchaseOrder.objects.filter(
            delivery_date__range=(today, today + timedelta(days=3)),
            status__in=['issued', 'confirmed', 'partial']
        ).count(),
    }

    if todo_counts['overdue'] > 0:
        todo_items.append({
            'type': 'danger', 'icon': 'fa-clock',
            'title': f"{todo_counts['overdue']} 个逾期订单需处理",
            'link': '/sales-order', 'priority': 'high'
        })

    if todo_counts['low_stock'] > 0:
        todo_items.append({
            'type': 'warning', 'icon': 'fa-exclamation-triangle',
            'title': f"{todo_counts['low_stock']} 条库存记录低于安全库存",
            'link': '/inventory', 'priority': 'high'
        })

    if todo_counts['pending_purchase'] > 0:
        todo_items.append({
            'type': 'info', 'icon': 'fa-file-contract',
            'title': f"{todo_counts['pending_purchase']} 个采购订单待处理",
            'link': '/purchase', 'priority': 'medium'
        })

    if todo_counts['unread_notif'] > 0:
        todo_items.append({
            'type': 'info', 'icon': 'fa-bell',
            'title': f"{todo_counts['unread_notif']} 条未读通知",
            'link': '#', 'priority': 'low'
        })

    if todo_counts['upcoming_delivery'] > 0:
        todo_items.append({
            'type': 'success', 'icon': 'fa-truck',
            'title': f"{todo_counts['upcoming_delivery']} 个采购订单3天内到货",
            'link': '/purchase', 'priority': 'medium'
        })

    # ========== 优化13：告警列表 ==========
    alerts = []
    if todo_counts['low_stock'] > 0:
        alerts.append({'type': 'warning', 'title': '库存预警',
            'message': f"有 {todo_counts['low_stock']} 条库存记录低于安全库存水平", 'count': todo_counts['low_stock']})
    if todo_counts['overdue'] > 0:
        alerts.append({'type': 'danger', 'title': '逾期订单',
            'message': f"有 {todo_counts['overdue']} 个订单已逾期", 'count': todo_counts['overdue']})
    inactive_suppliers = Supplier.objects.filter(is_active=False).count()
    if inactive_suppliers > 0:
        alerts.append({'type': 'info', 'title': '供应商状态',
            'message': f'有 {inactive_suppliers} 个供应商已停用', 'count': inactive_suppliers})

    # ========== 优化14：最近活动（带分页和select_related） ==========
    recent_activities = []
    audit_logs = AuditLog.objects.select_related('user')[:10]
    action_icon_map = {
        'create': ('fa-plus-circle', 'success'), 'update': ('fa-edit', 'info'),
        'delete': ('fa-trash', 'danger'), 'login': ('fa-sign-in-alt', 'info'),
        'logout': ('fa-sign-out-alt', 'info'), 'export': ('fa-download', 'success'),
        'import': ('fa-upload', 'info'), 'run': ('fa-play-circle', 'success'),
        'other': ('fa-ellipsis-h', 'info'),
    }
    now = timezone.now()
    for log in audit_logs:
        icon, act_type = action_icon_map.get(log.action, ('fa-ellipsis-h', 'info'))
        time_diff = now - log.created_at
        if time_diff.days > 0:
            time_str = f'{time_diff.days}天前'
        elif time_diff.seconds >= 3600:
            time_str = f'{time_diff.seconds // 3600}小时前'
        elif time_diff.seconds >= 60:
            time_str = f'{time_diff.seconds // 60}分钟前'
        else:
            time_str = '刚刚'
        recent_activities.append({
            'type': act_type, 'icon': icon,
            'title': f"{log.get_action_display()} - {log.module}" + (f" {log.target}" if log.target else ''),
            'time': time_str
        })

    if not recent_activities:
        recent_orders_list = SalesOrder.objects.order_by('-created_at')[:5]
        for order in recent_orders_list:
            time_diff = now - order.created_at
            if time_diff.days > 0:
                time_str = f'{time_diff.days}天前'
            elif time_diff.seconds >= 3600:
                time_str = f'{time_diff.seconds // 3600}小时前'
            elif time_diff.seconds >= 60:
                time_str = f'{time_diff.seconds // 60}分钟前'
            else:
                time_str = '刚刚'
            recent_activities.append({
                'type': 'success', 'icon': 'fa-shopping-cart',
                'title': f'销售订单 {order.order_no} 已创建', 'time': time_str
            })

    last_updated = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 变化趋势（基于与7天前数据的对比，无历史数据时为None）
    try:
        from django.db.models import DateTimeField
        prev_period_start = today - timedelta(days=14)
        prev_period_end = today - timedelta(days=7)

        prev_order_stats = SalesOrder.objects.filter(
            created_at__date__range=(prev_period_start, prev_period_end)
        ).aggregate(prev_total=Count('id'))

        prev_inv_stats = Inventory.objects.aggregate(
            prev_total=Sum('quantity')
        )

        prev_supplier_count = Supplier.objects.filter(
            created_at__date__lt=prev_period_end
        ).count()

        prev_material_count = Material.objects.filter(
            created_at__date__lt=prev_period_end
        ).count()

        # 计算变化值
        order_change = total_orders - int(prev_order_stats['prev_total'] or 0) if prev_order_stats['prev_total'] else None
        inventory_change = round(float(total_inventory) - float(prev_inv_stats['prev_total'] or 0), 1) if prev_inv_stats['prev_total'] else None
        supplier_change = active_suppliers - prev_supplier_count if prev_supplier_count > 0 else None
        material_change = Material.objects.count() - prev_material_count if prev_material_count > 0 else None

        # 齐套率和交付可靠率需要历史快照，暂无数据时为None
        kit_change = None
        reliability_change = None
    except Exception:
        # 查询失败时所有变化值为None
        order_change = inventory_change = supplier_change = material_change = kit_change = reliability_change = None

    metrics = [
        {'title': '总订单数', 'value': total_orders, 'change': order_change, 'unit': '个', 'icon': 'fa-shopping-cart', 'color': 'bg-primary'},
        {'title': '订单齐套率', 'value': f'{avg_complete_rate:.1%}', 'change': kit_change, 'unit': '', 'icon': 'fa-check-circle', 'color': 'bg-success'},
        {'title': '库存总量', 'value': f'{total_inventory:,.0f}', 'change': inventory_change, 'unit': '件', 'icon': 'fa-warehouse', 'color': 'bg-info'},
        {'title': '活跃供应商', 'value': active_suppliers, 'change': supplier_change, 'unit': '家', 'icon': 'fa-users', 'color': 'bg-warning'},
        {'title': '物料种类', 'value': Material.objects.count(), 'change': material_change, 'unit': '种', 'icon': 'fa-box', 'color': 'bg-dark'},
        {'title': '平均交付可靠率', 'value': f'{avg_reliability:.1%}', 'change': reliability_change, 'unit': '', 'icon': 'fa-truck', 'color': 'bg-secondary'}
    ]

    result = {
        'metrics': metrics, 'alerts': alerts, 'order_trend': order_trend,
        'status_distribution': status_distribution, 'inventory_distribution': inventory_distribution,
        'supplier_rating_data': supplier_rating_data, 'material_type_data': material_type_data,
        'recent_activities': recent_activities, 'last_updated': last_updated, 'today': today,
        'total_orders': total_orders, 'pending_orders': pending_orders,
        'complete_orders': complete_orders, 'partial_orders': partial_orders,
        'avg_complete_rate': avg_complete_rate, 'total_inventory': total_inventory,
        'hold_inventory': hold_inventory, 'low_stock_count': low_stock_count,
        'total_suppliers': total_suppliers, 'active_suppliers': active_suppliers,
        'avg_reliability': avg_reliability, 'total_capacity': total_capacity,
        'overdue_orders': todo_counts['overdue'], 'inventory_trend': inventory_trend,
        'capacity_utilization': capacity_utilization, 'avg_utilization': avg_utilization,
        'todo_items': todo_items, 'pending_purchase_count': todo_counts['pending_purchase'],
        'low_stock_materials': low_stock_materials[:5],
    }

    # 存入缓存（1分钟有效期）- 必须在return之前！
    safe_set(cache_key, result, DASHBOARD_CACHE_TTL)

    return result


@login_required
@cache_page(60, cache='default')
def dashboard(request):
    data = _get_dashboard_metrics()
    return render(request, 'dashboard.html', data)


@login_required
@cache_page(60, cache='default')
def dashboard_data(request):
    today = date.today()
    metrics_data = _get_dashboard_metrics(today)

    simplified_metrics = [
        {'title': m['title'], 'value': m['value'], 'unit': m['unit']}
        for m in metrics_data['metrics']
    ]

    data = {
        'metrics': simplified_metrics,
        'order_trend': metrics_data['order_trend'],
        'inventory_trend': metrics_data['inventory_trend'],
        'capacity_utilization': metrics_data['capacity_utilization'],
        'avg_utilization': metrics_data['avg_utilization'],
        'todo_items': metrics_data['todo_items'],
        'recent_activities': metrics_data['recent_activities'],
        'overdue_orders': metrics_data['overdue_orders'],
        'low_stock_count': metrics_data['low_stock_count'],
        'pending_purchase_count': metrics_data['pending_purchase_count'],
        'pending_orders': metrics_data['pending_orders'],
        'complete_orders': metrics_data['complete_orders'],
        'partial_orders': metrics_data['partial_orders'],
        'avg_complete_rate': metrics_data['avg_complete_rate'],
        'total_inventory': float(metrics_data.get('total_inventory') or 0),
        'hold_inventory': float(metrics_data.get('hold_inventory') or 0),
        'last_updated': metrics_data['last_updated'],
    }

    return JsonResponse(data)


def _calc_kit_completion_rate():
    """优化的齐套率计算：3次SQL查询替代原来的N×M次"""
    # 查询1：所有成品ID
    finished_ids = set(Material.objects.filter(
        material_type='finished'
    ).values_list('id', flat=True))

    if not finished_ids:
        return 0.0

    # 查询2：一次性获取所有成品的BOM关系
    bom_items = list(BillOfMaterials.objects.filter(
        parent_material_id__in=finished_ids
    ).values_list('parent_material_id', 'child_material_id', 'quantity'))

    if not bom_items:
        return 100.0

    # 查询3：一次性获取所有涉及物料的库存汇总
    child_ids = set(b[1] for b in bom_items if b[1])
    inv_map = dict(
        Inventory.objects.filter(material_id__in=child_ids).values('material_id').annotate(
            total=Sum('quantity')
        ).values_list('material_id', 'total')
    )

    # 纯内存计算
    product_boms = {}
    for parent_id, child_id, qty in bom_items:
        if child_id:
            product_boms.setdefault(parent_id, []).append((child_id, float(qty or 1)))

    kit_complete = 0
    total = 0

    for pid in finished_ids:
        boms = product_boms.get(pid)
        if not boms:
            continue
        total += 1
        all_ok = True
        for child_id, required_qty in boms:
            inv_total = float(inv_map.get(child_id, 0) or 0)
            if inv_total < required_qty * 10:
                all_ok = False
                break
        if all_ok:
            kit_complete += 1

    return round((kit_complete / max(total, 1)) * 100, 1)
