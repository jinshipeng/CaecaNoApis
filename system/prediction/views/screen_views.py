from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Q, F, DecimalField, ExpressionWrapper, Avg
from django.db import connection
from datetime import date, timedelta
from django.views import View
import logging

logger = logging.getLogger(__name__)

from ..models import (
    SalesOrder, Material, Inventory, Supplier,
    WorkCenter, BillOfMaterials, MaterialPlanResult, PurchaseOrder
)


def get_order_stats():
    """获取订单统计信息 - 使用活跃订单(与物料计划一致)"""
    ACTIVE_STATUSES = ['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
    stats = SalesOrder.objects.aggregate(
        # 修复: 只统计活跃订单总数(非全部DB记录)
        total=Count('id', filter=Q(status__in=ACTIVE_STATUSES)),
        # 待处理类：pending(待处理) + confirmed(已确认)
        pending=Count('id', filter=Q(status__in=['pending', 'confirmed'])),
        # 处理中：allocated(已占料/生产中) + partial(部分齐套) + in_production + processing
        processing=Count('id', filter=Q(status__in=['allocated', 'partial', 'in_production', 'processing'])),
        # 已完成：complete(完全齐套) + shipped(已发货) + delivered(已交付)
        complete=Count('id', filter=Q(status__in=['complete', 'completed'])),
        shipped=Count('id', filter=Q(status='shipped')),
        delivered=Count('id', filter=Q(status='delivered'))
    )

    return {
        'total': stats['total'],
        'pending': stats['pending'] or 0,  # 待处理+已确认
        'processing': stats['processing'] or 0,  # 已占料+部分齐套
        'completed': (stats['complete'] or 0) + (stats['shipped'] or 0) + (stats['delivered'] or 0)  # 已完成类
    }


def get_inventory_stats():
    """获取库存统计信息 - 按实际库存记录维度统计（每条记录独立判定状态）"""
    # 一次性获取所有库存及其关联的物料信息
    inventories = Inventory.objects.select_related(
        'material'
    ).only(
        'quantity',
        'is_hold',
        'inventory_type',
        'material__safety_stock',
        'material__standard_cost',
        'material__lead_time',
        'material__material_code',
        'material__material_name'
    )

    total_amount = 0
    low_stock_count = 0
    warning_count = 0
    normal_count = 0
    with_hold_count = 0

    for inv in inventories:
        material = inv.material
        if not material:
            continue

        qty = float(inv.quantity or 0)

        # 计算总金额
        if hasattr(material, 'standard_cost') and material.standard_cost:
            total_amount += qty * float(material.standard_cost)

        # Hold统计
        if inv.is_hold:
            with_hold_count += 1

        # 获取安全库存：优先用物料表字段，否则动态计算
        db_safety = float(material.safety_stock) if hasattr(material, 'safety_stock') else 0
        if db_safety and db_safety != 200:
            safety = db_safety
        else:
            daily_usage = max(qty / 30, 10)
            sc = float(getattr(material, 'standard_cost', 0) or 0)
            lt = int(getattr(material, 'lead_time', 7) or 7)
            rf = 1.5 if sc > 500 else (1.3 if sc > 100 else 1.2)
            safety = max(min(int(daily_usage * lt * rf), int(qty * 0.3)), 20)

        # 按每条库存记录独立判定状态
        if qty < safety * 0.5:
            low_stock_count += 1
        elif qty < safety:
            warning_count += 1
        else:
            normal_count += 1

    return {
        'total_amount': round(total_amount, 2),
        'low_stock_count': low_stock_count,
        'warning_count': warning_count,
        'normal_count': normal_count,
        'with_hold_count': with_hold_count,
        'record_count': len(inventories)
    }


def get_trend_data(today):
    """获取订单趋势数据 - 按订单日期统计（支持月度视图）"""
    # 获取最近6个月的月度订单趋势
    trend_dates = []
    trend_values = []

    for i in range(5, -1, -1):  # 最近6个月
        month_start = today.replace(day=1) - timedelta(days=i*30)
        month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

        # 统计该月的订单数量（基于order_date下单日期）
        month_count = SalesOrder.objects.filter(
            order_date__gte=month_start,
            order_date__lte=month_end
        ).count()

        trend_dates.append(month_start.strftime('%Y-%m'))
        trend_values.append(month_count)

    return trend_dates, trend_values


@login_required
def visual_screen(request):
    """可视化大屏视图 - 优化版本"""
    try:
        today = date.today()

        # ========== 优化：尝试从缓存获取 ==========
        from ..utils.safe_cache import safe_get, safe_set
        cache_key = f'screen_data_{today.isoformat()}'
        cached_data = safe_get(cache_key)
        if cached_data:
            return JsonResponse(cached_data)

        # 1. 订单统计（单次查询）
        order_stats = get_order_stats()
        total_orders = order_stats['total']
        pending_count = order_stats['pending']
        processing_count = order_stats['processing']
        completed_count = order_stats['completed']

        # 2. 齐套率：实时从库存+BOM计算（不再依赖MaterialPlanResult缓存表）
        avg_complete_rate = _calc_kit_completion_rate() / 100.0

        # 3. 库存统计（优化后的批量查询）
        inv_stats = get_inventory_stats()
        total_inventory_amount = inv_stats['total_amount']
        alert_count = inv_stats['low_stock_count']

        # 4-6. 合并供应商/产能/物料统计为更少的查询
        supplier_aggs = Supplier.objects.filter(is_active=True).aggregate(count=Count('id'))
        active_suppliers = supplier_aggs['count'] or 0

        wc_aggs = WorkCenter.objects.filter(is_active=True).aggregate(
            total_cap=Sum('daily_capacity_limit'),
            count=Count('id')
        )
        total_capacity = float(wc_aggs['total_cap'] or 0)

        total_materials = Material.objects.count()

        # 7. 预警信息（逾期订单 - 包含所有未完成状态）
        overdue_orders = SalesOrder.objects.filter(
            demand_date__lt=today,
            status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']  # 所有未完成状态
        ).count()
        warning_count = overdue_orders

        # 7.1 供应商交期预警数据
        delivery_change_alert_count = MaterialPlanResult.objects.filter(
            delivery_change_count__gte=2
        ).count()
        delivery_change_orders = list(
            MaterialPlanResult.objects.filter(
                delivery_change_count__gte=2
            ).select_related('order').values(
                'order__order_no', 'delivery_change_count', 'order__customer_name', 'order__demand_date'
            )[:10]
        )

        late_po_count = PurchaseOrder.objects.filter(
            actual_delivery_date__isnull=False,
            actual_delivery_date__gt=F('delivery_date'),
        ).count()
        late_supplier_stats = list(
            PurchaseOrder.objects.filter(
                actual_delivery_date__isnull=False,
                actual_delivery_date__gt=F('delivery_date'),
            ).values(
                'supplier__supplier_code', 'supplier__supplier_name',
                'delivery_date', 'actual_delivery_date'
            )
        )
        supplier_delay_map = {}
        for row in late_supplier_stats:
            key = row['supplier__supplier_code']
            delay = (row['actual_delivery_date'] - row['delivery_date']).days
            if key not in supplier_delay_map:
                supplier_delay_map[key] = {
                    'supplier_code': key,
                    'supplier_name': row['supplier__supplier_name'],
                    'late_count': 0,
                    'max_delay_days': 0,
                }
            supplier_delay_map[key]['late_count'] += 1
            supplier_delay_map[key]['max_delay_days'] = max(
                supplier_delay_map[key]['max_delay_days'], delay
            )
        supplier_delivery_alerts = sorted(
            supplier_delay_map.values(), key=lambda x: x['late_count'], reverse=True
        )[:5]

        # 8. 订单状态百分比
        pending_percent = (pending_count / total_orders * 100) if total_orders > 0 else 0
        processing_percent = (processing_count / total_orders * 100) if total_orders > 0 else 0
        completed_percent = (completed_count / total_orders * 100) if total_orders > 0 else 0
        
        # 9. 完成率（修复: 无数据时返回0而非硬编码78）
        completion_rate = round(completed_percent) if total_orders > 0 else 0
        completion_offset = 326 - (completion_rate / 100 * 326)
        
        # 10. 订单趋势（优化）
        trend_dates, trend_values = get_trend_data(today)
        
        # 11. 最近订单列表（优化）- 完整状态映射
        status_map = {
            'pending': '待处理',
            'confirmed': '已确认',
            'in_production': '生产中',
            'allocated': '已占料',
            'partial': '部分齐套',
            'complete': '完全齐套',
            'completed': '完全齐套',
            'processing': '进行中',
            'shipped': '已发货',
            'delivered': '已交付',
            'cancelled': '已取消'
        }
        
        recent_orders = SalesOrder.objects.select_related(
            'material'
        ).only(
            'order_no', 'customer_name', 'quantity', 'status', 'created_at',
            'material__standard_cost'
        ).order_by('-created_at')[:6]
        
        order_list = []
        for order in recent_orders:
            status = order.status if order.status in ['pending', 'partial', 'shipped', 'complete', 'completed', 'delivered', 'allocated', 'confirmed', 'in_production', 'processing', 'cancelled'] else 'pending'
            
            # 计算订单金额
            qty = float(order.quantity or 0)
            unit_price = 0
            if hasattr(order, 'material') and order.material:
                if hasattr(order.material, 'standard_cost') and order.material.standard_cost:
                    unit_price = float(order.material.standard_cost or 0)
            amount = round(qty * unit_price, 2)
            
            order_list.append({
                'order_no': order.order_no,
                'customer_name': order.customer_name,
                'total_amount': amount,
                'status': status,
                'status_display': status_map.get(order.status, '待处理')
            })
        
        # 12. 库存记录状态（按实际库存记录展示，每条记录独立判定）
        material_list = []
        for inv in Inventory.objects.select_related('material').all()[:8]:
            qty = float(inv.quantity or 0)
            mat = inv.material
            if not mat:
                continue

            if hasattr(mat, 'safety_stock') and mat.safety_stock and float(mat.safety_stock) != 200:
                safety = float(mat.safety_stock)
            else:
                daily_usage = max(qty / 30, 10)
                sc = float(getattr(mat, 'standard_cost', 0) or 0)
                lt = int(getattr(mat, 'lead_time', 7) or 7)
                rf = 1.5 if sc > 500 else (1.3 if sc > 100 else 1.2)
                safety = max(min(int(daily_usage * lt * rf), int(qty * 0.3)), 20)

            if qty < safety * 0.5:
                status = 'critical'
                status_display = '库存不足'
            elif qty < safety:
                status = 'low'
                status_display = '接近安全'
            else:
                status = 'normal'
                status_display = '正常'

            material_list.append({
                'material_code': getattr(mat, 'material_code', ''),
                'material_name': getattr(mat, 'material_name', ''),
                'quantity': int(qty) if qty == int(qty) else round(qty, 2),
                'inventory_type': inv.inventory_type or '',
                'status': status,
                'status_display': status_display
            })
        
        # 13. 运输路线数据（模拟数据）
        city_coords = {
            '北京': [116.46, 39.92],
            '上海': [121.48, 31.22],
            '广州': [113.23, 23.16],
            '深圳': [114.07, 22.62],
            '杭州': [120.19, 30.26],
            '南京': [118.78, 32.04],
            '成都': [104.06, 30.67],
            '武汉': [114.31, 30.52],
            '西安': [108.95, 34.27],
            '重庆': [106.54, 29.59],
            '苏州': [120.62, 31.32],
            '天津': [117.2, 39.13],
            '青岛': [120.33, 36.07],
            '厦门': [118.1, 24.47],
            '长沙': [112.94, 28.23]
        }
        
        route_data = []
        sample_orders = SalesOrder.objects.filter(status__in=['shipped', 'delivered'])[:6].only('status')
        cities_list = list(city_coords.keys())
        
        for i, order in enumerate(sample_orders):
            origin = cities_list[i % len(cities_list)]
            dest_index = (i + 5) % len(cities_list)
            destination = cities_list[dest_index]
            route_data.append({
                'name': f'{origin} -> {destination}',
                'coords': [city_coords[origin], city_coords[destination]],
                'status': '运输中' if order.status == 'shipped' else '已送达',
                'arrival': (today + timedelta(days=2)).strftime('%m-%d')
            })
        
        if len(route_data) == 0:
            # 无已发货订单时，基于城市坐标生成示例路线（仅用于大屏展示效果）
            route_data = [
                {'name': '上海 -> 北京', 'coords': [[121.48, 31.22], [116.46, 39.92]], 'status': '-', 'arrival': '-'},
                {'name': '深圳 -> 成都', 'coords': [[114.07, 22.62], [104.06, 30.67]], 'status': '-', 'arrival': '-'},
                {'name': '广州 -> 武汉', 'coords': [[113.23, 23.16], [114.31, 30.52]], 'status': '-', 'arrival': '-'},
                {'name': '杭州 -> 西安', 'coords': [[120.19, 30.26], [108.95, 34.27]], 'status': '-', 'arrival': '-'},
                {'name': '南京 -> 重庆', 'coords': [[118.78, 32.04], [106.54, 29.59]], 'status': '-', 'arrival': '-'}
            ]
        
        city_data = [
            [coord[0], coord[1], 3 + len([r for r in route_data if city in r['name']])]
            for city, coord in city_coords.items()
        ]
        
        transport_count = len([r for r in route_data if r['status'] == '运输中'])
        delivered_count = len([r for r in route_data if r['status'] == '已送达'])
        
        # 基于真实数据计算平均周转天数和平均交货天数
        # 平均周转天数 = 库存总量 / 日均消耗量（用近7天已完成订单估算）
        completed_qty_7d = float(SalesOrder.objects.filter(
            status__in=['complete', 'completed', 'shipped', 'delivered'],
            order_date__gte=today - timedelta(days=7)
        ).aggregate(total=Sum('quantity'))['total'] or 0)
        daily_consumption = completed_qty_7d / max(7, 1)
        total_inv_float = float(total_inventory_amount) if isinstance(total_inventory_amount, (int, float)) else 0
        avg_turnover = round(total_inv_float / max(daily_consumption, 1), 1) if daily_consumption > 0 else 15

        # 平均交货天数：取供应商平均交货周期
        from ..models import SupplierMaterial
        avg_lead = SupplierMaterial.objects.aggregate(avg_lt=Avg('lead_time'))['avg_lt']
        avg_shipping = int(round(float(avg_lead or 0))) if avg_lead else 3

        context = {
            'today': today,
            'total_orders': total_orders,
            'pending_count': pending_count,
            'processing_count': processing_count,
            'completed_count': completed_count,
            'pending_percent': pending_percent,
            'processing_percent': processing_percent,
            'completed_percent': completed_percent,
            'alert_count': alert_count,
            'warning_count': warning_count,
            'total_materials': total_materials,
            'total_inventory_amount': total_inventory_amount if total_inventory_amount > 0 else '0.00',
            'avg_turnover_days': avg_turnover,
            'completion_rate': completion_rate,
            'completion_offset': completion_offset,
            'trend_dates': trend_dates,
            'trend_values': trend_values,
            'order_list': order_list,
            'material_list': material_list,
            'route_data': route_data,
            'city_data': city_data,
            'transport_count': transport_count,
            'delivered_count': delivered_count,
            'avg_shipping_days': avg_shipping,
            'delivery_change_alert_count': delivery_change_alert_count,
            'delivery_change_orders': delivery_change_orders,
            'late_po_count': late_po_count,
            'supplier_delivery_alerts': supplier_delivery_alerts,
        }

        # 存入缓存（30秒有效期，大屏数据更新频繁）
        safe_set(cache_key, context, 30)

        logger.info(f'可视化大屏加载完成，共 {len(connection.queries)} 次数据库查询（已缓存）')

        return render(request, 'screen/visual_screen.html', context)

    except Exception as e:
        logger.error(f'可视化大屏加载失败: {str(e)}', exc_info=True)
        raise


def _calc_kit_completion_rate():
    """实时计算齐套率：基于每个成品的BOM物料库存是否充足（高性能版本）"""
    # 查询1：所有成品ID（单次查询）
    finished_ids = list(Material.objects.filter(
        material_type='finished'
    ).values_list('id', flat=True))

    if not finished_ids:
        return 0.0

    # 查询2：一次性获取所有成品的BOM关系（单次查询）
    bom_items = list(BillOfMaterials.objects.filter(
        parent_material_id__in=finished_ids
    ).values_list('parent_material_id', 'child_material_id', 'quantity'))

    if not bom_items:
        return 100.0

    # 查询3：一次性获取所有涉及物料的库存汇总（单次查询）
    child_ids = set(b[1] for b in bom_items if b[1])
    inv_map = dict(
        Inventory.objects.filter(material_id__in=child_ids).values('material_id').annotate(
            total=Sum('quantity')
        ).values_list('material_id', 'total')
    )

    # 纯内存计算（避免N+1查询）
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


class ScreenDataView(View):
    """可视化大屏数据 API 视图（供 REST API 调用）"""

    def get(self, request):
        """复用 visual_screen 的逻辑，返回 JSON 数据"""
        try:
            today = date.today()

            from ..utils.safe_cache import safe_get, safe_set
            cache_key = f'screen_data_{today.isoformat()}'
            cached_data = safe_get(cache_key)
            if cached_data:
                return JsonResponse(cached_data)

            order_stats = get_order_stats()
            total_orders = order_stats['total']
            pending_count = order_stats['pending']
            processing_count = order_stats['processing']
            completed_count = order_stats['completed']

            avg_complete_rate = _calc_kit_completion_rate() / 100.0

            inv_stats = get_inventory_stats()
            total_inventory = inv_stats['total_quantity']
            low_stock_count = inv_stats['low_stock_count']

            trend_dates, trend_values = get_trend_data(today)

            data = {
                'total_orders': total_orders,
                'pending_count': pending_count,
                'processing_count': processing_count,
                'completed_count': completed_count,
                'kit_completion_rate': round(avg_complete_rate * 100, 1),
                'total_inventory': float(total_inventory),
                'low_stock_count': low_stock_count,
                'trend_dates': trend_dates,
                'trend_values': trend_values,
            }
            safe_set(cache_key, data, timeout=300)
            return JsonResponse(data)
        except Exception as e:
            logger.error(f'ScreenDataView error: {e}', exc_info=True)
            return JsonResponse({'error': str(e)}, status=500)