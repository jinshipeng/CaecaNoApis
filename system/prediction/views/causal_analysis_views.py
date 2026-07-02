"""
单订单因果分析 + 决策追溯视图

提供每个订单不齐套的原因分析：
- 是否因禁用料导致缺料？
- 是否因物料Hold导致无法分配？
- 是否因供应商延期导致？
- 是否因产能不足导致？
- 物料为什么分配给了订单A而不是订单B？（决策追溯）
"""

import logging
from datetime import date, timedelta
from django.db.models import Sum, Q, F, Case, When, Value, IntegerField
from django.db.models.functions import Coalesce
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)

from ..models import (
    SalesOrder, Material, BillOfMaterials, Inventory, OrderAllocation,
    MaterialPlanResult, SupplierCommitment, SupplierMaterial, PlanLog,
    FactoryTransfer
)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_causal_analysis(request):
    """
    单订单因果分析API

    GET参数:
        order_id: 订单ID（必填）

    返回:
        - 根因分类（禁用/Hold/缺货/供应商延期/产能）
        - 每种原因的影响程度
        - 建议措施
        - 决策追溯链（物料分配给该订单 vs 其他订单的对比）
    """
    order_id = request.query_params.get('order_id')
    if not order_id:
        return Response({'success': False, 'error': '请提供order_id参数'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        order_id = int(order_id)
    except (ValueError, TypeError):
        return Response({'success': False, 'error': 'order_id必须为数字'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        order = SalesOrder.objects.select_related('material').get(id=order_id)
    except SalesOrder.DoesNotExist:
        return Response({'success': False, 'error': f'订单ID {order_id} 不存在'}, status=status.HTTP_400_BAD_REQUEST)

    # 1. 获取计划结果
    try:
        plan_result = MaterialPlanResult.objects.select_related('order').get(order_id=order_id)
    except MaterialPlanResult.DoesNotExist:
        plan_result = None

    # 2. 分析根因
    root_causes = _analyze_root_causes(order, plan_result)

    # 3. 决策追溯 - 为什么物料分配给了别人
    decision_trace = _build_decision_trace(order)

    # 4. 时间线重建
    timeline = _rebuild_order_timeline(order)

    return Response({
        'success': True,
        'order_info': {
            'order_no': order.order_no,
            'material_code': order.material.material_code if order.material else '',
            'quantity': order.quantity,
            'demand_date': str(order.demand_date) if order.demand_date else '',
            'priority': order.priority,
            'status': order.status,
            'factory_code': order.factory_code or '',
            'complete_rate': plan_result.complete_rate if plan_result else 0,
            'is_complete': plan_result.is_complete if plan_result else False,
        },
        'root_cause_analysis': root_causes,
        'decision_trace': decision_trace,
        'timeline': timeline,
        'recommendations': _generate_recommendations(root_causes, order),
    })


def _analyze_root_causes(order, plan_result):
    """分析订单不齐套的根本原因"""
    causes = {
        'forbidden_material': {'severity': 'none', 'affected_materials': [], 'description': ''},
        'hold_material': {'severity': 'none', 'affected_materials': [], 'description': ''},
        'insufficient_stock': {'severity': 'none', 'affected_materials': [], 'description': ''},
        'supplier_delay': {'severity': 'none', 'affected_materials': [], 'description': ''},
        'capacity_constraint': {'severity': 'none', 'description': ''},
        'bom_missing': {'severity': 'none', 'affected_materials': [], 'description': ''},
    }

    material_id = order.material_id
    shortage_details = {}

    if plan_result and plan_result.shortage_details:
        import json
        try:
            shortage_details = plan_result.shortage_details
            if isinstance(shortage_details, str):
                shortage_details = json.loads(shortage_details)
        except (json.JSONDecodeError, TypeError):
            shortage_details = {}

    # 检查每种缺料物料的根因
    if isinstance(shortage_details, list):
        for item in shortage_details:
            mat_id = item.get('material_id')
            shortage_qty = item.get('shortage', 0)

            # 原因A: 禁用料
            forbidden = SupplierMaterial.objects.filter(
                material_id=mat_id, is_forbidden=True
            ).select_related('material', 'supplier')

            if forbidden.exists():
                causes['forbidden_material']['severity'] = 'high'
                causes['forbidden_material']['affected_materials'].append({
                    'material_id': mat_id,
                    'shortage_qty': shortage_qty,
                    'forbidden_suppliers': [
                        {'supplier': fs.supplier.supplier_name, 'reason': fs.forbidden_reason}
                        for fs in forbidden[:3]
                    ]
                })
                continue

            # 原因B: Hold物料
            held_inv = Inventory.objects.filter(
                material_id=mat_id, is_hold=True, quantity__gt=0
            )

            total_held = sum(int(i.quantity or 0) for i in held_inv)
            available = Inventory.objects.filter(
                material_id=mat_id, is_hold=False
            ).aggregate(total=Sum('quantity'))['total'] or 0

            if total_held > 0 and available < shortage_qty:
                severity = 'high' if total_held >= shortage_qty else 'medium'
                causes['hold_material']['severity'] = severity
                causes['hold_material']['affected_materials'].append({
                    'material_id': mat_id,
                    'held_quantity': total_held,
                    'available_quantity': available,
                    'shortage_qty': shortage_qty,
                })
                continue

            # 原因C: 库存不足
            total_stock = Inventory.objects.filter(material_id=mat_id).aggregate(total=Sum('quantity'))['total'] or 0
            if total_stock < shortage_qty:
                causes['insufficient_stock']['severity'] = 'medium'
                causes['insufficient_stock']['affected_materials'].append({
                    'material_id': mat_id,
                    'total_stock': total_stock,
                    'shortage_qty': shortage_qty,
                    'gap': shortage_qty - total_stock,
                })
                continue

            # 原因D: 供应商承诺延期
            commitments = SupplierCommitment.objects.filter(
                material_id=mat_id, delivery_date__gt=date.today()
            ).order_by('delivery_date')

            if commitments.exists():
                earliest_commit = commitments.first()
                days_to_delivery = (earliest_commit.delivery_date - date.today()).days

                # 计算需求时间点
                shipping_days = getattr(order, 'shipping_days', 45) or 45
                production_lt = 2
                needed_by = order.demand_date - timedelta(days=shipping_days + production_lt) if order.demand_date else date.today()

                if earliest_commit.delivery_date > needed_by:
                    delay_days = (earliest_commit.delivery_date - needed_by).days
                    causes['supplier_delay']['severity'] = 'high' if delay_days > 14 else ('medium' if delay_days > 7 else 'low')
                    causes['supplier_delay']['affected_materials'].append({
                        'material_id': mat_id,
                        'earliest_commitment_date': str(earliest_commit.delivery_date),
                        'needed_by_date': str(needed_by),
                        'delay_days': delay_days,
                        'supplier': earliest_commit.supplier.supplier_name if hasattr(earliest_commit, 'supplier') else '',
                    })

    # 原因E: BOM缺失
    bom_items = BillOfMaterials.objects.filter(parent_material_id=material_id, is_active=True)
    if not bom_items.exists() and material_id:
        causes['bom_missing']['severity'] = 'high'
        causes['bom_missing']['description'] = f'物料 {material_id} 未找到有效的BOM结构'

    # 设置描述
    for cause_key, cause_data in causes.items():
        affected_count = len(cause_data.get('affected_materials', []))
        if affected_count > 0:
            descriptions = {
                'forbidden_material': f'{affected_count} 个物料被供应商禁用',
                'hold_material': f'{affected_count} 个物料处于Hold状态，共 {sum(m["held_quantity"] for m in cause_data["affected_materials"])} 数量被冻结',
                'insufficient_stock': f'{affected_count} 个物料库存不足',
                'supplier_delay': f'{affected_count} 个物料的供应商承诺交期晚于需求时间',
                'bom_missing': cause_data.get('description', ''),
            }
            cause_data['description'] = descriptions.get(cause_key, '')

    # 清理空原因
    return {k: v for k, v in causes.items() if v['severity'] != 'none' or v.get('affected_materials') or v.get('description')}


def _build_decision_trace(order):
    """
    决策追溯 - 回答"为什么物料X没有分配给这个订单"

    对比该订单与其他订单的分配情况
    """
    material_id = order.material_id

    # 获取该订单的分配记录
    my_allocations = list(OrderAllocation.objects.filter(order_id=order.id).select_related('material'))
    allocated_mat_ids = set(a.material_id for a in my_allocations)

    # 获取同物料的其他订单分配情况
    competing_allocations = OrderAllocation.objects.filter(
        material_id__in=[material_id],
    ).exclude(order_id=order.id).select_related('order', 'material').order_by('order__priority', 'order__demand_date')[:20]

    trace_entries = []
    seen_orders = set()

    for alloc in competing_allocations:
        if alloc.order_id in seen_orders:
            continue
        seen_orders.add(alloc.order_id)

        other_order = alloc.order
        priority_comparison = '更高' if other_order.priority < order.priority else ('更低' if other_order.priority > order.priority else '相同')
        date_comparison = ''
        if other_order.demand_date and order.demand_date:
            if other_order.demand_date < order.demand_date:
                date_comparison = '更早'
            elif other_order.demand_date > order.demand_date:
                date_comparison = '更晚'
            else:
                date_comparison = '相同'

        trace_entries.append({
            'competing_order_no': other_order.order_no,
            'competing_priority': other_order.priority,
            'priority_comparison': priority_comparison,
            'demand_date': str(other_order.demand_date) if other_order.demand_date else '',
            'date_comparison': date_comparison,
            'allocated_quantity': int(alloc.allocated_quantity or 0),
            'is_alternative': alloc.is_alternative,
            'reason': f'优先级{priority_comparison}' + (f"，交期{date_comparison}" if date_comparison else ''),
        })

    # 调拨记录
    transfers = FactoryTransfer.objects.filter(related_order_id=order.id).select_related('material')
    transfer_info = [{
        'transfer_no': t.transfer_no,
        'material_code': t.material.material_code if t.material else '',
        'quantity': t.quantity,
        'from_factory': t.from_factory,
        'to_factory': t.to_factory,
        'status': t.status,
    } for t in transfers]

    return {
        'my_allocations': [{'material_id': a.material_id, 'qty': a.allocated_quantity, 'is_alt': a.is_alternative} for a in my_allocations],
        'competing_orders': trace_entries,
        'transfers': transfer_info,
        'summary': {
            'total_competing_orders': len(trace_entries),
            'higher_priority_competitors': sum(1 for t in trace_entries if t['priority_comparison'] == '更高'),
            'earlier_deadline_competitors': sum(1 for t in trace_entries if t['date_comparison'] == '更早'),
        }
    }


def _rebuild_order_timeline(order):
    """重建订单的时间线"""
    events = []

    # 订单创建
    if order.created_at:
        events.append({
            'event': '订单创建',
            'timestamp': order.created_at.isoformat(),
            'type': 'info',
        })

    # 下单日期
    if order.order_date:
        events.append({
            'event': f'下单 (数量: {order.quantity})',
            'timestamp': f"{order.order_date}T00:00:00",
            'type': 'info',
        })

    # 需求交付日
    if order.demand_date:
        shipping_days = getattr(order, 'shipping_days', 45) or 45
        production_lt = 2
        material_needed_by = order.demand_date - timedelta(days=shipping_days + production_lt)
        events.append({
            'event': f'需求交付日 (需在 {material_needed_by} 前齐套物料)',
            'timestamp': f"{order.demand_date}T23:59:59",
            'type': 'deadline',
        })
        events.append({
            'event': f'物料最晚到货日 (倒推运输{shipping_days}天+生产{production_lt}天)',
            'timestamp': f"{material_needed_by}T00:00:00",
            'type': 'milestone',
        })

    # 计划日志
    logs = PlanLog.objects.filter(order_id=order.id).order_by('created_at')[:20]
    for log in logs:
        event_type = 'warning' if log.log_type in ('WARNING', 'ERROR') else ('success' if log.log_type == 'PLANNING' else 'info')
        events.append({
            'event': log.message[:100],
            'timestamp': log.created_at.isoformat(),
            'type': event_type,
        })

    # 分配记录时间
    allocations = OrderAllocation.objects.filter(order_id=order.id).order_by('created_at')
    for alloc in allocations[:10]:
        events.append({
            'event': f'分配物料 ID={alloc.material_id}, 数量={alloc.allocated_quantity}',
            'timestamp': alloc.created_at.isoformat(),
            'type': 'allocation',
        })

    # 按时间排序
    events.sort(key=lambda x: x['timestamp'])
    return events


def _generate_recommendations(root_causes, order):
    """基于根因生成建议"""
    recommendations = []

    if 'forbidden_material' in root_causes and root_causes['forbidden_material']['severity'] != 'none':
        recommendations.append({
            'priority': '紧急',
            'category': '禁用料处理',
            'action': '联系供应商解除禁用或启用替代供应商',
            'affected_count': len(root_causes['forbidden_material'].get('affected_materials', [])),
        })

    if 'hold_material' in root_causes and root_causes['hold_material']['severity'] != 'none':
        rec = {
            'priority': '高',
            'category': 'Hold物料释放',
            'action': '评估Hold物料是否可以提前释放',
            'affected_count': len(root_causes['hold_material'].get('affected_materials', [])),
        }
        held_mats = root_causes['hold_material'].get('affected_materials', [])
        if held_mats:
            soonest_release = min(
                (m.get('held_until') for m in held_mats if m.get('held_until')),
                default=None
            )
            if soonest_release:
                rec['earliest_release'] = str(soonest_release)
        recommendations.append(rec)

    if 'insufficient_stock' in root_causes and root_causes['insufficient_stock']['severity'] != 'none':
        recommendations.append({
            'priority': '高',
            'category': '补库/调拨',
            'action': '启动紧急采购或跨工厂调拨',
            'affected_count': len(root_causes['insufficient_stock'].get('affected_materials', [])),
        })

    if 'supplier_delay' in root_causes and root_causes['supplier_delay']['severity'] != 'none':
        severity = root_causes['supplier_delay']['severity']
        urgency = '紧急' if severity == 'high' else ('高' if severity == 'medium' else '中')
        recommendations.append({
            'priority': urgency,
            'category': '供应商催交',
            'action': '联系供应商加急生产或切换物流方式（海运→空运）',
            'affected_count': len(root_causes['supplier_delay'].get('affected_materials', [])),
        })

    if 'bom_missing' in root_causes and root_causes['bom_missing']['severity'] != 'none':
        recommendations.append({
            'priority': '紧急',
            'category': 'BOM数据',
            'action': '维护产品的BOM结构数据',
        })

    if not recommendations:
        recommendations.append({
            'priority': '低',
            'category': '正常',
            'action': '订单状态正常，无需特殊处理',
        })

    return sorted(recommendations, key=lambda x: {'紧急': 0, '高': 1, '中': 2, '低': 3}.get(x['priority'], 4))


# ============================================================
# 归因类型枚举（8类）及证据收集逻辑
# ============================================================

class CausalType:
    """归因类型枚举 - 覆盖缺料事件的8大根因类别"""
    SUPPLIER_DELAY = 'supplier_delay'           # 供应商延期
    QUALITY_HOLD = 'quality_hold'               # 质量冻结
    BOM_CHANGE = 'bom_change'                   # BOM/ECN变更
    DEMAND_SURGE = 'demand_surge'               # 需求激增
    CAPACITY_CONSTRAINT = 'capacity_constraint' # 产能不足
    INVENTORY_MISALLOCATION = 'inventory_misallocation'  # 库存分配不当
    LOGISTICS_DELAY = 'logistics_delay'         # 物流延迟
    FORECAST_ERROR = 'forecast_error'           # 预测偏差

    # 类型显示名称映射
    DISPLAY_NAMES = {
        SUPPLIER_DELAY: '供应商延期',
        QUALITY_HOLD: '质量冻结',
        BOM_CHANGE: 'BOM/ECN变更',
        DEMAND_SURGE: '需求激增',
        CAPACITY_CONSTRAINT: '产能不足',
        INVENTORY_MISALLOCATION: '库存分配不当',
        LOGISTICS_DELAY: '物流延迟',
        FORECAST_ERROR: '预测偏差',
    }

    ALL_TYPES = [
        SUPPLIER_DELAY, QUALITY_HOLD, BOM_CHANGE,
        DEMAND_SURGE, CAPACITY_CONSTRAINT, INVENTORY_MISALLOCATION,
        LOGISTICS_DELAY, FORECAST_ERROR,
    ]


def _collect_evidence(causal_type: str, material_id: int, order_id: int) -> dict:
    """
    根据归因类型收集对应的证据数据

    每种归因类型对应不同的数据源和查询逻辑:
    - supplier_delay: 查询供应商承诺记录、交期偏差
    - quality_hold: 查询库存Hold记录、质检异常
    - bom_change: 查询BOM版本变更历史
    - demand_surge: 查询订单数量变化趋势
    - capacity_constraint: 查询产线产能利用率
    - inventory_misallocation: 查询分配记录对比
    - logistics_delay: 查询调拨/在途记录
    - forecast_error: 对比预测与实际需求
    """
    evidence = {'causal_type': causal_type, 'material_id': material_id, 'order_id': order_id}

    try:
        if causal_type == CausalType.SUPPLIER_DELAY:
            _evidence_supplier_delay(evidence, material_id)

        elif causal_type == CausalType.QUALITY_HOLD:
            _evidence_quality_hold(evidence, material_id)

        elif causal_type == CausalType.BOM_CHANGE:
            _evidence_bom_change(evidence, material_id)

        elif causal_type == CausalType.DEMAND_SURGE:
            _evidence_demand_surge(evidence, order_id)

        elif causal_type == CausalType.CAPACITY_CONSTRAINT:
            _evidence_capacity_constraint(evidence)

        elif causal_type == CausalType.INVENTORY_MISALLOCATION:
            _evidence_inventory_misallocation(evidence, material_id, order_id)

        elif causal_type == CausalType.LOGISTICS_DELAY:
            _evidence_logistics_delay(evidence, material_id)

        elif causal_type == CausalType.FORECAST_ERROR:
            _evidence_forecast_error(evidence, material_id, order_id)

    except Exception as e:
        logger.warning(f"收集归因证据失败 type={causal_type}, mat={material_id}: {str(e)}")
        evidence['collection_error'] = str(e)

    return evidence


def _evidence_supplier_delay(evidence: dict, material_id: int):
    """供应商延期证据：查询供应商承诺交期与实际到货的偏差"""
    from datetime import date

    commitments = SupplierCommitment.objects.filter(
        material_id=material_id
    ).select_related('supplier', 'material').order_by('-delivery_date')[:10]

    commitment_list = []
    for c in commitments:
        days_late = None
        if c.delivery_date and c.delivery_date < date.today():
            days_late = (date.today() - c.delivery_date).days
        commitment_list.append({
            'commitment_id': c.id,
            'supplier_name': c.supplier.supplier_name if hasattr(c, 'supplier') and c.supplier else '',
            'committed_delivery': str(c.delivery_date) if c.delivery_date else '',
            'quantity': int(c.quantity or 0),
            'days_late': days_late,
            'status': 'overdue' if days_late and days_late > 0 else 'pending',
        })

    avg_delay = sum(c['days_late'] for c in commitment_list if c.get('days_late') and c['days_late'] > 0)
    avg_delay = round(avg_delay / max(sum(1 for c in commitment_list if c.get('days_late', 0) > 0), 1), 1)

    evidence.update({
        'data_source': 'SupplierCommitment',
        'commitments_count': len(commitment_list),
        'overdue_commitments': sum(1 for c in commitment_list if c.get('status') == 'overdue'),
        'average_delay_days': avg_delay,
        'details': commitment_list[:5],
        'severity': 'high' if avg_delay > 14 else ('medium' if avg_delay > 7 else 'low'),
    })


def _evidence_quality_hold(evidence: dict, material_id: int):
    """质量冻结证据：查询被Hold的库存记录"""
    held_records = Inventory.objects.filter(
        material_id=material_id, is_hold=True, quantity__gt=0
    ).select_related('material')[:10]

    total_held = sum(int(r.quantity or 0) for r in held_records)
    available_qty = Inventory.objects.filter(
        material_id=material_id, is_hold=False
    ).aggregate(total=Sum('quantity'))['total'] or 0

    hold_details = [{
        'inventory_id': r.id,
        'location': getattr(r, 'location', ''),
        'held_quantity': int(r.quantity or 0),
        'hold_reason': getattr(r, 'hold_reason', '质量异常'),
    } for r in held_records]

    evidence.update({
        'data_source': 'Inventory (is_hold=True)',
        'total_held_quantity': total_held,
        'available_quantity': available_qty,
        'hold_record_count': len(hold_details),
        'hold_ratio': round(total_held / max(total_held + available_qty, 1), 3),
        'details': hold_details,
        'severity': 'high' if total_held > available_qty else ('medium' if total_held > 0 else 'low'),
    })


def _evidence_bom_change(evidence: dict, material_id: int):
    """BOM/ECN变更证据：查询BOM结构变化"""
    from django.utils import timezone

    # 查询该物料作为子件的BOM关系
    bom_as_child = BillOfMaterials.objects.filter(
        child_material_id=material_id, is_active=True
    ).select_related('parent_material', 'child_material')

    # 查询该物料作为父件的BOM关系（其子件可能发生变化）
    bom_as_parent = BillOfMaterials.objects.filter(
        parent_material_id=material_id, is_active=True
    ).select_related('parent_material', 'child_material')

    recent_changes = []
    # 检查PlanLog中的ECN相关日志
    ecn_logs = PlanLog.objects.filter(
        message__icontains='ECN'
    ).order_by('-created_at')[:5]

    for log in ecn_logs:
        recent_changes.append({
            'log_id': log.id,
            'message': log.message[:200],
            'created_at': log.created_at.isoformat() if log.created_at else '',
        })

    evidence.update({
        'data_source': 'BillOfMaterials + PlanLog',
        'bom_as_child_count': bom_as_child.count(),
        'bom_as_parent_count': bom_as_parent.count(),
        'recent_ecn_logs': len(recent_changes),
        'affected_parent_materials': [
            {
                'parent_code': b.parent_material.material_code if b.parent_material else '',
                'usage_qty': float(b.quantity or 0),
            } for b in bom_as_parent[:5]
        ],
        'recent_changes': recent_changes,
        'severity': 'high' if len(recent_changes) > 3 else ('medium' if len(recent_changes) > 0 else 'low'),
    })


def _evidence_demand_surge(evidence: dict, order_id: int):
    """需求激增证据：分析订单需求量的异常增长"""
    from datetime import date, timedelta
    from django.db.models import Avg, Count

    target_order = SalesOrder.objects.filter(id=order_id).first()
    if not target_order:
        evidence.update({'error': f'订单 {order_id} 不存在'})
        return

    material_id = target_order.material_id
    target_qty = float(target_order.quantity or 0)

    # 同物料近期订单的均量对比
    thirty_days_ago = date.today() - timedelta(days=30)
    recent_orders = SalesOrder.objects.filter(
        material_id=material_id,
        order_date__gte=thirty_days_ago,
    ).exclude(id=order_id)

    agg = recent_orders.aggregate(
        avg_qty=Avg('quantity'),
        count=Count('id'),
        max_qty=Sum('quantity'),
    )
    avg_qty = float(agg['avg_qty'] or 0)
    surge_ratio = round(target_qty / max(avg_qty, 1), 2)

    evidence.update({
        'data_source': 'SalesOrder (同物料30天)',
        'target_order_quantity': target_qty,
        'historical_avg_quantity': round(avg_qty, 1),
        'historical_order_count': agg['count'],
        'surge_ratio': surge_ratio,
        'is_abnormal': surge_ratio > 1.5,
        'severity': 'high' if surge_ratio > 2.0 else ('medium' if surge_ratio > 1.5 else 'low'),
    })


def _evidence_capacity_constraint(evidence: dict):
    """产能不足证据：查询产线利用率"""
    from ..models import WorkCenter

    work_centers = WorkCenter.objects.all()[:10]
    wc_data = []
    overloaded_count = 0

    for wc in work_centers:
        daily_cap = float(wc.daily_capacity_limit or 0)
        utilization = round(daily_cap / max(daily_cap + 1, 1), 2) if daily_cap > 0 else 0.0
        is_overloaded = utilization > 0.9
        if is_overloaded:
            overloaded_count += 1
        wc_data.append({
            'work_center_code': wc.work_center_code,
            'daily_capacity': daily_cap,
            'utilization_rate': utilization,
            'overloaded': is_overloaded,
        })

    evidence.update({
        'data_source': 'WorkCenter',
        'total_work_centers': len(wc_data),
        'overloaded_work_centers': overloaded_count,
        'overall_utilization': round(sum(w['utilization_rate'] for w in wc_data) / max(len(wc_data), 1), 3),
        'details': wc_data[:5],
        'severity': 'high' if overloaded_count > len(wc_data) * 0.3 else ('medium' if overloaded_count > 0 else 'low'),
    })


def _evidence_inventory_misallocation(evidence: dict, material_id: int, order_id: int):
    """库存分配不当证据：对比该订单与竞争订单的分配情况"""
    my_allocs = OrderAllocation.objects.filter(
        order_id=order_id, material_id=material_id
    )
    my_total = sum(int(a.allocated_quantity or 0) for a in my_allocs)

    # 同物料其他订单的分配总量
    other_allocs = OrderAllocation.objects.filter(
        material_id=material_id,
    ).exclude(order_id=order_id).select_related('order')
    other_total = sum(int(a.allocated_quantity or 0) for a in other_allocs)

    # 总可用库存
    total_stock = Inventory.objects.filter(material_id=material_id).aggregate(t=Sum('quantity'))['t'] or 0
    unallocated = total_stock - my_total - other_total

    allocation_details = [{
        'order_no': a.order.order_no if a.order else '',
        'allocated_qty': int(a.allocated_quantity or 0),
        'priority': a.order.priority if a.order else 99,
        'demand_date': str(a.order.demand_date) if a.order and a.order.demand_date else '',
    } for a in other_allocs.select_related('order')[:10]]

    allocation_details.sort(key=lambda x: x['priority'])

    evidence.update({
        'data_source': 'OrderAllocation + Inventory',
        'my_allocated_quantity': my_total,
        'others_allocated_quantity': other_total,
        'total_available_stock': total_stock,
        'unallocated_quantity': unallocated,
        'allocation_fairness_score': round(my_total / max(other_total, 1), 3) if other_total > 0 else 1.0,
        'competing_allocations': allocation_details[:5],
        'severity': 'high' if my_total == 0 and other_total > 0 else ('medium' if my_total < other_total * 0.3 else 'low'),
    })


def _evidence_logistics_delay(evidence: dict, material_id: int):
    """物流延迟证据：查询调拨和在途记录"""
    transfers = FactoryTransfer.objects.filter(
        material_id=material_id
    ).select_related('material')[:10]

    pending_transfers = [t for t in transfers if t.status in ['in_transit', 'pending']]
    transfer_details = [{
        'transfer_no': t.transfer_no,
        'quantity': t.quantity,
        'from_factory': t.from_factory,
        'to_factory': t.to_factory,
        'status': t.status,
        'created_at': t.created_at.isoformat() if t.created_at else '',
    } for t in transfers[:5]]

    evidence.update({
        'data_source': 'FactoryTransfer',
        'total_transfers': len(transfers),
        'pending_in_transit': len(pending_transfers),
        'in_transit_quantity': sum(t.quantity for t in pending_transfers),
        'details': transfer_details,
        'severity': 'high' if len(pending_transfers) > 3 else ('medium' if len(pending_transfers) > 0 else 'low'),
    })


def _evidence_forecast_error(evidence: dict, material_id: int, order_id: int):
    """预测偏差证据：对比计划量与实际需求"""
    target_order = SalesOrder.objects.filter(id=order_id).first()
    if not target_order:
        evidence.update({'error': f'订单 {order_id} 不存在'})
        return

    actual_qty = float(target_order.quantity or 0)

    # 查找该物料的计划结果来评估预测准确性
    plan_results = MaterialPlanResult.objects.filter(
        material_id=material_id
    )[:10]

    planned_quantities = []
    for pr in plan_results:
        shortage = pr.shortage_details
        if isinstance(shortage, str):
            import json
            try:
                shortage = json.loads(shortage)
            except Exception:
                shortage = {}
        if isinstance(shortage, list):
            for s in shortage:
                planned_quantities.append(s.get('planned_qty', s.get('shortage', 0)))

    avg_planned = sum(planned_quantities) / max(len(planned_quantities), 1) if planned_quantities else actual_qty
    forecast_deviation = abs(actual_qty - avg_planned) / max(avg_planned, 1)

    evidence.update({
        'data_source': 'MaterialPlanResult + SalesOrder',
        'actual_demand': actual_qty,
        'average_planned_quantity': round(avg_planned, 1),
        'forecast_deviation_rate': round(forecast_deviation, 3),
        'plan_result_count': len(plan_results),
        'severity': 'high' if forecast_deviation > 0.5 else ('medium' if forecast_deviation > 0.2 else 'low'),
    })


# ============================================================
# API: 缺料事件归因链分析
# ============================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def causal_root_chain_analysis(request):
    """
    单个缺料事件的完整归因链分析API

    GET参数:
        material_id: 物料ID（必填）
        order_id: 订单ID（必填）

    返回完整的归因链，包含：
    - event: 缺料事件基本信息
    - root_cause_chain: 分层归因链（direct_cause → contributing_factor → root_cause）
    - root_causes_summary: 按类别汇总
    - mitigation_path: 从根因到解决方案的路径
    """
    material_id = request.query_params.get('material_id')
    order_id = request.query_params.get('order_id')

    if not material_id or not order_id:
        return Response({
            'success': False,
            'error': '请提供 material_id 和 order_id 参数'
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        material_id = int(material_id)
        order_id = int(order_id)
    except (ValueError, TypeError):
        return Response({
            'success': False,
            'error': 'material_id 和 order_id 必须为数字'
        }, status=status.HTTP_400_BAD_REQUEST)

    logger.info(f"[归因链分析] 开始分析 material_id={material_id}, order_id={order_id}")

    try:
        # 1. 构建缺料事件信息
        event_info = _build_shortage_event(material_id, order_id)

        # 2. 构建完整归因链
        root_cause_chain = _build_root_cause_chain(material_id, order_id, event_info)

        # 3. 生成根因汇总
        root_causes_summary = _summarize_root_causes(root_cause_chain)

        # 4. 生成缓解路径
        mitigation_path = _build_mitigation_path(root_cause_chain, event_info)

        result = {
            'success': True,
            'event': event_info,
            'root_cause_chain': root_cause_chain,
            'root_causes_summary': root_causes_summary,
            'mitigation_path': mitigation_path,
            'analyzed_at': __import__('datetime').datetime.now().isoformat(),
        }

        logger.info(f"[归因链分析] 完成 material_id={material_id}, "
                   f"归因链长度={len(root_cause_chain)}, "
                   f"根因数={len(root_causes_summary)}")

        return Response(result)

    except Exception as e:
        logger.error(f"[归因链分析] 异常: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'error': f'归因链分析过程出错: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _build_shortage_event(material_id: int, order_id: int) -> dict:
    """构建缺料事件的基本信息"""
    from datetime import datetime

    # 查询订单信息
    try:
        order = SalesOrder.objects.select_related('material').get(id=order_id)
        order_no = order.order_no
        demand_date = str(order.demand_date) if order.demand_date else ''
        quantity = int(order.quantity or 0)
        priority = order.priority
        status = order.status
        material_code = order.material.material_code if order.material else ''
    except SalesOrder.DoesNotExist:
        order_no = ''
        demand_date = ''
        quantity = 0
        priority = 99
        status = 'unknown'
        material_code = ''

    # 查询缺料详情
    shortage_qty = quantity  # 默认以订单量为缺料量
    detected_at = datetime.now()

    try:
        plan_result = MaterialPlanResult.objects.filter(order_id=order_id).first()
        if plan_result and plan_result.shortage_details:
            import json
            sd = plan_result.shortage_details
            if isinstance(sd, str):
                sd = json.loads(sd)
            if isinstance(sd, list):
                for item in sd:
                    if item.get('material_id') == material_id:
                        shortage_qty = item.get('shortage', shortage_qty)
                        break
        if plan_result and plan_result.updated_at:
            detected_at = plan_result.updated_at
    except Exception:
        pass

    return {
        'material_id': material_id,
        'order_id': order_id,
        'order_no': order_no,
        'material_code': material_code,
        'shortage_qty': shortage_qty,
        'detected_at': detected_at.isoformat() if hasattr(detected_at, 'isoformat') else str(detected_at),
        'demand_date': demand_date,
        'priority': priority,
        'status': status,
    }


def _build_root_cause_chain(material_id: int, order_id: int, event_info: dict) -> list:
    """
    构建完整的归因链

    归因链分层结构：
    Level 1 (direct_cause): 直接原因 - 直接导致缺料的表面因素
    Level 2 (contributing_factor): 促进因素 - 加剧问题的中间因素
    Level 3 (root_cause): 根本原因 - 问题的源头
    """
    chain = []

    # --- 第一层：直接原因检测 ---
    direct_causes = _detect_direct_causes(material_id, order_id, event_info)
    chain.extend(direct_causes)

    # --- 第二层：促进因素检测 ---
    contributing_factors = _detect_contributing_factors(material_id, order_id, event_info, direct_causes)
    chain.extend(contributing_factors)

    # --- 第三层：根本原因识别 ---
    root_causes = _identify_root_causes(material_id, order_id, event_info, direct_causes, contributing_factors)
    chain.extend(root_causes)

    # 按层级排序并添加level编号
    for i, item in enumerate(chain):
        item['level'] = i + 1

    return chain


def _detect_direct_causes(material_id: int, order_id: int, event_info: dict) -> list:
    """检测第一层：直接导致缺料的表面原因"""
    causes = []

    # 1. 检查质量冻结
    held_inv = Inventory.objects.filter(
        material_id=material_id, is_hold=True, quantity__gt=0
    )
    total_held = sum(int(i.quantity or 0) for i in held_inv)
    if total_held > 0:
        evidence = _collect_evidence(CausalType.QUALITY_HOLD, material_id, order_id)
        causes.append({
            'type': 'direct_cause',
            'category': CausalType.QUALITY_HOLD,
            'description': f'物料被Hold冻结，共 {total_held} 数量不可用',
            'evidence': evidence,
            'impact_weight': min(1.0, total_held / max(event_info.get('shortage_qty', 1), 1)),
            'actionable': True,
            'suggested_action': '联系质量部门评估释放Hold物料的可行性',
        })

    # 2. 检查供应商延期
    commitments = SupplierCommitment.objects.filter(
        material_id=material_id, delivery_date__gt=__import__('datetime').date.today()
    ).order_by('delivery_date')
    if commitments.exists():
        earliest = commitments.first()
        try:
            order = SalesOrder.objects.get(id=order_id)
            shipping_days = getattr(order, 'shipping_days', 45) or 45
            needed_by = order.demand_date - __import__('datetime').timedelta(days=shipping_days + 2) if order.demand_date else __import__('datetime').date.today()
            if earliest.delivery_date > needed_by:
                delay_days = (earliest.delivery_date - needed_by).days
                evidence = _collect_evidence(CausalType.SUPPLIER_DELAY, material_id, order_id)
                causes.append({
                    'type': 'direct_cause',
                    'category': CausalType.SUPPLIER_DELAY,
                    'description': f'供应商承诺交期晚于需求时间 {delay_days} 天',
                    'evidence': evidence,
                    'impact_weight': min(1.0, delay_days / 30.0),
                    'actionable': True,
                    'suggested_action': '联系供应商加急生产或切换空运方式',
                })
        except SalesOrder.DoesNotExist:
            pass

    # 3. 检查库存不足
    total_stock = Inventory.objects.filter(material_id=material_id).aggregate(t=Sum('quantity'))['t'] or 0
    shortage_qty = event_info.get('shortage_qty', 0)
    if total_stock < shortage_qty and total_held == 0:
        evidence = _collect_evidence(CausalType.INVENTORY_MISALLOCATION, material_id, order_id)
        causes.append({
            'type': 'direct_cause',
            'category': CausalType.INVENTORY_MISALLOCATION,
            'description': f'当前总库存 {total_stock} 小于缺料量 {shortage_qty}',
            'evidence': evidence,
            'impact_weight': min(1.0, (shortage_qty - total_stock) / max(shortage_qty, 1)),
            'actionable': True,
            'suggested_action': '启动跨工厂调拨或紧急采购流程',
        })

    # 如果没有检测到任何直接原因，给出默认说明
    if not causes:
        causes.append({
            'type': 'direct_cause',
            'category': 'unknown',
            'description': '未能识别明确的直接缺料原因，需人工排查',
            'evidence': {},
            'impact_weight': 0.5,
            'actionable': False,
            'suggested_action': '建议人工检查物料状态和分配记录',
        })

    return causes


def _detect_contributing_factors(material_id: int, order_id: int, event_info: dict, direct_causes: list) -> list:
    """检测第二层：促进因素（加剧问题但非唯一原因的因素）"""
    factors = []

    # 基于已发现的直接原因，进一步查找促进因素
    direct_categories = set(c.get('category', '') for c in direct_causes)

    # 如果已有供应商延期，检查是否同时有物流延迟
    if CausalType.SUPPLIER_DELAY in direct_categories or CausalType.LOGISTICS_DELAY not in direct_categories:
        evidence = _collect_evidence(CausalType.LOGISTICS_DELAY, material_id, order_id)
        if evidence.get('severity') in ('high', 'medium'):
            factors.append({
                'type': 'contributing_factor',
                'category': CausalType.LOGISTICS_DELAY,
                'description': f'{evidence.get("pending_in_transit", 0)} 批调拨/在途物料未到位，加剧了供应紧张',
                'evidence': evidence,
                'impact_weight': 0.4,
                'actionable': True,
                'suggested_action': '跟踪在途物流状态，必要时启用备选运输方案',
            })

    # 检查需求激增
    evidence_demand = _collect_evidence(CausalType.DEMAND_SURGE, material_id, order_id)
    if evidence_demand.get('is_abnormal'):
        factors.append({
            'type': 'contributing_factor',
            'category': CausalType.DEMAND_SURGE,
            'description': f'当前订单需求量为历史均值的 {evidence_demand.get("surge_ratio", 1)} 倍，超出常规预期',
            'evidence': evidence_demand,
            'impact_weight': min(0.7, (evidence_demand.get('surge_ratio', 1) - 1) * 0.5),
            'actionable': True,
            'suggested_action': '与销售部门确认需求合理性，调整采购计划',
        })

    # 检查预测偏差
    evidence_forecast = _collect_evidence(CausalType.FORECAST_ERROR, material_id, order_id)
    if evidence_forecast.get('forecast_deviation_rate', 0) > 0.2:
        factors.append({
            'type': 'contributing_factor',
            'category': CausalType.FORECAST_ERROR,
            'description': f'实际需求与计划预测偏差率达 {evidence_forecast.get("forecast_deviation_rate", 0)*100:.1f}%',
            'evidence': evidence_forecast,
            'impact_weight': min(0.6, evidence_forecast.get('forecast_deviation_rate', 0)),
            'actionable': True,
            'suggested_action': '优化需求预测模型，增加安全库存缓冲',
        })

    # 检查产能约束
    evidence_cap = _collect_evidence(CausalType.CAPACITY_CONSTRAINT, material_id)
    if evidence_cap.get('severity') in ('high', 'medium'):
        factors.append({
            'type': 'contributing_factor',
            'category': CausalType.CAPACITY_CONSTRAINT,
            'description': f'{evidence_cap.get("overloaded_work_centers", 0)} 条产线处于高负荷运行，影响物料周转效率',
            'evidence': evidence_cap,
            'impact_weight': 0.35,
            'actionable': evidence_cap.get('overloaded_work_centers', 0) > 0,
            'suggested_action': '协调产线排程优化或安排加班生产',
        })

    return factors


def _identify_root_causes(material_id: int, order_id: int, event_info: dict,
                          direct_causes: list, contributing_factors: list) -> list:
    """识别第三层：根本原因（问题的源头）"""
    roots = []

    all_detected_categories = (
        set(c.get('category', '') for c in direct_causes) |
        set(f.get('category', '') for f in contributing_factors)
    )

    # 根据已检测到的因素，推断根本原因
    has_quality_issue = CausalType.QUALITY_HOLD in all_detected_categories
    has_supplier_issue = CausalType.SUPPLIER_DELAY in all_detected_categories
    has_demand_issue = CausalType.DEMAND_SURGE in all_detected_categories
    has_forecast_issue = CausalType.FORECAST_ERROR in all_detected_categories

    # 根因1: 如果有质量问题 → 根因可能是质量管理流程
    if has_quality_issue:
        evidence = _collect_evidence(CausalType.BOM_CHANGE, material_id, order_id)
        roots.append({
            'type': 'root_cause',
            'category': CausalType.QUALITY_HOLD,
            'description': '物料质量控制流程存在漏洞，导致频繁出现质量冻结',
            'evidence': evidence,
            'impact_weight': 0.85,
            'actionable': True,
            'suggested_action': '推动IQC进料检验流程优化，建立供应商质量预警机制',
        })

    # 根因2: 如果有供应商问题 → 根因可能是供应商管理策略
    if has_supplier_issue:
        roots.append({
            'type': 'root_cause',
            'category': CausalType.SUPPLIER_DELAY,
            'description': '单一供应商依赖度过高，缺乏有效的备选供应商体系',
            'evidence': _collect_evidence(CausalType.SUPPLIER_DELAY, material_id, order_id),
            'impact_weight': 0.80,
            'actionable': True,
            'suggested_action': '推进关键物料双源/多源供应商策略，降低单点风险',
        })

    # 根因3: 如果有需求或预测问题 → 根因可能是计划协同机制
    if has_demand_issue or has_forecast_issue:
        roots.append({
            'type': 'root_cause',
            'category': CausalType.FORECAST_ERROR,
            'description': '销售-运营-供应链三方协同机制不畅，需求信号传递失真',
            'evidence': _collect_evidence(CausalType.FORECAST_ERROR, material_id, order_id),
            'impact_weight': 0.75,
            'actionable': True,
            'suggested_action': '建立S&OP（销售运营计划）定期评审机制，提升需求可见性',
        })

    # 根因4: BOM/ECN变更影响（通用性检查）
    evidence_bom = _collect_evidence(CausalType.BOM_CHANGE, material_id, order_id)
    if evidence_bom.get('severity') in ('high', 'medium'):
        roots.append({
            'type': 'root_cause',
            'category': CausalType.BOM_CHANGE,
            'description': '工程变更管理流程不完善，ECN变更对供应链冲击未充分评估',
            'evidence': evidence_bom,
            'impact_weight': 0.70,
            'actionable': True,
            'suggested_action': '强化ECN变更的供应链影响预评估环节',
        })

    # 如果没有找到明确根因，给出综合建议
    if not roots:
        roots.append({
            'type': 'root_cause',
            'category': 'systemic',
            'description': '系统性供应链韧性不足，多环节缓冲能力偏弱',
            'evidence': {},
            'impact_weight': 0.60,
            'actionable': True,
            'suggested_action': '开展全面的供应链健康度诊断，识别薄弱环节',
        })

    return roots


def _summarize_root_causes(chain: list) -> list:
    """按类别汇总归因链中的所有原因"""
    summary_map = {}

    for item in chain:
        category = item.get('category', 'unknown')
        display_name = CausalType.DISPLAY_NAMES.get(category, category)

        if category not in summary_map:
            summary_map[category] = {
                'category': category,
                'display_name': display_name,
                'count': 0,
                'total_impact': 0.0,
                'types': set(),
            }

        summary_map[category]['count'] += 1
        summary_map[category]['total_impact'] += item.get('impact_weight', 0)
        summary_map[category]['types'].add(item.get('type', ''))

    # 转换为列表格式，按total_impact降序排列
    result = []
    for cat, data in summary_map.items():
        result.append({
            'category': data['category'],
            'display_name': data['display_name'],
            'count': data['count'],
            'total_impact': round(data['total_impact'], 3),
            'cause_types': sorted(list(data['types'])),
        })

    result.sort(key=lambda x: x['total_impact'], reverse=True)
    return result


def _build_mitigation_path(chain: list, event_info: dict) -> list:
    """
    从根因到解决方案的缓解路径

    基于归因链的分析结果，生成结构化的行动步骤
    """
    path = []
    step = 0

    # 按优先级从根因到直接原因生成缓解措施
    # 先处理根因（从后往前），再处理直接原因

    root_items = [c for c in chain if c.get('type') == 'root_cause']
    contrib_items = [c for c in chain if c.get('type') == 'contributing_factor']
    direct_items = [c for c in chain if c.get('type') == 'direct_cause']

    # 步骤1-2: 处理根因（长期措施）
    for root in sorted(root_items, key=lambda x: -x.get('impact_weight', 0)):
        step += 1
        path.append({
            'step': step,
            'phase': '根治',
            'action': root.get('suggested_action', ''),
            'target_category': CausalType.DISPLAY_NAMES.get(root.get('category', ''), root.get('category', '')),
            'expected_effect': f'消除{CausalType.DISPLAY_NAMES.get(root.get("category", ""), "")}类问题的根源',
            'confidence': round(0.7 + root.get('impact_weight', 0) * 0.25, 2),
            'timeframe': '中长期（2-8周）',
        })

    # 步骤3-4: 处理促进因素（中期措施）
    for contrib in sorted(contrib_items, key=lambda x: -x.get('impact_weight', 0)):
        step += 1
        path.append({
            'step': step,
            'phase': '缓解',
            'action': contrib.get('suggested_action', ''),
            'target_category': CausalType.DISPLAY_NAMES.get(contrib.get('category', ''), contrib.get('category', '')),
            'expected_effect': f'降低{CausalType.DISPLAY_NAMES.get(contrib.get("category", ""), "")}的影响程度',
            'confidence': round(0.6 + contrib.get('impact_weight', 0) * 0.3, 2),
            'timeframe': '中期（1-2周）',
        })

    # 步骤5+: 处理直接原因（短期应急）
    for direct in sorted(direct_items, key=lambda x: -x.get('impact_weight', 0)):
        step += 1
        path.append({
            'step': step,
            'phase': '应急',
            'action': direct.get('suggested_action', ''),
            'target_category': CausalType.DISPLAY_NAMES.get(direct.get('category', ''), direct.get('category', '')),
            'expected_effect': f'快速解决{CausalType.DISPLAY_NAMES.get(direct.get("category", ""), "")}导致的缺料',
            'confidence': round(0.75 + direct.get('impact_weight', 0) * 0.2, 2),
            'timeframe': '立即（0-3天）',
        })

    return path


# ============================================================
# API: 批量缺料事件共同根因分析
# ============================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def causal_batch_analysis(request):
    """
    批量缺料事件共同根因分析API

    GET参数:
        start_date: 分析起始日期（YYYY-MM-DD格式，可选，默认30天前）
        end_date: 分析结束日期（YYYY-MM-DD格式，可选，默认今天）
        material_ids: 物料ID列表，逗号分隔（可选过滤）
        factory_code: 工厂代码（可选过滤）

    返回:
        - pareto_data: 根因帕累托图数据（80%问题由20%原因导致）
        - causality_network: 因果关联网络数据（用于前端可视化）
        - trend_analysis: 趋势分析（某类根因是否在增加）
        - common_root_causes: 共同根因排名
    """
    from datetime import date, timedelta

    start_date_str = request.query_params.get('start_date')
    end_date_str = request.query_params.get('end_date')
    material_ids_str = request.query_params.get('material_ids', '')
    factory_code = request.query_params.get('factory_code', '')

    # 解析时间范围
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else date.today() - timedelta(days=30)
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else date.today()
    except (ValueError, TypeError):
        start_date = date.today() - timedelta(days=30)
        end_date = date.today()

    # 解析物料ID过滤
    material_ids_filter = None
    if material_ids_str:
        try:
            material_ids_filter = [int(m.strip()) for m in material_ids_str.split(',') if m.strip().isdigit()]
        except (ValueError, TypeError):
            pass

    logger.info(f"[批量根因分析] 时间范围: {start_date} ~ {end_date}, "
               f"物料过滤: {material_ids_filter}, 工厂: {factory_code}")

    try:
        # 1. 获取时间范围内的缺料事件集合
        shortage_events = _query_batch_shortage_events(start_date, end_date, material_ids_filter, factory_code)

        # 2. 对每个事件执行归因链分析（采样，避免性能问题）
        analyzed_events = _batch_analyze_events(shortage_events, max_events=50)

        # 3. 生成帕累托数据
        pareto_data = _generate_pareto_data(analyzed_events)

        # 4. 生成因果关联网络
        causality_network = _build_causality_network(analyzed_events)

        # 5. 趋势分析
        trend_analysis = _analyze_root_cause_trends(analyzed_events, start_date, end_date)

        # 6. 共同根因排名
        common_root_causes = _rank_common_root_causes(analyzed_events)

        result = {
            'success': True,
            'analysis_scope': {
                'start_date': str(start_date),
                'end_date': str(end_date),
                'total_events_found': len(shortage_events),
                'events_analyzed': len(analyzed_events),
                'material_filter': material_ids_filter,
                'factory_filter': factory_code,
            },
            'pareto_data': pareto_data,
            'causality_network': causality_network,
            'trend_analysis': trend_analysis,
            'common_root_causes': common_root_causes,
            'analyzed_at': __import__('datetime').datetime.now().isoformat(),
        }

        logger.info(f"[批量根因分析] 完成: {len(shortage_events)}个事件, "
                   f"分析{len(analyzed_events)}个, 发现{len(common_root_causes)}类共同根因")

        return Response(result)

    except Exception as e:
        logger.error(f"[批量根因分析] 异常: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'error': f'批量根因分析过程出错: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _query_batch_shortage_events(start_date, end_date, material_ids_filter=None, factory_code=None):
    """
    查询时间范围内的缺料事件

    通过MaterialPlanResult和SalesOrder关联查询获取缺料事件列表
    """
    events = []

    # 方案1: 从MaterialPlanResult中查找不齐套的计划结果
    query = Q(is_complete=False) | Q(complete_rate__lt=1.0)

    plan_results = MaterialPlanResult.objects.filter(query)

    # 关联订单的时间范围过滤
    if start_date and end_date:
        plan_results = plan_results.filter(
            order__demand_date__gte=start_date,
            order__demand_date__lte=end_date,
        )

    if material_ids_filter:
        plan_results = plan_results.filter(material_id__in=material_ids_filter)

    if factory_code:
        plan_results = plan_results.filter(order__factory_code=factory_code)

    plan_results = plan_results.select_related('order')[:200]

    import json
    for pr in plan_results:
        shortage_details = pr.shortage_details
        shortages = []
        if isinstance(shortage_details, str):
            try:
                shortage_details = json.loads(shortage_details)
            except Exception:
                shortage_details = {}
        if isinstance(shortage_details, list):
            shortages = shortage_details
        elif isinstance(shortage_details, dict):
            shortages = [shortage_details]

        for s in shortages:
            mat_id = s.get('material_id') or pr.material_id
            events.append({
                'material_id': mat_id,
                'order_id': pr.order_id,
                'order_no': pr.order.order_no if pr.order else '',
                'shortage_qty': s.get('shortage', 0),
                'demand_date': str(pr.order.demand_date) if pr.order and pr.order.demand_date else '',
                'complete_rate': pr.complete_rate or 0,
            })

    # 去重
    seen = set()
    unique_events = []
    for e in events:
        key = (e['material_id'], e['order_id'])
        if key not in seen:
            seen.add(key)
            unique_events.append(e)

    return unique_events


def _batch_analyze_events(events: list, max_events: int = 50) -> list:
    """
    批量分析缺料事件（带采样控制）

    为每个事件执行轻量级归因分析，提取关键归因特征
    """
    analyzed = []
    sample = events[:max_events] if len(events) > max_events else events

    for idx, event in enumerate(sample):
        try:
            material_id = event.get('material_id')
            order_id = event.get('order_id')

            # 快速归因分析（只做关键检测，不做完整链）
            quick_causes = _quick_cause_detection(material_id, order_id)

            analyzed.append({
                **event,
                'detected_causes': quick_causes,
                'primary_cause': quick_causes[0].get('category') if quick_causes else 'unknown',
            })
        except Exception as e:
            logger.debug(f"批量分析第{idx+1}个事件跳过: {str(e)}")
            analyzed.append({
                **event,
                'detected_causes': [],
                'primary_cause': 'analysis_failed',
                'error': str(e),
            })

    return analyzed


def _quick_cause_detection(material_id: int, order_id: int) -> list:
    """
    快速归因检测（用于批量分析的轻量版本）

    只返回最可能的归因类型及其权重，不构建完整链
    """
    causes = []

    # 并行检测各类归因（简化版）
    # 1. Hold检测
    held = Inventory.objects.filter(material_id=material_id, is_hold=True, quantity__gt=0).aggregate(
        t=Sum('quantity')
    )['t'] or 0
    if held > 0:
        causes.append({
            'category': CausalType.QUALITY_HOLD,
            'weight': min(1.0, held / 100.0),
        })

    # 2. 供应商延期检测
    try:
        commits = SupplierCommitment.objects.filter(
            material_id=material_id
        ).order_by('delivery_date')[:3]
        overdue = 0
        for c in commits:
            if c.delivery_date and c.delivery_date < __import__('datetime').date.today():
                overdue += 1
        if overdue > 0:
            causes.append({
                'category': CausalType.SUPPLIER_DELAY,
                'weight': min(1.0, overdue / 3.0),
            })
    except Exception:
        pass

    # 3. 库存不足检测
    total_stock = Inventory.objects.filter(material_id=material_id).aggregate(t=Sum('quantity'))['t'] or 0
    try:
        order = SalesOrder.objects.get(id=order_id)
        need = order.quantity or 0
        if total_stock < need * 0.5:
            causes.append({
                'category': CausalType.INVENTORY_MISALLOCATION,
                'weight': min(1.0, (need - total_stock) / max(need, 1)),
            })
    except Exception:
        pass

    # 4. 需求激增检测
    try:
        from datetime import timedelta
        order = SalesOrder.objects.get(id=order_id)
        avg_qty = SalesOrder.objects.filter(
            material_id=material_id,
            order_date__gte=__import__('datetime').date.today() - timedelta(days=30)
        ).exclude(id=order_id).aggregate(a=Sum('quantity'))['a'] or 1
        if float(order.quantity or 0) > float(avg_qty) * 1.5:
            causes.append({
                'category': CausalType.DEMAND_SURGE,
                'weight': min(1.0, float(order.quantity or 0) / max(float(avg_qty), 1) - 1),
            })
    except Exception:
        pass

    # 按权重降序排列
    causes.sort(key=lambda x: -x.get('weight', 0))
    return causes


def _generate_pareto_data(analyzed_events: list) -> dict:
    """
    生成帕累托图数据

    统计各根因类型的累计贡献比例，用于展示"80%问题由20%原因导致"
    """
    from collections import Counter

    # 统计每种根因出现的次数和累计影响权重
    cause_counts = Counter()
    cause_weights = {}

    for event in analyzed_events:
        primary = event.get('primary_cause', 'unknown')
        cause_counts[primary] += 1

        for cause in event.get('detected_causes', []):
            cat = cause.get('category', 'unknown')
            w = cause.get('weight', 0)
            cause_weights[cat] = cause_weights.get(cat, 0) + w

    total_events = len(analyzed_events)
    total_weight = sum(cause_weights.values()) or 1

    # 按出现次数降序排列
    sorted_causes = sorted(cause_counts.items(), key=lambda x: -x[1])

    pareto_items = []
    cumulative_count = 0
    cumulative_weight = 0.0

    for category, count in sorted_causes:
        cumulative_count += count
        cat_weight = cause_weights.get(category, 0)
        cumulative_weight += cat_weight

        pareto_items.append({
            'category': category,
            'display_name': CausalType.DISPLAY_NAMES.get(category, category),
            'event_count': count,
            'percentage': round(count / max(total_events, 1) * 100, 1),
            'cumulative_percentage': round(cumulative_count / max(total_events, 1) * 100, 1),
            'weight_contribution': round(cat_weight / total_weight * 100, 1),
            'cumulative_weight_pct': round(cumulative_weight / total_weight * 100, 1),
        })

    # 计算80/20分界点
    pareto_threshold_idx = 0
    for i, item in enumerate(pareto_items):
        if item['cumulative_percentage'] >= 80:
            pareto_threshold_idx = i + 1
            break

    return {
        'items': pareto_items,
        'total_events': total_events,
        'top_causes_for_80pct': pareto_items[:max(pareto_threshold_idx, 1)],
        'vital_few_count': pareto_threshold_idx,
        'trivial_many_count': len(pareto_items) - pareto_threshold_idx,
        'interpretation': (
            f'前{pareto_threshold_idx}类根因覆盖了约80%的缺料事件，'
            f'应优先集中资源解决这些关键问题'
        ),
    }


def _build_causality_network(analyzed_events: list) -> dict:
    """
    构建因果关联网络数据

    用于前端可视化展示不同归因类型之间的关联关系
    """
    from collections import defaultdict

    # 节点：每种归因类型作为一个节点
    nodes = []
    for ctype in CausalType.ALL_TYPES:
        nodes.append({
            'id': ctype,
            'name': CausalType.DISPLAY_NAMES.get(ctype, ctype),
            'category': ctype,
        })

    # 边：统计共同出现在同一事件中的归因类型对
    co_occurrence = defaultdict(int)
    event_cause_sets = []

    for event in analyzed_events:
        cause_set = set()
        for cause in event.get('detected_causes', []):
            cause_set.add(cause.get('category', 'unknown'))
        event_cause_sets.append(cause_set)

        # 两两组合统计共现
        cause_list = list(cause_set)
        for i in range(len(cause_list)):
            for j in range(i + 1, len(cause_list)):
                pair = tuple(sorted([cause_list[i], cause_list[j]]))
                co_occurrence[pair] += 1

    # 构建边数据
    edges = []
    for (source, target), weight in sorted(co_occurrence.items(), key=lambda x: -x[1]):
        if weight >= 2:  # 至少共现2次才显示连线
            edges.append({
                'source': source,
                'target': target,
                'weight': weight,
                'strength': round(weight / max(len(analyzed_events), 1) * 10, 2),
            })

    # 计算中心性指标（简单的度中心性）
    degree_centrality = defaultdict(int)
    for edge in edges:
        degree_centrality[edge['source']] += 1
        degree_centrality[edge['target']] += 1

    # 更新节点信息
    for node in nodes:
        node['degree'] = degree_centrality.get(node['id'], 0)
        node['event_count'] = sum(
            1 for e in analyzed_events
            if any(c.get('category') == node['id'] for c in e.get('detected_causes', []))
        )

    # 找出核心节点（高度最高的几个）
    nodes_sorted = sorted(nodes, key=lambda x: -x['degree'])
    core_nodes = [n['id'] for n in nodes_sorted[:3]]

    return {
        'nodes': nodes,
        'edges': edges[:30],  # 限制边数量避免过于复杂
        'core_nodes': core_nodes,
        'network_stats': {
            'total_nodes': len(nodes),
            'total_edges': len(edges),
            'density': round(len(edges) / max(len(nodes) * (len(nodes) - 1) / 2, 1), 3),
            'core_node_names': [CausalType.DISPLAY_NAMES.get(n, n) for n in core_nodes],
        },
    }


def _analyze_root_cause_trends(analyzed_events: list, start_date, end_date) -> dict:
    """
    趋势分析：判断某类根因是否随时间呈上升趋势

    将时间范围划分为多个时间段，观察各根因类型的占比变化
    """
    from collections import defaultdict
    from datetime import timedelta

    # 将时间范围分为4个子区间
    total_days = (end_date - start_date).days
    interval_days = max(total_days // 4, 1)

    intervals = []
    current_start = start_date
    for i in range(4):
        interval_end = current_start + timedelta(days=interval_days - 1)
        if i == 3:
            interval_end = end_date
        intervals.append({
            'label': f'T{i+1}',
            'start': current_start,
            'end': interval_end,
        })
        current_start = interval_end + timedelta(days=1)

    # 统计每个时间段内各根因的出现频率
    interval_cause_counts = defaultdict(lambda: defaultdict(int))

    for event in analyzed_events:
        demand_date_str = event.get('demand_date', '')
        try:
            event_date = datetime.strptime(demand_date_str, '%Y-%m-%d').date() if demand_date_str else start_date
        except (ValueError, TypeError):
            event_date = start_date

        # 确定属于哪个时间段
        for idx, iv in enumerate(intervals):
            if iv['start'] <= event_date <= iv['end']:
                primary = event.get('primary_cause', 'unknown')
                interval_cause_counts[iv['label']][primary] += 1
                break

    # 生成趋势数据
    trend_series = {}
    for ctype in CausalType.ALL_TYPES:
        series_data = []
        for iv in intervals:
            count = interval_cause_counts[iv['label']].get(ctype, 0)
            series_data.append({
                'interval': iv['label'],
                'date_range': f"{iv['start']} ~ {iv['end']}",
                'count': count,
            })
        trend_series[ctype] = series_data

    # 判断哪些根因呈上升趋势
    increasing_causes = []
    decreasing_causes = []
    stable_causes = []

    for ctype, series in trend_series.items():
        counts = [s['count'] for s in series]
        if len(counts) >= 2:
            first_half_avg = sum(counts[:2]) / 2
            second_half_avg = sum(counts[2:]) / 2
            change_rate = (second_half_avg - first_half_avg) / max(first_half_avg, 1)

            if change_rate > 0.3:
                increasing_causes.append({
                    'category': ctype,
                    'display_name': CausalType.DISPLAY_NAMES.get(ctype, ctype),
                    'change_rate': round(change_rate * 100, 1),
                    'trend': '上升 ⚠️',
                })
            elif change_rate < -0.3:
                decreasing_causes.append({
                    'category': ctype,
                    'display_name': CausalType.DISPLAY_NAMES.get(ctype, ctype),
                    'change_rate': round(change_rate * 100, 1),
                    'trend': '下降 ✅',
                })
            else:
                stable_causes.append({
                    'category': ctype,
                    'display_name': CausalType.DISPLAY_NAMES.get(ctype, ctype),
                    'change_rate': round(change_rate * 100, 1),
                    'trend': '稳定 ➡️',
                })

    return {
        'intervals': [{'label': iv['label'], 'range': f"{iv['start']} ~ {iv['end']}"} for iv in intervals],
        'trend_series': {
            ctype: series for ctype, series in trend_series.items()
            if any(s['count'] > 0 for s in series)
        },
        'increasing_causes': sorted(increasing_causes, key=lambda x: -abs(x['change_rate'])),
        'decreasing_causes': decreasing_causes,
        'stable_causes': stable_causes,
        'alert': (
            f'⚠️ 发现{len(increasing_causes)}类根因呈上升趋势，'
            f'需要重点关注: {", ".join([c["display_name"] for c in increasing_causes[:3]])}'
        ) if increasing_causes else '✅ 各类根因总体稳定或下降，无显著恶化趋势',
    }


def _rank_common_root_causes(analyzed_events: list) -> list:
    """
    共同根因排名

    基于所有分析事件的归因结果，找出最普遍的共同根因
    """
    from collections import defaultdict

    # 综合评分 = 出现频次 × 平均影响权重
    cause_scores = defaultdict(lambda: {'count': 0, 'total_weight': 0, 'events': []})

    for event in analyzed_events:
        event_key = f"{event.get('material_id')}_{event.get('order_id')}"

        for cause in event.get('detected_causes', []):
            cat = cause.get('category', 'unknown')
            w = cause.get('weight', 0)
            cause_scores[cat]['count'] += 1
            cause_scores[cat]['total_weight'] += w
            if len(cause_scores[cat]['events']) < 5:
                cause_scores[cat]['events'].append(event_key)

    total_events = max(len(analyzed_events), 1)

    rankings = []
    for cat, score_data in cause_scores.items():
        frequency = score_data['count'] / total_events
        avg_weight = score_data['total_weight'] / max(score_data['count'], 1)
        composite_score = frequency * 0.6 + avg_weight * 0.4

        rankings.append({
            'rank': 0,  # 后面填充
            'category': cat,
            'display_name': CausalType.DISPLAY_NAMES.get(cat, cat),
            'frequency': round(frequency * 100, 1),  # 百分比
            'event_count': score_data['count'],
            'avg_impact_weight': round(avg_weight, 3),
            'composite_score': round(composite_score, 4),
            'sample_events': score_data['events'][:3],
        })

    # 按综合得分降序排列
    rankings.sort(key=lambda x: -x['composite_score'])

    for i, r in enumerate(rankings):
        r['rank'] = i + 1

    return {
        'rankings': rankings[:10],  # 取Top 10
        'top_root_cause': rankings[0] if rankings else None,
        'summary': (
            f'最常见的根因是「{rankings[0]["display_name"]}」，'
            f'影响了{rankings[0]["frequency"]}%的缺料事件'
        ) if rankings else '暂无足够数据进行根因排名',
    }
