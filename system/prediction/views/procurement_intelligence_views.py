"""
采购智能辅助视图 - 追料提醒、呆滞预警、备料时间线

帮助采购部门：
1. 追料提醒：基于供应商承诺到期日自动催促
2. 呆滞预警：长期未动用的库存识别和处理方案
3. 备料时间线：何时该向哪个供应商下多少量的采购单
"""

import logging
from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Sum, Q, F, Avg, Count, Case, When, Value, IntegerField, DecimalField, Max, Min
from django.db.models.functions import Coalesce
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)

from ..models import (
    SalesOrder, Material, Inventory, SupplierCommitment, SupplierMaterial,
    PurchaseOrder, Supplier, OrderAllocation, MaterialPlanResult, PlanLog
)
from ..constants import ORDER_ACTIVE_STATUSES, PO_ACTIVE_STATUSES, SHIPPING_DAYS


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def procurement_chase_alerts(request):
    """
    追料提醒API - 识别即将到期但可能延期的供应商承诺

    GET参数:
        days_ahead: 提前多少天提醒（默认14天）
    """
    days_ahead = int(request.query_params.get('days_ahead', 14))
    today = date.today()
    alert_date = today + timedelta(days=days_ahead)

    # 查询即将到期且关联活跃订单的承诺
    commitments = SupplierCommitment.objects.filter(
        delivery_date__lte=alert_date,
        delivery_date__gte=today,
    ).select_related('material', 'supplier').order_by('delivery_date')

    alerts = []
    for comm in commitments:
        # 检查是否有依赖此承诺的活跃订单
        related_orders = SalesOrder.objects.filter(
            material_id=comm.material_id,
            status__in=ORDER_ACTIVE_STATUSES,
            demand_date__gte=today,
        ).count()

        if related_orders == 0:
            continue

        days_remaining = (comm.delivery_date - today).days

        # 风险评估
        urgency = 'critical' if days_remaining <= 3 else ('high' if days_remaining <= 7 else ('medium' if days_remaining <= 14 else 'low'))

        # 检查是否有替代供应商
        alt_suppliers = SupplierMaterial.objects.filter(
            material_id=comm.material_id,
            is_forbidden=False,
        ).exclude(supplier_id=comm.supplier_id).count()

        alerts.append({
            'commitment_id': comm.id,
            'supplier_name': comm.supplier.supplier_name if hasattr(comm, 'supplier') and comm.supplier else '',
            'material_code': comm.material.material_code if comm.material else '',
            'material_name': comm.material.material_name if comm.material else '',
            'committed_quantity': int(comm.quantity or 0),
            'delivery_date': str(comm.delivery_date),
            'days_remaining': days_remaining,
            'urgency': urgency,
            'related_active_orders': related_orders,
            'has_alternative_supplier': alt_suppliers > 0,
            'order_no': comm.order_no or '',
            'suggested_action': _suggest_chase_action(urgency, days_remaining, alt_suppliers > 0),
        })

    return Response({
        'success': True,
        'alert_date': str(alert_date),
        'total_alerts': len(alerts),
        'by_urgency': {
            'critical': sum(1 for a in alerts if a['urgency'] == 'critical'),
            'high': sum(1 for a in alerts if a['urgency'] == 'high'),
            'medium': sum(1 for a in alerts if a['urgency'] == 'medium'),
            'low': sum(1 for a in alerts if a['urgency'] == 'low'),
        },
        'alerts': alerts,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def procurement_obsolescence_warning(request):
    """
    呆滞预警API - 识别长期未动用的库存

    GET参数:
        stagnant_days: 超过多少天未动用视为呆滞（默认90天）
    """
    stagnant_days = int(request.query_params.get('stagnant_days', 90))
    today = date.today()
    cutoff_date = today - timedelta(days=stagnant_days)

    # 查询所有非零库存
    inventories = Inventory.objects.filter(
        quantity__gt=0,
        is_hold=False,
    ).select_related('material')

    stagnant_items = []
    total_stagnant_value = Decimal('0')

    for inv in inventories:
        last_movement = inv.updated_at.date() if inv.updated_at else cutoff_date
        days_stagnant = (today - last_movement).days

        if days_stagnant < stagnant_days:
            continue

        # 计算库存价值
        unit_cost = inv.material.standard_cost if inv.material else Decimal('0')
        inventory_value = Decimal(str(inv.quantity or 0)) * unit_cost
        total_stagnant_value += inventory_value

        # 检查未来需求（是否有订单会消耗此物料）
        future_demand = SalesOrder.objects.filter(
            material_id=inv.material_id,
            status__in=ORDER_ACTIVE_STATUSES,
            demand_date__gte=today,
        ).aggregate(total_demand=Sum('quantity'))['total_demand'] or 0

        # 计算消耗速度
        recent_allocations = OrderAllocation.objects.filter(
            material_id=inv.material_id,
        ).order_by('-updated_at')[:5]

        avg_daily_usage = 0
        if recent_allocations:
            # 简化估算：基于最近分配量
            total_recent_alloc = sum(int(a.allocated_quantity or 0) for a in recent_allocations)
            avg_daily_usage = total_recent_alloc / max(stagnant_days, 1)

        days_of_supply = int(inv.quantity / avg_daily_usage) if avg_daily_usage > 0 else 999

        risk_level = '极高' if days_of_supply > 365 else ('高' if days_of_supply > 180 else ('中' if days_of_supply > 90 else '低'))

        stagnant_items.append({
            'inventory_id': inv.id,
            'material_code': inv.material.material_code if inv.material else '',
            'material_name': inv.material.material_name if inv.material else '',
            'warehouse': inv.warehouse or '',
            'quantity': int(inv.quantity or 0),
            'unit_cost': str(unit_cost),
            'inventory_value': str(round(inventory_value, 2)),
            'days_stagnant': days_stagnant,
            'last_movement_date': str(last_movement),
            'future_demand': int(future_demand),
            'avg_daily_usage': round(avg_daily_usage, 2),
            'days_of_supply': days_of_supply,
            'risk_level': risk_level,
            'suggested_action': _suggest_obsolescence_action(risk_level, days_of_supply, future_demand > 0),
        })

    # 按库存价值排序
    stagnant_items.sort(key=lambda x: float(x['inventory_value']), reverse=True)

    return Response({
        'success': True,
        'stagnant_days_threshold': stagnant_days,
        'total_stagnant_items': len(stagnant_items),
        'total_stagnant_value': str(round(total_stagnant_value, 2)),
        'by_risk_level': {
            '极高': sum(1 for s in stagnant_items if s['risk_level'] == '极高'),
            '高': sum(1 for s in stagnant_items if s['risk_level'] == '高'),
            '中': sum(1 for s in stagnant_items if s['risk_level'] == '中'),
            '低': sum(1 for s in stagnant_items if s['risk_level'] == '低'),
        },
        'items': stagnant_items[:50],  # 最多返回50条
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def procurement_timeline(request):
    """
    备料时间线API - 告诉采购员何时向谁买什么

    基于活跃订单的需求日期、物料提前期、当前库存，生成采购行动时间线
    """
    today = date.today()

    # 获取所有待处理的活跃订单及其物料需求
    active_orders = SalesOrder.objects.filter(
        status__in=ORDER_ACTIVE_STATUSES,
        demand_date__gte=today,
    ).select_related('material').order_by('demand_date', 'priority')

    # 按物料聚合需求
    material_demands = {}  # {material_id: [{order, qty, needed_by_date}]}

    for order in active_orders:
        # 简化：直接使用成品物料作为需求（实际应展开BOM）
        material_id = order.material_id
        shipping_days = getattr(order, 'shipping_days', 45) or 45
        production_lt = 2
        needed_by = order.demand_date - timedelta(days=shipping_days + production_lt) if order.demand_date else today

        if material_id not in material_demands:
            material_demands[material_id] = []
        material_demands[material_id].append({
            'order_no': order.order_no,
            'quantity': int(order.quantity or 0),
            'needed_by_date': needed_by,
            'demand_date': order.demand_date,
            'priority': order.priority,
        })

    # 为每个物料生成采购建议
    timeline_entries = []

    for material_id, demands in material_demands.items():
        # 当前库存
        current_stock = Inventory.objects.filter(
            material_id=material_id, is_hold=False
        ).aggregate(total=Sum('quantity'))['total'] or 0

        total_demand = sum(d['quantity'] for d in demands)
        net_demand = max(0, total_demand - int(current_stock))

        if net_demand <= 0:
            continue  # 库存充足，不需要采购

        # 找最优供应商
        best_suppliers = SupplierMaterial.objects.filter(
            material_id=material_id,
            is_forbidden=False,
        ).select_related('supplier', 'material').order_by('unit_price', 'lead_time')[:5]

        earliest_need = min(d['needed_by_date'] for d in demands)
        latest_need = max(d['needed_by_date'] for d in demands)

        supplier_options = []
        for sm in best_suppliers:
            lead_time = sm.lead_time or 7
            latest_order_date = earliest_need - timedelta(days=lead_time)
            days_to_order = (latest_order_date - today).days

            supplier_options.append({
                'supplier_name': sm.supplier.supplier_name if sm.supplier else '',
                'supplier_rating': sm.supplier.rating if sm.supplier else '',
                'unit_price': str(round(float(sm.unit_price or 0), 2)),
                'lead_time_days': lead_time,
                'latest_order_date': str(latest_order_date),
                'days_to_order': days_to_order,
                'urgency': '立即' if days_to_order <= 0 else (
                    f'{days_to_order}天内' if days_to_order <= 7 else (
                        f'{days_to_order}天内' if days_to_order <= 30 else f'可在{days_to_order}天后下单'
                    )),
            })

        # 获取物料信息
        try:
            material = Material.objects.get(id=material_id)
            mat_code = material.material_code
            mat_name = material.material_name
            safety_stock = material.safety_stock or 0
        except Material.DoesNotExist:
            mat_code = f'MAT-{material_id}'
            mat_name = '未知物料'
            safety_stock = 0

        timeline_entries.append({
            'material_id': material_id,
            'material_code': mat_code,
            'material_name': mat_name,
            'current_stock': int(current_stock),
            'total_demand': total_demand,
            'net_demand': net_demand,
            'safety_stock': safety_stock,
            'recommended_order_qty': net_demand + safety_stock,  # 含安全库存
            'earliest_need_by': str(earliest_need),
            'latest_need_by': str(latest_need),
            'related_orders_count': len(demands),
            'related_orders': [d['order_no'] for d in demands[:5]],
            'supplier_options': supplier_options,
            'status': 'urgent' if (earliest_need - today).days <= 14 else (
                'planned' if (earliest_need - today).days <= 30 else 'normal'
            ),
        })

    # 按紧急程度排序
    status_order = {'urgent': 0, 'planned': 1, 'normal': 2}
    timeline_entries.sort(key=lambda x: (status_order.get(x['status'], 99), x['earliest_need_by']))

    return Response({
        'success': True,
        'generated_at': today.isoformat(),
        'total_materials_needing_procurement': len(timeline_entries),
        'timeline': timeline_entries,
    })


def _suggest_chase_action(urgency, days_remaining, has_alternative):
    """生成追料建议动作"""
    if urgency == 'critical':
        if has_alternative:
            return '立即联系备选供应商安排加急；同时向原供应商发出正式催交通知'
        return '立即联系供应商确认生产进度；必要时升级至供应商管理层'
    elif urgency == 'high':
        if has_alternative:
            return '向原供应商确认进度；同步向备选供应商下达预备订单'
        return '发送正式催交通知；确认物流方式是否需要调整'
    elif urgency == 'medium':
        return '发送友好提醒邮件；确认生产计划是否正常'
    return '正常跟进即可'


def _suggest_obsolescence_action(risk_level, days_of_supply, has_future_demand):
    """生成呆滞处理建议"""
    if risk_level == '极高':
        if has_future_demand:
            return '虽有未来需求但消耗极慢，建议评估是否超量采购；考虑促销或调剂给其他产品线'
        return '无未来需求且积压严重，建议立即启动呆滞清理：折价出售/退货/报废'
    elif risk_level == '高':
        if has_future_demand:
            return '消耗偏慢，建议暂停采购并监控消耗趋势'
        return '未来需求不明，建议寻找替代用途或联系供应商协商退换'
    elif risk_level == '中':
        return '开始关注消耗趋势，适当控制后续采购量'
    return '正常监控即可'


# ==================== 结构化智能采购决策系统 ====================


def _calculate_urgency_score(material_id: int, demands: list, inventory: int, today: date) -> int:
    """
    计算物料紧急度评分（0-100）

    综合考量以下维度：
    - 距最晚采购日的剩余天数（越近越紧急）
    - 缺料影响的活跃订单数量（越多越紧急）
    - 替代供应商可用性（无可替代则更紧急）
    - 当前库存覆盖天数（越低越紧急）

    Args:
        material_id: 物料ID
        demands: 该物料的需求列表，每项包含 quantity 和 needed_by_date
        inventory: 当前可用库存数量
        today: 当前日期

    Returns:
        int: 紧急度评分，0-100，越高越紧急
    """
    score = 0

    if not demands:
        return 0

    # 1. 距最晚采购日维度（权重40%）
    earliest_need = min(d.get('needed_by_date', today) for d in demands)
    days_to_need = (earliest_need - today).days

    if days_to_need <= 0:
        time_score = 100  # 已过期，最高紧急度
    elif days_to_need <= 3:
        time_score = 95
    elif days_to_need <= 7:
        time_score = 80
    elif days_to_need <= 14:
        time_score = 60
    elif days_to_need <= 30:
        time_score = 40
    elif days_to_need <= 60:
        time_score = 20
    else:
        time_score = 5
    score += time_score * 0.4

    # 2. 影响订单数维度（权重25%）
    total_demand_qty = sum(d.get('quantity', 0) for d in demands)
    order_count = len(demands)

    if order_count >= 10:
        order_score = 100
    elif order_count >= 5:
        order_score = 75
    elif order_count >= 3:
        order_score = 50
    elif order_count >= 1:
        order_score = 30
    else:
        order_score = 0
    score += order_score * 0.25

    # 3. 库存覆盖维度（权重20%）
    avg_daily_usage = max(total_demand_qty / max((earliest_need - today).days, 1), 1) if total_demand_qty > 0 else 1
    days_of_supply = inventory / avg_daily_usage if avg_daily_usage > 0 else 999

    if days_of_supply <= 0:
        coverage_score = 100
    elif days_of_supply <= 7:
        coverage_score = 90
    elif days_of_supply <= 14:
        coverage_score = 70
    elif days_of_supply <= 30:
        coverage_score = 45
    elif days_of_supply <= 60:
        coverage_score = 20
    else:
        coverage_score = 5
    score += coverage_score * 0.2

    # 4. 替代供应商维度（权重15%）
    alt_supplier_count = SupplierMaterial.objects.filter(
        material_id=material_id,
        is_forbidden=False,
    ).count()

    if alt_supplier_count == 0:
        alt_score = 100  # 无替代供应商，高紧急度
    elif alt_supplier_count == 1:
        alt_score = 70
    elif alt_supplier_count <= 3:
        alt_score = 40
    else:
        alt_score = 10
    score += alt_score * 0.15

    return min(100, max(0, int(score)))


def _calculate_risk_score(material_id: int, suppliers: list, commitments: list) -> int:
    """
    计算物料采购风险评分（0-100）

    综合考量以下维度：
    - 供应商交付可靠率（越低风险越高）
    - 单一供应源风险（仅一家则高风险）
    - 价格波动风险（基于历史PO价格方差）
    - 供应商承诺履约情况

    Args:
        material_id: 物料ID
        suppliers: 该物料的供应商物料关系列表（SupplierMaterial queryset或list）
        commitments: 该物料的供应商承诺列表

    Returns:
        int: 风险评分，0-100，越高风险越大
    """
    score = 0

    supplier_materials = SupplierMaterial.objects.filter(
        material_id=material_id,
        is_forbidden=False,
    ).select_related('supplier')

    supplier_count = supplier_materials.count()

    if supplier_count == 0:
        return 100  # 无可用供应商，极高风险

    # 1. 单一供应源风险（权重25%）
    if supplier_count == 1:
        single_source_score = 100
    elif supplier_count == 2:
        single_source_score = 65
    elif supplier_count <= 3:
        single_source_score = 35
    else:
        single_source_score = 10
    score += single_source_score * 0.25

    # 2. 供应商交付可靠率（权重35%）
    avg_reliability = supplier_materials.aggregate(
        avg_rel=Avg('supplier__delivery_reliability')
    )['avg_rel'] or 0.9

    reliability_risk = (1.0 - avg_reliability) * 100  # 可靠率越低，风险越高
    score += reliability_risk * 0.35

    # 3. 价格波动风险（权重20%）
    price_stats = supplier_materials.aggregate(
        avg_price=Avg('unit_price'),
        max_price=Max('unit_price') if hasattr(SupplierMaterial.objects, 'aggregate') else None,
        min_price=Min('unit_price') if hasattr(SupplierMaterial.objects, 'aggregate') else None,
    )

    avg_price = float(price_stats['avg_price'] or 0)
    if avg_price > 0:
        max_p = float(price_stats.get('max_price') or avg_price)
        min_p = float(price_stats.get('min_price') or avg_price)
        price_variance = (max_p - min_p) / avg_price if avg_price > 0 else 0
        price_risk_score = min(100, int(price_variance * 200))  # 波动越大风险越高
    else:
        price_risk_score = 50  # 无法判断，中等风险
    score += price_risk_score * 0.20

    # 4. 供应商评级风险（权重20%）
    rating_risk_map = {'D': 100, 'C': 70, 'B': 40, 'A': 10}
    worst_rating = supplier_materials.annotate(
        sup_rating=F('supplier__rating')
    ).order_by('sup_rating').first()

    rating_score = rating_risk_map.get(
        worst_rating.supplier.rating if worst_rating and worst_rating.supplier else 'B', 40
    )
    score += rating_score * 0.20

    return min(100, max(0, int(score)))


def _calculate_confidence_score(forecast_data: dict) -> int:
    """
    计算预测置信度评分（0-100）

    基于Prophet预测的置信区间宽度、历史数据量、趋势稳定性等因子评估。

    Args:
        forecast_data: 预测数据字典，可能包含以下字段：
            - forecast_value: 预测值
            - upper_bound: 预测上界
            - lower_bound: 预测下界
            - history_data_points: 历史数据点数量
            - trend: 趋势方向 ('increasing'/'decreasing'/'stable')
            - seasonality_strength: 季节性强度

    Returns:
        int: 置信度评分，0-100，越高越可信
    """
    if not forecast_data:
        return 50  # 无预测数据，默认中等置信度

    score = 100

    # 1. 置信区间宽度影响（区间越宽，置信度越低）
    upper = forecast_data.get('upper_bound', 0)
    lower = forecast_data.get('lower_bound', 0)
    forecast_val = forecast_data.get('forecast_value', 0)

    if forecast_val > 0 and upper > 0 and lower > 0:
        interval_width = (upper - lower) / forecast_val
        if interval_width > 1.0:
            score -= 40  # 区间超过预测值本身，低置信度
        elif interval_width > 0.5:
            score -= 25
        elif interval_width > 0.2:
            score -= 10
        # 区间窄则不减分

    # 2. 历史数据量影响
    data_points = forecast_data.get('history_data_points', 0)
    if data_points < 30:
        score -= 30  # 数据不足一个月
    elif data_points < 90:
        score -= 15  # 不足三个月
    # 足够数据不减分

    # 3. 趋势稳定性
    trend = forecast_data.get('trend', 'stable')
    if trend == 'stable':
        pass  # 稳定趋势不扣分
    elif trend in ('increasing', 'decreasing'):
        score -= 5  # 有方向性趋势，轻微降置信度
    else:
        score -= 10  # 不明趋势

    return min(100, max(0, int(score)))


def _rank_suppliers(supplier_materials, material_id: int) -> list:
    """
    对指定物料的供应商进行综合排序

    排序依据：交付可靠率(40%) + 价格竞争力(25%) + 交期长短(20%) + 供应商评级(15%)

    Args:
        supplier_materials: SupplierMaterial queryset（已过滤该物料）
        material_id: 物料ID

    Returns:
        list: 排序后的供应商列表，每项包含：
            - supplier_name: 供应商名称
            - on_time_prob: 准时交付概率（基于delivery_reliability）
            - unit_price: 单价
            - lead_time: 交货周期（天）
            - risk_factors: 风险因素列表
            - composite_score: 综合得分
    """
    sms = list(supplier_materials.select_related('supplier').filter(
        material_id=material_id,
        is_forbidden=False,
    ))

    if not sms:
        return []

    ranked = []
    # 获取价格基准用于计算价格竞争力
    prices = [float(sm.unit_price or 0) for sm in sms if sm.unit_price]
    min_price = min(prices) if prices else 1
    max_price = max(prices) if prices else 1
    price_range = max_price - min_price if max_price != min_price else 1

    # 获取交期基准
    lead_times = [sm.lead_time or 7 for sm in sms]
    min_lt = min(lead_times) if lead_times else 7
    max_lt = max(lead_times) if lead_times else 7
    lt_range = max_lt - min_lt if max_lt != min_lt else 1

    rating_scores = {'A': 100, 'B': 75, 'C': 50, 'D': 25}

    for sm in sms:
        supplier = sm.supplier
        if not supplier:
            continue

        # 准时交付概率（0-100）
        on_time_prob = int((supplier.delivery_reliability or 0.9) * 100)

        # 价格竞争力得分（价格越低分越高）
        unit_price_val = float(sm.unit_price or 0)
        if price_range > 0:
            price_score = 100 - int(((unit_price_val - min_price) / price_range) * 100)
        else:
            price_score = 100

        # 交期得分（交期越短分越高）
        lead_time_val = sm.lead_time or 7
        if lt_range > 0:
            lt_score = 100 - int(((lead_time_val - min_lt) / lt_range) * 100)
        else:
            lt_score = 100

        # 评级得分
        rating_score = rating_scores.get(supplier.rating, 50)

        # 综合加权得分
        composite_score = (
            on_time_prob * 0.40 +
            price_score * 0.25 +
            lt_score * 0.20 +
            rating_score * 0.15
        )

        # 收集风险因素
        risk_factors = []
        if on_time_prob < 70:
            risk_factors.append(f'交付准时率偏低({on_time_prob}%)')
        if supplier.rating in ('C', 'D'):
            risk_factors.append(f'供应商评级较低({supplier.rating}级)')
        if lead_time_val > 30:
            risk_factors.append(f'交期较长({lead_time_val}天)')
        if supplier_count_for_material := SupplierMaterial.objects.filter(
            material_id=material_id, is_forbidden=False
        ).count():
            if supplier_count_for_material == 1:
                risk_factors.append('单一供应源风险')

        ranked.append({
            'supplier_name': supplier.supplier_name,
            'supplier_id': supplier.id,
            'on_time_prob': on_time_prob,
            'unit_price': str(round(unit_price_val, 2)),
            'lead_time': lead_time_val,
            'risk_factors': risk_factors,
            'composite_score': round(composite_score, 1),
        })

    # 按综合得分降序排列
    ranked.sort(key=lambda x: x['composite_score'], reverse=True)
    return ranked


def _generate_suggested_action(urgency: int, risk: int, confidence: int) -> str:
    """
    根据紧急度、风险和置信度生成建议动作

    动作枚举值：
    - immediate_order: 立即下单（极高紧急度）
    - urgent_order: 紧急下单（高紧急度）
    - normal_order: 正常下单（中等紧急度和风险）
    - defer: 延后处理（低紧急度）
    - switch_supplier: 切换供应商（高风险+有替代方案时推荐）

    Args:
        urgency: 紧急度评分（0-100）
        risk: 风险评分（0-100）
        confidence: 置信度评分（0-100）

    Returns:
        str: 建议动作字符串
    """
    if urgency >= 80:
        if risk >= 70:
            return 'immediate_order'  # 高紧急+高风险 → 立即行动
        return 'urgent_order'  # 高紧急+可接受风险 → 紧急下单
    elif urgency >= 50:
        if risk >= 75:
            return 'switch_supplier'  # 中等紧急+高风险 → 考虑切换供应商
        return 'normal_order'  # 中等紧急+可控风险 → 正常下单
    elif urgency >= 20:
        if confidence < 40:
            return 'defer'  # 低紧急+低置信度 → 延后观察
        return 'normal_order'
    else:
        return 'defer'  # 极低紧急度 → 可延后


# ========== API: 结构化采购智能推荐 ==========


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def procurement_intelligent_recommendations(request):
    """
    结构化采购智能推荐API - 从"文本建议"升级为"结构化决策系统"

    为每个需要采购的物料生成完整的决策包，包含：
    - 推荐采购量（考虑安全库存+需求预测）
    - 多维评分（紧急度/风险/置信度）
    - 供应商排名与对比
    - 建议动作与替代方案

    GET参数:
        min_urgency: 最低紧急度筛选阈值（默认0）
        max_risk: 最高风险筛选阈值（默认100）
        status: 状态筛选（pending/ordered/deferred，默认返回全部）
        limit: 返回条数上限（默认50）
    """
    min_urgency = int(request.query_params.get('min_urgency', 0))
    max_risk = int(request.query_params.get('max_risk', 100))
    status_filter = request.query_params.get('status', '')
    limit = int(request.query_params.get('limit', 50))

    today = date.today()

    # 获取所有活跃订单及其物料需求
    active_orders = SalesOrder.objects.filter(
        status__in=ORDER_ACTIVE_STATUSES,
        demand_date__gte=today,
    ).select_related('material').order_by('demand_date', 'priority')

    # 按物料聚合需求
    material_demands: dict[int, list] = {}
    for order in active_orders:
        material_id = order.material_id
        shipping_days = getattr(order, 'shipping_days', 45) or 45
        production_lt = getattr(order, 'production_lead_time', 2) or 2
        needed_by = order.demand_date - timedelta(days=shipping_days + production_lt) if order.demand_date else today

        if material_id not in material_demands:
            material_demands[material_id] = []
        material_demands[material_id].append({
            'order_no': order.order_no,
            'quantity': int(order.quantity or 0),
            'needed_by_date': needed_by,
            'demand_date': order.demand_date,
            'priority': order.priority or 1,
        })

    # 批量获取库存信息
    material_ids = list(material_demands.keys())
    inventories_dict = {}
    if material_ids:
        inv_qs = Inventory.objects.filter(
            material_id__in=material_ids,
            is_hold=False,
        ).values('material_id').annotate(
            total_qty=Sum('quantity')
        )
        inventories_dict = {inv['material_id']: inv['total_qty'] or 0 for inv in inv_qs}

    # 批量获取供应商承诺
    commitments_dict: dict[int, list] = {}
    if material_ids:
        comm_qs = SupplierCommitment.objects.filter(
            material_id__in=material_ids,
            delivery_date__gte=today,
        ).select_related('supplier')
        for comm in comm_qs:
            mid = comm.material_id
            if mid not in commitments_dict:
                commitments_dict[mid] = []
            commitments_dict[mid].append(comm)

    # 为每个物料生成完整推荐
    recommendations = []

    for material_id, demands in material_demands.items():
        current_stock = int(inventories_dict.get(material_id, 0))
        total_demand = sum(d['quantity'] for d in demands)
        net_demand = max(0, total_demand - current_stock)

        if net_demand <= 0:
            continue

        # 获取物料详情
        try:
            material = Material.objects.get(id=material_id)
            mat_code = material.material_code
            mat_name = material.material_name
            safety_stock = material.safety_stock or 0
            standard_cost = float(material.standard_cost or 0)
            min_order_qty = material.min_order_qty or 1
        except Material.DoesNotExist:
            mat_code = f'MAT-{material_id}'
            mat_name = '未知物料'
            safety_stock = 0
            standard_cost = 0
            min_order_qty = 1

        # 推荐采购量 = 净需求 + 安全库存（向上取整到MOQ）
        recommended_quantity = net_demand + safety_stock
        if recommended_quantity > 0 and recommended_quantity < min_order_qty:
            recommended_quantity = min_order_qty
        # 向上取整到整数
        recommended_quantity = int(recommended_quantity)

        # 计算多维评分
        urgency_score = _calculate_urgency_score(material_id, demands, current_stock, today)
        risk_score = _calculate_risk_score(material_id, [], commitments_dict.get(material_id, []))

        # 预测数据（基于当前需求估算，上下限为±20%浮动范围）
        forecast_data = {
            'forecast_value': float(total_demand),
            'upper_bound': float(total_demand * 1.2),
            'lower_bound': float(total_demand * 0.8),
            'history_data_points': len(demands) * 10,  # 基于需求条数估算的历史数据量
            'trend': 'stable',
        }
        confidence_score = _calculate_confidence_score(forecast_data)

        # 供应商排名
        supplier_ranking = _rank_suppliers(SupplierMaterial.objects, material_id)

        # 建议动作
        suggested_action = _generate_suggested_action(urgency_score, risk_score, confidence_score)

        # 最晚下单日期（基于最早需求和最优供应商交期）
        earliest_need = min(d['needed_by_date'] for d in demands)
        best_lead_time = supplier_ranking[0]['lead_time'] if supplier_ranking else 7
        latest_order_date = earliest_need - timedelta(days=best_lead_time)

        # 预估总成本（使用排名第一的供应商单价）
        estimated_cost = 0
        if supplier_ranking:
            estimated_cost = recommended_quantity * float(supplier_ranking[0]['unit_price'])
        elif standard_cost > 0:
            estimated_cost = recommended_quantity * standard_cost

        # 替代方案
        alternative_options = []
        alt_suppliers = supplier_ranking[1:4] if len(supplier_ranking) > 1 else []

        for idx, alt_sup in enumerate(alt_suppliers):
            alt_cost = recommended_quantity * float(alt_sup['unit_price'])
            alternative_options.append({
                'rank': idx + 2,
                'supplier_name': alt_sup['supplier_name'],
                'unit_price': alt_sup['unit_price'],
                'estimated_total_cost': str(round(alt_cost, 2)),
                'lead_time_days': alt_sup['lead_time'],
                'on_time_prob': alt_sup['on_time_prob'],
                'pros': f'备选方案#{idx + 2}' if idx == 0 else '',
            })

        # 替代物料方案
        substitute_options = []
        try:
            from ..models import SubstituteMaterial
            subs = SubstituteMaterial.objects.filter(
                material_id=material_id,
                is_active=True,
            ).select_related('material')[:3]
            for sub in subs:
                if sub.material_id != material_id:
                    substitute_options.append({
                        'substitute_material_code': sub.material.material_code if sub.material else '',
                        'substitute_material_name': sub.material.material_name if sub.material else '',
                        'priority': sub.priority,
                        'ratio': sub.ratio,
                        'group_id': sub.group_id,
                    })
        except Exception:
            pass  # 替代物料查询非核心功能，失败不影响主流程

        # 状态判定
        if urgency_score >= 80:
            rec_status = 'urgent'
        elif urgency_score >= 50:
            rec_status = 'pending'
        else:
            rec_status = 'normal'

        # 应用状态过滤器
        if status_filter and rec_status != status_filter:
            continue

        # 应用评分过滤器
        if urgency_score < min_urgency or risk_score > max_risk:
            continue

        recommendations.append({
            'material_id': material_id,
            'material_code': mat_code,
            'material_name': mat_name,
            'recommended_quantity': recommended_quantity,
            'urgency_score': urgency_score,
            'risk_score': risk_score,
            'confidence_score': confidence_score,
            'supplier_ranking': supplier_ranking,
            'suggested_action': suggested_action,
            'estimated_cost': str(round(estimated_cost, 2)),
            'latest_order_date': str(latest_order_date),
            'alternative_options': {
                'alternative_suppliers': alternative_options,
                'substitute_materials': substitute_options,
            },
            'status': rec_status,
            'current_stock': current_stock,
            'total_demand': total_demand,
            'related_orders_count': len(demands),
        })

    # 按紧急度降序排列
    recommendations.sort(key=lambda x: x['urgency_score'], reverse=True)

    return Response({
        'success': True,
        'generated_at': today.isoformat(),
        'total_recommendations': len(recommendations),
        'filters_applied': {
            'min_urgency': min_urgency,
            'max_risk': max_risk,
            'status': status_filter or 'all',
        },
        'summary': {
            'urgent_count': sum(1 for r in recommendations if r['urgency_score'] >= 80),
            'high_urgency_count': sum(1 for r in recommendations if 50 <= r['urgency_score'] < 80),
            'normal_count': sum(1 for r in recommendations if r['urgency_score'] < 50),
            'total_estimated_cost': str(round(
                sum(float(r['estimated_cost']) for r in recommendations), 2
            )),
            'avg_urgency': round(
                sum(r['urgency_score'] for r in recommendations) / max(len(recommendations), 1), 1
            ),
            'avg_risk': round(
                sum(r['risk_score'] for r in recommendations) / max(len(recommendations), 1), 1
            ),
        },
        'recommendations': recommendations[:limit],
    })


# ========== API: 采购风险仪表盘 ==========


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def procurement_risk_dashboard(request):
    """
    采购风险仪表盘API - 全局采购风险态势聚合展示

    提供五大风险维度的量化评估、Top10高风险物料识别、
    供应商健康度分布、成本敞口汇总及周环比趋势。

    GET参数:
        lookback_days: 趋势对比回溯天数（默认7天）
    """
    lookback_days = int(request.query_params.get('lookback_days', 7))
    today = date.today()
    week_ago = today - timedelta(days=lookback_days)

    # ===== 1. 整体风险评分 =====
    overall_risk_score = _compute_overall_risk_score(today)

    # ===== 2. 五大风险分解 =====
    risk_breakdown = _compute_risk_breakdown(today, week_ago, lookback_days)

    # ===== 3. Top10 高风险物料 =====
    top_risk_materials = _get_top_risk_materials(today, limit=10)

    # ===== 4. 供应商健康度分布 =====
    supplier_health_map = _compute_supplier_health_map()

    # ===== 5. 成本敞口汇总 =====
    cost_exposure_summary = _compute_cost_exposure(today)

    # ===== 6. 趋势指标（对比上周） =====
    trend_indicators = _compute_trend_indicators(today, week_ago)

    return Response({
        'success': True,
        'as_of': today.isoformat(),
        'overall_risk_score': overall_risk_score,
        'risk_level': _risk_level_label(overall_risk_score),
        'risk_breakdown': risk_breakdown,
        'top_risk_materials': top_risk_materials,
        'supplier_health_map': supplier_health_map,
        'cost_exposure_summary': cost_exposure_summary,
        'trend_indicators': trend_indicators,
    })


def _compute_overall_risk_score(today: date) -> int:
    """计算整体采购风险评分（0-100）"""
    scores = []

    # 维度A：逾期/即将逾期承诺占比
    total_comm = SupplierCommitment.objects.filter(delivery_date__gte=today).count()
    overdue_comm = SupplierCommitment.objects.filter(
        delivery_date__lt=today,
        delivery_date__gte=today - timedelta(days=7),
    ).count()
    if total_comm > 0:
        scores.append(min(100, int(overdue_comm / max(total_comm, 1) * 100)))

    # 维度B：缺料订单占比
    from ..models import MaterialPlanResult
    incomplete_plans = MaterialPlanResult.objects.filter(
        is_complete=False,
        updated_at__date__gte=today - timedelta(days=30),
    ).count()
    total_plans = MaterialPlanResult.objects.filter(
        updated_at__date__gte=today - timedelta(days=30),
    ).count()
    if total_plans > 0:
        scores.append(int(incomplete_plans / total_plans * 100))

    # 维度C：Hold库存价值占比
    hold_inv_value = Inventory.objects.filter(is_hold=True).aggregate(
        total=Sum(F('quantity') * 1)  # 简化计数
    )['total'] or 0
    total_inv = Inventory.objects.count()
    if total_inv > 0:
        scores.append(min(100, int(hold_inv_value / total_inv * 50)))

    # 维度D：低评级供应商物料占比
    low_rating_sm = SupplierMaterial.objects.filter(
        is_forbidden=False,
        supplier__rating__in=['C', 'D'],
    ).count()
    total_sm = SupplierMaterial.objects.filter(is_forbidden=False).count()
    if total_sm > 0:
        scores.append(int(low_rating_sm / total_sm * 100))

    return int(sum(scores) / max(len(scores), 1)) if scores else 0


def _compute_risk_breakdown(today: date, week_ago: date, lookback_days: int = 7) -> dict:
    """计算五大风险维度分解"""
    breakdown = {}

    # A. 供应商延期风险
    recent_delays = SupplierCommitment.objects.filter(
        delivery_date__lt=today,
        delivery_date__gte=week_ago,
    ).count()
    total_active_comm = SupplierCommitment.objects.filter(
        delivery_date__gte=today,
    ).count()
    delay_ratio = recent_delays / max(total_active_comm, 1) * 100
    breakdown['supplier_delay_risk'] = {
        'score': min(100, int(delay_ratio * 2)),  # 放大系数使信号更明显
        'label': '供应商延期风险',
        'detail': f'近期{recent_delays}条承诺已逾期，占活跃承诺{delay_ratio:.1f}%',
        'trend': 'up' if recent_delays > 3 else 'stable',
    }

    # B. 质量冻结风险
    hold_invs = Inventory.objects.filter(is_hold=True).select_related('material')
    hold_count = hold_invs.count()
    hold_value_est = sum(
        (inv.quantity or 0) * float(inv.material.standard_cost or 0)
        for inv in hold_invs if inv.material
    )
    breakdown['quality_hold_risk'] = {
        'score': min(100, hold_count * 5),  # 每条hold记5分
        'label': '质量冻结风险',
        'detail': f'当前{hold_count}条库存记录被冻结，估算冻结金额¥{hold_value_est:,.2f}',
        'trend': 'up' if hold_count > 10 else 'stable',
    }

    # C. 库存呆滞风险
    stagnant_threshold = 90
    cutoff = today - timedelta(days=stagnant_threshold)
    stagnant_invs = Inventory.objects.filter(
        quantity__gt=0,
        is_hold=False,
        updated_at__date__lte=cutoff,
    ).count()
    breakdown['inventory_obsolescence_risk'] = {
        'score': min(100, stagnant_invs * 3),
        'label': '库存呆滞风险',
        'detail': f'{stagnant_threshold}天未动用库存{stagnant_invs}项',
        'trend': 'up' if stagnant_invs > 20 else 'stable',
    }

    # D. 单源供应风险
    from django.db.models.functions import Concat
    single_source_materials = Material.objects.filter(
        id__in=SupplierMaterial.objects.filter(
            is_forbidden=False,
        ).values('material_id').annotate(
            cnt=Count('supplier_id', distinct=True)
        ).filter(cnt=1).values('material_id')
    ).count()
    total_sourced_materials = Material.objects.filter(
        id__in=SupplierMaterial.objects.filter(
            is_forbidden=False,
        ).values_list('material_id', flat=True).distinct()
    ).count()
    single_source_ratio = single_source_materials / max(total_sourced_materials, 1) * 100
    breakdown['single_source_risk'] = {
        'score': int(single_source_ratio),
        'label': '单源供应风险',
        'detail': f'{single_source_materials}个物料仅有单一供应商（占比{single_source_ratio:.1f}%）',
        'trend': 'stable',
    }

    # E. 需求激增风险
    recent_orders = SalesOrder.objects.filter(
        created_at__date__gte=week_ago,
        status__in=ORDER_ACTIVE_STATUSES,
    ).count()
    prev_orders = SalesOrder.objects.filter(
        created_at__date__gte=week_ago - timedelta(days=lookback_days),
        created_at__date__lt=week_ago,
        status__in=ORDER_ACTIVE_STATUSES,
    ).count()
    surge_ratio = (recent_orders - prev_orders) / max(prev_orders, 1) * 100
    breakdown['demand_surge_risk'] = {
        'score': min(100, max(0, int(surge_ratio))),
        'label': '需求激增风险',
        'detail': f'本周新增{recent_orders}单 vs 上周{prev_orders}单（变化{surge_ratio:+.1f}%）',
        'trend': 'up' if surge_ratio > 20 else ('down' if surge_ratio < -20 else 'stable'),
    }

    return breakdown


def _get_top_risk_materials(today: date, limit: int = 10) -> list:
    """获取Top N高风险物料"""
    active_orders = SalesOrder.objects.filter(
        status__in=ORDER_ACTIVE_STATUSES,
        demand_date__gte=today,
    ).select_related('material')

    material_demands: dict[int, list] = {}
    for order in active_orders:
        mid = order.material_id
        shipping_days = getattr(order, 'shipping_days', 45) or 45
        production_lt = getattr(order, 'production_lead_time', 2) or 2
        needed_by = order.demand_date - timedelta(days=shipping_days + production_lt) if order.demand_date else today
        if mid not in material_demands:
            material_demands[mid] = []
        material_demands[mid].append({
            'quantity': int(order.quantity or 0),
            'needed_by_date': needed_by,
        })

    material_ids = list(material_demands.keys())
    inventories_dict = {}
    if material_ids:
        inv_qs = Inventory.objects.filter(
            material_id__in=material_ids, is_hold=False
        ).values('material_id').annotate(total=Sum('quantity'))
        inventories_dict = {inv['material_id']: inv['total'] or 0 for inv in inv_qs}

    risk_items = []
    for material_id, demands in material_demands.items():
        stock = int(inventories_dict.get(material_id, 0))
        urgency = _calculate_urgency_score(material_id, demands, stock, today)
        risk = _calculate_risk_score(material_id, [], [])

        # 综合风险 = 紧急度 * 0.6 + 风险 * 0.4
        composite_risk = urgency * 0.6 + risk * 0.4

        try:
            mat = Material.objects.get(id=material_id)
            mat_code = mat.material_code
            mat_name = mat.material_name
        except Material.DoesNotExist:
            mat_code = f'MAT-{material_id}'
            mat_name = '未知'

        risk_items.append({
            'material_id': material_id,
            'material_code': mat_code,
            'material_name': mat_name,
            'composite_risk_score': round(composite_risk, 1),
            'urgency_score': urgency,
            'risk_score': risk,
            'current_stock': stock,
            'demand_count': len(demands),
            'primary_risk_factor': _identify_primary_risk_factor(urgency, risk),
        })

    risk_items.sort(key=lambda x: x['composite_risk_score'], reverse=True)
    return risk_items[:limit]


def _identify_primary_risk_factor(urgency: int, risk: int) -> str:
    """识别主要风险驱动因素"""
    if urgency >= 80:
        return '时间紧迫'
    if risk >= 70:
        return '供应商风险'
    if urgency >= 50:
        return '需求压力'
    if risk >= 50:
        return '供应不稳定'
    return '一般监控'


def _compute_supplier_health_map() -> dict:
    """计算供应商健康度分布（A/B/C/D级各有多少物料）"""
    health_data = SupplierMaterial.objects.filter(
        is_forbidden=False,
    ).values('supplier__rating').annotate(
        material_count=Count('material_id', distinct=True),
        supplier_count=Count('supplier_id', distinct=True),
    ).order_by('supplier__rating')

    result = {'A': {'material_count': 0, 'supplier_count': 0},
              'B': {'material_count': 0, 'supplier_count': 0},
              'C': {'material_count': 0, 'supplier_count': 0},
              'D': {'material_count': 0, 'supplier_count': 0}}

    for item in health_data:
        rating = item['supplier__rating']
        if rating in result:
            result[rating] = {
                'material_count': item['material_count'],
                'supplier_count': item['supplier_count'],
            }

    total_materials = sum(v['material_count'] for v in result.values())
    for rating in result:
        result[rating]['percentage'] = round(
            result[rating]['material_count'] / max(total_materials, 1) * 100, 1
        )

    return result


def _compute_cost_exposure(today: date) -> dict:
    """计算成本敞口汇总"""
    # 待采购物料的预估成本敞口
    active_orders = SalesOrder.objects.filter(
        status__in=ORDER_ACTIVE_STATUSES,
        demand_date__gte=today,
    ).select_related('material')

    exposed_materials = set()
    total_exposure = Decimal('0')
    high_exposure_items = []

    for order in active_orders:
        mid = order.material_id
        if mid in exposed_materials:
            continue
        exposed_materials.add(mid)

        try:
            mat = Material.objects.get(id=mid)
            stock = Inventory.objects.filter(
                material_id=mid, is_hold=False
            ).aggregate(total=Sum('quantity'))['total'] or 0
            demand_qty = int(order.quantity or 0)
            net_need = max(0, demand_qty - int(stock))

            if net_need > 0:
                # 取最低价供应商单价作为基准
                best_price = SupplierMaterial.objects.filter(
                    material_id=mid, is_forbidden=False
                ).order_by('unit_price').first()
                unit_p = float(best_price.unit_price) if best_price and best_price.unit_price else float(mat.standard_cost or 0)
                exposure = Decimal(str(net_need * unit_p))
                total_exposure += exposure

                if exposure > 50000:  # 超过5万的高敞口项
                    high_exposure_items.append({
                        'material_code': mat.material_code,
                        'exposure_amount': str(round(exposure, 2)),
                        'net_quantity': net_need,
                    })
        except Material.DoesNotExist:
            continue

    # 在途PO金额
    po_exposure = PurchaseOrder.objects.filter(
        status__in=PO_ACTIVE_STATUSES,
    ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

    return {
        'total_procurement_exposure': str(round(total_exposure, 2)),
        'in_transit_po_value': str(round(po_exposure, 2)),
        'combined_exposure': str(round(total_exposure + po_exposure, 2)),
        'high_exposure_items': high_exposure_items[:5],
        'exposed_material_count': len(exposed_materials),
    }


def _compute_trend_indicators(today: date, week_ago: date) -> dict:
    """计算各维度趋势指标（对比上周）"""
    indicators = {}

    # 1. 新增订单趋势
    this_week_orders = SalesOrder.objects.filter(
        created_at__date__gte=week_ago,
        created_at__date__lt=today,
    ).count()
    last_week_orders = SalesOrder.objects.filter(
        created_at__date__gte=week_ago - timedelta(days=7),
        created_at__date__lt=week_ago,
    ).count()
    order_change = this_week_orders - last_week_orders
    indicators['new_order_trend'] = {
        'direction': 'up' if order_change > 0 else ('down' if order_change < 0 else 'stable'),
        'change_value': order_change,
        'change_pct': round(order_change / max(last_week_orders, 1) * 100, 1) if last_week_orders > 0 else 0,
        'this_week': this_week_orders,
        'last_week': last_week_orders,
    }

    # 2. 逾期承诺趋势
    this_week_overdue = SupplierCommitment.objects.filter(
        delivery_date__lt=today,
        delivery_date__gte=week_ago,
    ).count()
    last_week_overdue = SupplierCommitment.objects.filter(
        delivery_date__lt=week_ago,
        delivery_date__gte=week_ago - timedelta(days=7),
    ).count()
    overdue_change = this_week_overdue - last_week_overdue
    indicators['overdue_commitment_trend'] = {
        'direction': 'up' if overdue_change > 0 else ('down' if overdue_change < 0 else 'stable'),
        'change_value': overdue_change,
        'this_week': this_week_overdue,
        'last_week': last_week_overdue,
    }

    # 3. Hold库存趋势
    this_week_holds = Inventory.objects.filter(
        is_hold=True,
        updated_at__date__gte=week_ago,
        updated_at__date__lt=today,
    ).count()
    last_week_holds = Inventory.objects.filter(
        is_hold=True,
        updated_at__date__gte=week_ago - timedelta(days=7),
        updated_at__date__lt=week_ago,
    ).count()
    hold_change = this_week_holds - last_week_holds
    indicators['hold_inventory_trend'] = {
        'direction': 'up' if hold_change > 0 else ('down' if hold_change < 0 else 'stable'),
        'change_value': hold_change,
        'this_week': this_week_holds,
        'last_week': last_week_holds,
    }

    # 4. 采购PO创建趋势
    this_week_pos = PurchaseOrder.objects.filter(
        created_at__date__gte=week_ago,
        created_at__date__lt=today,
    ).count()
    last_week_pos = PurchaseOrder.objects.filter(
        created_at__date__gte=week_ago - timedelta(days=7),
        created_at__date__lt=week_ago,
    ).count()
    po_change = this_week_pos - last_week_pos
    indicators['po_creation_trend'] = {
        'direction': 'up' if po_change > 0 else ('down' if po_change < 0 else 'stable'),
        'change_value': po_change,
        'this_week': this_week_pos,
        'last_week': last_week_pos,
    }

    return indicators


def _risk_level_label(score: int) -> str:
    """将数值风险评分转换为文字等级"""
    if score >= 80:
        return '极高危'
    elif score >= 60:
        return '高危'
    elif score >= 40:
        return '中危'
    elif score >= 20:
        return '低危'
    return '安全'


# ========== API: 一键采购 ==========


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def procurement_one_click_purchase(request):
    """
    一键生成采购订单API - 基于智能推荐批量创建采购订单

    接收结构化的采购推荐列表，自动完成校验、风险评估和PO创建。

    POST Body:
        recommendations: [
            {
                "material_id": int,          # 物料ID
                "supplier_id": int,           # 选定供应商ID
                "quantity": int,              # 采购数量
                "expected_date": str(YYYY-MM-DD)  # 期望交付日期（可选）
            },
            ...
        ]
        auto_create_po: bool                 # 是否自动创建PurchaseOrder记录（默认true）
    Returns:
        created_purchase_orders: 创建成功的PO列表
        total_cost: 总成本
        validation_warnings: 校验警告列表
        risk_confirmation: 需人工确认的风险项
    """
    data = request.data
    recommendations = data.get('recommendations', [])
    auto_create_po = data.get('auto_create_po', True)

    if not isinstance(recommendations, list) or len(recommendations) == 0:
        return Response({
            'success': False,
            'error': 'recommendations不能为空，需提供采购推荐列表',
        }, status=status.HTTP_400_BAD_REQUEST)

    today = date.today()
    created_purchase_orders = []
    validation_warnings = []
    risk_confirmations = []
    total_cost = Decimal('0')

    for idx, rec in enumerate(recommendations):
        material_id = rec.get('material_id')
        supplier_id = rec.get('supplier_id')
        quantity = rec.get('quantity')
        expected_date_str = rec.get('expected_date')

        # ---- 字段校验 ----
        item_warnings = []

        if not material_id:
            item_warnings.append(f'第{idx + 1}项: 缺少material_id')
        if not supplier_id:
            item_warnings.append(f'第{idx + 1}项: 缺少supplier_id')
        if not quantity or int(quantity) <= 0:
            item_warnings.append(f'第{idx + 1}项: quantity必须为正整数')

        if item_warnings:
            validation_warnings.extend(item_warnings)
            continue

        quantity = int(quantity)
        material_id = int(material_id)
        supplier_id = int(supplier_id)

        # ---- 校验物料是否存在 ----
        try:
            material = Material.objects.get(id=material_id)
        except Material.DoesNotExist:
            validation_warnings.append(f'第{idx + 1}项: 物料ID={material_id}不存在')
            continue

        # ---- 校验供应商是否存在且可供货 ----
        try:
            supplier = Supplier.objects.get(id=supplier_id)
        except Supplier.DoesNotExist:
            validation_warnings.append(f'第{idx + 1}项: 供应商ID={supplier_id}不存在')
            continue

        # 校验供应商物料关系
        sm = SupplierMaterial.objects.filter(
            supplier_id=supplier_id,
            material_id=material_id,
            is_forbidden=False,
        ).first()
        if not sm:
            validation_warnings.append(
                f'第{idx + 1}项: 供应商[{supplier.supplier_name}]未注册物料[{material.material_code}]或已被禁用'
            )
            continue

        # ---- MOQ校验 ----
        moq = sm.min_order_qty or material.min_order_qty or 1
        if quantity < moq:
            validation_warnings.append(
                f'第{idx + 1}项: 数量{quantity}低于最小起订量(MOQ={moq})'
            )
            # 不阻断，继续处理但记录警告

        # ---- 期望交期校验 ----
        expected_date = None
        if expected_date_str:
            try:
                expected_date = date.fromisoformat(expected_date_str)
                lead_time = sm.lead_time or supplier.normal_lead_time or 7
                earliest_possible = today + timedelta(days=lead_time)
                if expected_date < earliest_possible:
                    validation_warnings.append(
                        f'第{idx + 1}项: 期望交期{expected_date_str}早于供应商最快交期{earliest_possible}'
                    )
                    risk_confirmations.append({
                        'item_index': idx + 1,
                        'material_code': material.material_code,
                        'risk_type': 'delivery_date_unrealistic',
                        'message': f'期望交期{expected_date_str}可能无法满足，供应商交期为{lead_time}天',
                    })
            except ValueError:
                validation_warnings.append(f'第{idx + 1}项: expected_date格式错误，应为YYYY-MM-DD')

        # ---- 风险评估 ----
        # 供应商评级风险
        if supplier.rating in ('C', 'D'):
            risk_confirmations.append({
                'item_index': idx + 1,
                'material_code': material.material_code,
                'risk_type': 'low_supplier_rating',
                'message': f'供应商[{supplier.supplier_name}]评级为{supplier.rating}级，存在交付风险',
            })

        # 单一供应源风险
        alt_count = SupplierMaterial.objects.filter(
            material_id=material_id,
            is_forbidden=False,
        ).exclude(supplier_id=supplier_id).count()
        if alt_count == 0:
            risk_confirmations.append({
                'item_index': idx + 1,
                'material_code': material.material_code,
                'risk_type': 'single_source',
                'message': f'物料[{material.material_code}]仅有此一个可用供应商，无备选方案',
            })

        # 交付可靠率风险
        if supplier.delivery_reliability and supplier.delivery_reliability < 0.8:
            risk_confirmations.append({
                'item_index': idx + 1,
                'material_code': material.material_code,
                'risk_type': 'low_reliability',
                'message': f'供应商准时交付率仅{supplier.delivery_reliability*100:.0f}%，低于安全阈值80%',
            })

        # ---- 计算成本 ----
        unit_price = sm.unit_price or Decimal('0')
        line_total = Decimal(str(quantity)) * unit_price
        total_cost += line_total

        # ---- 创建PO（如果启用）----
        po_record = None
        if auto_create_po:
            import uuid
            po_no = f'PO-{today.strftime("%Y%m%d")}-{uuid.uuid4().hex[:8].upper()}'

            if expected_date is None:
                lead_time = sm.lead_time or supplier.normal_lead_time or 7
                expected_date = today + timedelta(days=lead_time)

            po_record = PurchaseOrder.objects.create(
                po_no=po_no,
                supplier=supplier,
                material=material,
                quantity=quantity,
                unit_price=unit_price,
                order_date=today,
                delivery_date=expected_date,
                status='draft',
                remarks=f'一键采购自动生成 | 来源: 智能推荐系统',
            )

            logger.info(
                f'一键采购创建PO: {po_no}, 物料={material.material_code}, '
                f'供应商={supplier.supplier_name}, 数量={quantity}, 金额={line_total}'
            )

            created_purchase_orders.append({
                'po_no': po_no,
                'material_code': material.material_code,
                'material_name': material.material_name,
                'supplier_name': supplier.supplier_name,
                'quantity': quantity,
                'unit_price': str(round(float(unit_price), 2)),
                'total_amount': str(round(float(line_total), 2)),
                'expected_delivery': str(expected_date),
                'status': 'draft',
            })
        else:
            # 仅模拟返回（不实际创建）
            created_purchase_orders.append({
                'po_no': '(预览模式-未创建)',
                'material_code': material.material_code,
                'material_name': material.material_name,
                'supplier_name': supplier.supplier_name,
                'quantity': quantity,
                'unit_price': str(round(float(unit_price), 2)),
                'total_amount': str(round(float(line_total), 2)),
                'expected_delivery': str(expected_date) if expected_date else '(待定)',
                'status': 'preview',
            })

    # 汇总响应
    response_data = {
        'success': True,
        'processed_at': today.isoformat(),
        'total_submitted': len(recommendations),
        'created_count': len(created_purchase_orders),
        'skipped_count': len(validation_warnings),
        'created_purchase_orders': created_purchase_orders,
        'total_cost': str(round(total_cost, 2)),
        'validation_warnings': validation_warnings,
        'risk_confirmation': risk_confirmations,
        'summary': {
            'has_warnings': len(validation_warnings) > 0,
            'has_risks': len(risk_confirmations) > 0,
            'warning_count': len(validation_warnings),
            'risk_count': len(risk_confirmations),
            'auto_create_po': auto_create_po,
        },
    }

    # 如果有校验错误导致部分跳过，仍返回200但标记警告
    if validation_warnings and not created_purchase_orders:
        response_data['success'] = False
        return Response(response_data, status=status.HTTP_207_MULTI_STATUS)

    return Response(response_data, status=status.HTTP_200_OK)
