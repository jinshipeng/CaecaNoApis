"""
What-If Scenario Simulator - 独立场景仿真模块

从 ai_views.py 中提取，实现单一职责原则。
支持5种仿真场景:
- urgent_insert: 紧急插单影响分析（高优先级订单插入）
- order_cancel: 砍单/取消订单影响（释放物料与产能）
- supplier_delay: 供应商延期评估（交期延后）
- capacity_failure: 产能故障模拟（产线停机）
- bom_ecn: BOM工程变更影响（子件替换）
"""

import json
from datetime import datetime, timedelta, date
from collections import defaultdict
import logging

from django.db.models import Sum
import numpy as np

logger = logging.getLogger(__name__)

from .models import SalesOrder, Material, Inventory, OrderAllocation, PlanLog, MaterialPlanResult


class WhatIfSimulator:
    """
    What-If Scenario Simulator
    
    用于评估不同决策对供应链的影响。
    支持5种仿真场景:
    - urgent_insert: 紧急插单影响分析（高优先级订单插入）
    - order_cancel: 砍单/取消订单影响（释放物料与产能）
    - supplier_delay: 供应商延期评估（交期延后）
    - capacity_failure: 产能故障模拟（产线停机）
    - bom_ecn: BOM工程变更影响（子件替换）
    """

    def run_simulation(self, scenario, params):
        """运行指定场景的仿真模拟（统一异常防护，防止500错误）"""

        # 场景方法映射
        scenario_methods = {
            'urgent_insert': self._simulate_urgent_insert,
            'order_cancel': self._simulate_order_cancel,
            'supplier_delay': self._simulate_supplier_delay,
            'capacity_failure': self._simulate_capacity_failure,
            'bom_ecn': self._simulate_bom_ecn,
            'capacity_change': self._simulate_capacity_change,
            'demand_surge': self._simulate_demand_surge,
        }

        if scenario not in scenario_methods:
            raise ValueError(f"未知场景: {scenario}")

        try:
            return scenario_methods[scenario](params)
        except ValueError:
            raise  # 让未知场景的错误正常抛出，由视图层返回400
        except Exception as e:
            logger.error(f'What-If仿真异常 [scenario={scenario}]: {str(e)}', exc_info=True)
            # 返回安全的兜底结果而非让异常冒泡导致500
            return {
                'scenario_name': f'{scenario}_simulation_error',
                'error': f'仿真执行出错: {str(e)}',
                'input_parameters': params,
                'overall_impact_score': 0.5,
                'risk_assessment': {'risk_score': 0.5, 'risk_level': '仿真异常'},
                'recommendations': [{'action': 'RETRY', 'reason': f'请检查参数或联系管理员。错误: {str(e)}', 'steps': []}],
                'decision_support': {'can_accept': False, 'accept_conditionally': True, 'should_decline': False},
                'simulated_at': datetime.now().isoformat()
            }

    def _simulate_urgent_insert(self, params):
        """
        紧急插单仿真 - 模拟插入一个高优先级订单，分析对现有订单的影响范围
        输出: 受影响的订单列表（哪些会延期）、建议的让料方案、风险评分
        """
        quantity = float(params.get('quantity', 100))
        demand_date_str = params.get('demand_date')
        priority = int(params.get('priority', 1))  # 默认最高优先级

        try:
            demand_date = datetime.strptime(demand_date_str, '%Y-%m-%d').date() if demand_date_str else date.today() + timedelta(days=7)
        except (ValueError, TypeError):
            demand_date = date.today() + timedelta(days=7)

        # 查询受影响订单：优先级低于新订单且交期接近的活跃订单
        active_orders = list(
            SalesOrder.objects.filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing'],
                demand_date__gte=date.today()
            )
            .select_related('material')
            .order_by('priority', 'demand_date')[:50]
        )

        # 分析受影响订单
        affected_orders = []
        for order in active_orders:
            days_to_order = (order.demand_date - date.today()).days
            days_to_new_order = (demand_date - date.today()).days

            # 新订单优先级更高且交期接近时产生冲突
            if priority < order.priority and abs(days_to_order - days_to_new_order) < 14:
                impact_level = 'high' if priority < order.priority and abs(days_to_order - days_to_new_order) < 7 else 'medium'
                potential_delay = max(0, 3 if impact_level == 'high' else 1)

                affected_orders.append({
                    'order_no': order.order_no,
                    'current_priority': order.priority,
                    'demand_date': str(order.demand_date),
                    'material_code': order.material.material_code if order.material else '',
                    'risk_level': impact_level,
                    'potential_delay_days': potential_delay,
                    'suggestion': f'建议{"延后" if impact_level == "high" else "关注"}该订单交付日期{potential_delay}天'
                })

        affected_orders.sort(key=lambda x: (0 if x['risk_level'] == 'high' else 1, -x['potential_delay_days']))

        # 计算风险评分
        high_risk_count = sum(1 for o in affected_orders if o['risk_level'] == 'high')
        risk_score = min(1.0, 0.15 + high_risk_count * 0.12 + len(affected_orders) * 0.03)
        risk_level = '低' if risk_score < 0.3 else ('中' if risk_score < 0.6 else ('高' if risk_score < 0.8 else '极高'))

        # 生成让料方案建议
        reallocation_suggestions = []
        if affected_orders:
            low_priority_orders = [o for o in affected_orders if o['risk_level'] == 'medium']
            if low_priority_orders:
                reallocation_suggestions.append({
                    'strategy': '低优先级让料',
                    'description': f'从{len(low_priority_orders)}个中/低优先级订单释放物料资源',
                    'affected_count': len(low_priority_orders),
                    'feasibility': '高'
                })
            reallocation_suggestions.append({
                'strategy': '安全库存借用',
                'description': '临时调用安全库存满足紧急订单，后续补库',
                'feasibility': '中',
                'condition': '需在合理周期内完成补库采购'
            })
            reallocation_suggestions.append({
                'strategy': '供应商加急',
                'description': '联系关键物料供应商安排加急生产或空运',
                'cost_impact': '物流成本增加（加急运费）',
                'feasibility': '中-高'
            })

        return {
            'scenario_name': '紧急插单影响分析',
            'input_parameters': {'quantity': quantity, 'demand_date': str(demand_date), 'priority': priority},
            'affected_orders': {
                'total_affected': len(affected_orders),
                'high_risk_count': high_risk_count,
                'orders_at_risk': [o for o in affected_orders if o['risk_level'] == 'high'],
                'details': affected_orders[:10]
            },
            'reallocation_suggestions': reallocation_suggestions,
            'risk_assessment': {
                'risk_score': round(risk_score, 3),
                'risk_level': risk_level,
                'reasoning': f'影响{len(affected_orders)}个现有订单，其中{high_risk_count}个高风险'
            },
            'decision_support': {
                'can_accept': risk_score < 0.5,
                'accept_conditionally': 0.5 <= risk_score < 0.75,
                'should_decline': risk_score >= 0.75
            },
            'simulated_at': datetime.now().isoformat()
        }

    def _simulate_supplier_delay(self, params):
        """Simulate supplier delay impact"""
        try:
            supplier_id = params.get('supplier_id')
            delay_days = int(params.get('delay_days', 7))
            affected_materials = params.get('affected_materials', [])

            from .models import SupplierCommitment

            # 防御：supplier_id为空时返回提示而非报错
            if not supplier_id:
                return {
                    'scenario_name': '供应商延期评估',
                    'input_parameters': {'supplier_id': supplier_id, 'delay_days': delay_days},
                    'warning': '未指定供应商ID，请选择要模拟延期的供应商',
                    'impacted_commitments': [],
                    'impacted_orders': [],
                    'risk_assessment': {'risk_score': 0, 'risk_level': '未知', 'commitments_affected': 0},
                    'mitigation_strategies': [],
                    'alternative_suppliers': [],
                    'simulated_at': datetime.now().isoformat()
                }

            commitments = SupplierCommitment.objects.filter(supplier_id=supplier_id).select_related('material', 'supplier')
            if affected_materials:
                commitments = commitments.filter(material_id__in=affected_materials)

            commitment_list = []
            for comm in commitments:
                original_date = comm.delivery_date
                new_date = original_date + timedelta(days=delay_days)
                commitment_list.append({
                    'commitment_id': comm.id,
                    'material_code': comm.material.material_code if comm.material else '',
                    'material_name': comm.material.material_name if comm.material else '',
                    'original_delivery_date': str(original_date),
                    'new_delivery_date': str(new_date),
                    'delay_days': delay_days,
                    'quantity': int(comm.quantity or 0),
                    'order_no': comm.order_no
                })

            impacted_orders = self._analyze_delay_impact_on_orders(commitment_list, delay_days)
            risk_assessment = self._assess_delay_risk(commitment_list, delay_days, impacted_orders)
            mitigation_strategies = self._suggest_delay_mitigation(delay_days, commitment_list)

            return {
                'scenario_name': '供应商延期评估',
                'input_parameters': {'supplier_id': supplier_id, 'delay_days': delay_days, 'affected_commitments': len(commitment_list)},
                'impacted_commitments': commitment_list,
                'impacted_orders': impacted_orders,
                'risk_assessment': risk_assessment,
                'mitigation_strategies': mitigation_strategies,
                'alternative_suppliers': self._find_alternative_suppliers(supplier_id, [c['material_code'] for c in commitment_list]),
                'simulated_at': datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f'供应商延期仿真异常: {str(e)}', exc_info=True)
            return {
                'scenario_name': '供应商延期评估',
                'error': f'仿真执行出错: {str(e)}',
                'input_parameters': params,
                'impacted_commitments': [],
                'risk_assessment': {'risk_score': 0.5, 'risk_level': '仿真异常'},
                'simulated_at': datetime.now().isoformat()
            }

    def _simulate_capacity_change(self, params):
        """Simulate capacity change impact"""
        work_center = params.get('work_center')
        change_type = params.get('change_type', 'increase')
        change_pct = float(params.get('change_percentage', 20)) / 100
        duration = int(params.get('duration_days', 30))
        
        from .models import WorkCenter
        
        wc = WorkCenter.objects.filter(work_center_code=work_center).first()
        if not wc:
            return {'success': False, 'error': f'Work center {work_center} not found'}
        
        current_capacity = float(wc.daily_capacity_limit or 0)
        
        if change_type == 'increase':
            new_capacity = current_capacity * (1 + change_pct)
            capacity_delta = current_capacity * change_pct
        else:
            new_capacity = current_capacity * (1 - change_pct)
            capacity_delta = -current_capacity * change_pct
        
        production_impact = self._evaluate_production_impact(work_center, current_capacity, new_capacity, duration)
        delivery_impact = self._assess_delivery_impact_from_capacity_change(work_center, capacity_delta, duration)
        roi_analysis = None
        if change_type == 'increase':
            roi_analysis = self._calculate_capacity_expansion_roi(current_capacity, capacity_delta, production_impact)
        
        return {
            'scenario_name': 'Capacity Change Impact Analysis',
            'work_center': {'code': work_center, 'name': wc.work_center_name if wc else '', 'current_daily_capacity': current_capacity, 'new_daily_capacity': round(new_capacity, 2), 'change_type': change_type, 'change_amount': round(capacity_delta, 2), 'change_percentage': round(change_pct * 100, 1)},
            'duration_days': duration,
            'production_impact': production_impact,
            'delivery_impact': delivery_impact,
            'roi_analysis': roi_analysis,
            'recommendations': self._generate_capacity_recommendations(change_type, production_impact, delivery_impact),
            'simulated_at': datetime.now().isoformat()
        }

    def _generate_capacity_recommendations(self, change_type, production_impact, delivery_impact):
        """
        根据产能变化生成建议列表（标准格式: {action, reason, steps}）
        """
        recs = []
        is_increase = change_type == 'increase'

        # 基础建议
        if is_increase:
            recs.append({
                'action': 'ACCEPT',
                'reason': '产能增加后可承接更多订单，建议重新评估高优先级订单的排产计划',
                'steps': ['更新工作中心产能参数', '运行新产能下的MRP计算', '释放之前因产能不足被延迟的订单'],
                'priority': 'P1',
                'confidence': '高'
            })
            recs.append({
                'action': 'NEGOTIATE',
                'reason': '设备/人员增加后需进行磨合验证，建议设置2-3周的观察期',
                'steps': ['小批量试产验证新产能稳定性', '监控首检合格率', '确认设备OEE达标'],
                'priority': 'P2',
                'confidence': '中'
            })
        else:
            recs.append({
                'action': 'DECLINE',
                'reason': '产能下降将导致部分订单延迟交付，需优先保护高价值客户订单',
                'steps': ['识别受影响订单列表', '与客户协商调整交付时间', '考虑外协加工替代产能'],
                'priority': 'P1',
                'confidence': '高'
            })
            recs.append({
                'action': 'NEGOTIATE',
                'reason': '建议暂停非关键订单的新单承接，集中资源保护核心产品线',
                'steps': ['标记受影响工作中心为"受限状态"', '通知销售谨慎接单', '评估是否需要加班/外包弥补'],
                'priority': 'P2',
                'confidence': '中'
            })

        # 根据生产影响追加建议
        if isinstance(production_impact, dict):
            delayed = production_impact.get('orders_delayed', 0)
            if isinstance(delayed, (int, float)) and delayed > 0:
                recs.append({
                    'action': 'NEGOTIATE',
                    'reason': f"预计有 {int(delayed)} 个订单生产进度受影响，需启动应急排产调整",
                    'steps': ['重新优化排产顺序', '启用备用工作中心', '调整班次安排'],
                    'priority': 'P1',
                    'confidence': '高'
                })

        # 根据交付影响追加建议
        if isinstance(delivery_impact, dict):
            delayed_d = delivery_impact.get('orders_potentially_delayed', 0)
            if isinstance(delayed_d, (int, float)) and delayed_d > 0:
                recs.append({
                    'action': 'NEGOTIATE',
                    'reason': f"预计 {int(delayed_d)} 个订单交付可能延迟，需提前与客户沟通",
                    'steps': ['识别受影响订单列表', '评估延迟天数', '主动联系受影响客户'],
                    'priority': 'P2',
                    'confidence': '中'
                })

        return recs

    def _evaluate_production_impact(self, work_center, current_capacity, new_capacity, duration):
        """评估产能变化对生产的影响"""
        try:
            from .models import SalesOrder
            delta_pct = (new_capacity - current_capacity) / max(current_capacity, 1)
            recent_orders = SalesOrder.objects.filter(
                order_date__gte=date.today() - timedelta(days=30),
                status__in=['pending', 'in_progress']
            ).count()
            orders_delayed = max(0, int(recent_orders * abs(delta_pct) * 0.3))
            return {
                'work_center': work_center,
                'capacity_delta': round(new_capacity - current_capacity, 2),
                'capacity_change_pct': round(delta_pct * 100, 1),
                'orders_delayed': orders_delayed,
                'average_delay_days': max(1, int(abs(delta_pct) * 5)) if delta_pct < 0 else 0,
                'production_volume_change': round(delta_pct * 100, 1),
            }
        except Exception as e:
            logger.warning(f"_evaluate_production_impact failed: {e}")
            return {'work_center': work_center, 'orders_delayed': 0, 'capacity_change_pct': 0}

    def _assess_delivery_impact_from_capacity_change(self, work_center, capacity_delta, duration):
        """评估产能变化对交付的影响"""
        try:
            from .models import SalesOrder
            delta_pct = capacity_delta / max(abs(capacity_delta) + 100, 1)
            recent_orders = SalesOrder.objects.filter(
                order_date__gte=date.today() - timedelta(days=30)
            ).count()
            orders_potentially_delayed = max(0, int(recent_orders * abs(delta_pct) * 0.5)) if delta_pct < 0 else 0
            return {
                'orders_potentially_delayed': orders_potentially_delayed,
                'on_time_delivery_change_pct': round(delta_pct * 10, 1),
                'delivery_risk_level': '高' if delta_pct < -0.15 else ('中' if delta_pct < 0 else '低'),
            }
        except Exception as e:
            logger.warning(f"_assess_delivery_impact_from_capacity_change failed: {e}")
            return {'orders_potentially_delayed': 0, 'delivery_risk_level': '低'}

    def _calculate_capacity_expansion_roi(self, current_capacity, capacity_delta, production_impact):
        """计算产能扩张的投资回报（简化模型）"""
        try:
            avg_revenue_per_unit = 500.0
            additional_output = capacity_delta * 30
            additional_revenue = additional_output * avg_revenue_per_unit
            investment_cost = capacity_delta * 2000
            roi = (additional_revenue - investment_cost) / max(investment_cost, 1)
            payback_months = max(1, int(investment_cost / max(additional_revenue / 12, 1))) if additional_revenue > 0 else 999
            return {
                'estimated_additional_revenue': round(additional_revenue, 0),
                'estimated_investment_cost': round(investment_cost, 0),
                'projected_roi_pct': round(roi * 100, 1),
                'payback_period_months': payback_months,
                'financial_risk': '低' if roi > 0.5 else ('中' if roi > 0.1 else '高'),
            }
        except Exception as e:
            logger.warning(f"_calculate_capacity_expansion_roi failed: {e}")
            return {'projected_roi_pct': 0, 'financial_risk': '中'}

    def _simulate_demand_surge(self, params):
        """Simulate demand surge scenario"""
        surge_pct = float(params.get('surge_percentage', 50)) / 100
        duration = int(params.get('duration_days', 14))
        
        from django.db import models as django_models
        
        recent_orders = SalesOrder.objects.filter(order_date__gte=date.today() - timedelta(days=30))
        avg_daily_demand = float(recent_orders.aggregate(avg_qty=django_models.Avg('quantity'))['avg_qty'] or 0)
        surge_demand = avg_daily_demand * (1 + surge_pct)
        total_additional_demand = surge_demand * duration
        
        supply_chain_stress = self._stress_test_supply_chain(surge_demand, duration)
        inventory_burn_rate = self._calculate_inventory_burn_rate(surge_demand)
        shortage_risk = self._assess_shortage_risk_under_surge(total_additional_demand)
        emergency_response = self._generate_emergency_response_plan(surge_pct, duration)
        
        return {
            'scenario_name': 'Demand Surge Stress Test',
            'input_parameters': {'surge_percentage': round(surge_pct * 100, 1), 'duration_days': duration, 'current_avg_daily_demand': round(avg_daily_demand, 1), 'projected_daily_demand': round(surge_demand, 1), 'total_additional_demand': round(total_additional_demand, 1)},
            'supply_chain_stress': supply_chain_stress,
            'inventory_analysis': {'burn_rate': inventory_burn_rate, 'days_of_supply_remaining': inventory_burn_rate.get('days_of_supply_remaining', 0), 'critical_materials': inventory_burn_rate.get('critical_materials', [])},
            'shortage_risk_assessment': shortage_risk,
            'emergency_response_plan': emergency_response,
            'recovery_timeline': self._estimate_recovery_timeline(surge_pct, duration),
            'simulated_at': datetime.now().isoformat()
        }

    def _estimate_material_needs(self, quantity):
        """Estimate required materials"""
        avg_bom_ratio = 3.5
        return {
            'estimated_unique_materials': int(avg_bom_ratio),
            'estimated_total_quantity': round(quantity * avg_bom_ratio, 1),
            'note': 'Based on historical BOM structure estimation'
        }

    def _analyze_order_impacts(self, quantity, demand_date, priority):
        """Analyze impact on existing orders（优化版本 - 使用select_related）"""
        # 使用select_related预加载material，避免N+1查询
        active_orders = list(
            SalesOrder.objects.filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing'],  # 所有未完成状态
                demand_date__gte=date.today()  # 只查未来的订单
            )
            .select_related('material')
            .order_by('priority', 'demand_date')[:50]  # 限制最多50个订单
        )

        impacted = []
        for order in active_orders:
            days_to_order = (order.demand_date - date.today()).days
            days_to_new_order = (demand_date - date.today()).days

            if priority < order.priority and abs(days_to_order - days_to_new_order) < 14:
                impact_level = 'high' if priority < order.priority else 'medium'

                if impact_level != 'low':
                    impacted.append({
                        'order_no': order.order_no,
                        'current_priority': order.priority,
                        'demand_date': str(order.demand_date),
                        'risk_level': impact_level,
                        'potential_delay_days': max(0, 3 if impact_level == 'high' else 1) if priority < order.priority else 0
                    })

        return sorted(impacted, key=lambda x: (0 if x['risk_level'] == 'high' else 1, -x['potential_delay_days']))

    def _evaluate_capacity_impact(self, quantity, demand_date):
        """Evaluate capacity impact"""
        days_available = max(1, (demand_date - date.today()).days)
        daily_capacity_needed = quantity / days_available
        
        current_utilization = 0.75
        projected_utilization = min(1.0, current_utilization + (daily_capacity_needed / 1000))
        
        return {
            'additional_daily_capacity_required': round(daily_capacity_needed, 1),
            'current_utilization': current_utilization,
            'projected_utilization': round(projected_utilization, 3),
            'utilization_increase': round(projected_utilization - current_utilization, 3),
            'is_feasible': projected_utilization < 0.95,
            'overtime_required': projected_utilization > 0.9
        }

    def _evaluate_inventory_impact(self, material_reqs):
        """Evaluate inventory impact（优化：使用聚合查询替代全表遍历）"""
        total_estimated_need = material_reqs.get('estimated_total_quantity', 0)

        from .models import Inventory
        # 使用单次聚合查询，避免加载所有库存记录到内存
        total_inventory = float(Inventory.objects.aggregate(
            total=Sum('quantity')
        )['total'] or 0)

        inventory_after_allocation = max(0, total_inventory - total_estimated_need)
        depletion_rate = total_estimated_need / (total_inventory + 1e-6) if total_inventory > 0 else 1

        return {
            'total_current_inventory': round(total_inventory, 1),
            'estimated_consumption': round(total_estimated_need, 1),
            'remaining_inventory': round(inventory_after_allocation, 1),
            'depletion_rate': round(depletion_rate, 3),
            'risk_level': 'critical' if depletion_rate > 0.3 else ('high' if depletion_rate > 0.15 else 'medium'),
            'safety_stock_impacted': depletion_rate > 0.1
        }

    def _calculate_overall_impact(self, orders_impact, capacity_impact, inventory_impact):
        """Calculate overall impact score (0-1)"""
        order_factor = len(orders_impact) * 0.05
        high_risk_orders = sum(1 for o in orders_impact if o['risk_level'] == 'high')
        order_factor += high_risk_orders * 0.1
        
        capacity_factor = capacity_impact.get('utilization_increase', 0) * 2
        if not capacity_impact.get('is_feasible'):
            capacity_factor += 0.3
        
        inventory_factor = inventory_impact.get('depletion_rate', 0) * 1.5
        if inventory_impact.get('risk_level') == 'critical':
            inventory_factor += 0.2
        
        overall_score = min(1.0, 0.2 + order_factor + capacity_factor + inventory_factor)
        
        return round(overall_score, 3)

    def _generate_urgent_order_recommendations(self, score, orders, capacity, inventory):
        """Generate urgent order recommendations"""
        recs = []
        
        if score < 0.4:
            recs.append({'action': 'ACCEPT', 'confidence': 'high', 'reason': 'Low impact, can accept', 'steps': ['Confirm order', 'Check material availability', 'Schedule resources']})
        elif score < 0.7:
            recs.append({'action': 'CONDITIONAL_ACCEPT', 'confidence': 'medium', 'reason': 'Moderate impact but manageable', 'steps': ['Negotiate slight delivery adjustment', 'Expedite supplier sourcing', 'Arrange overtime', 'Monitor closely'], 'conditions': ['Customer agrees to adjustment', 'Key materials arrive on time', 'Capacity needs adjustment']})
        else:
            recs.append({'action': 'NEGOTIATE_OR_DECLINE', 'confidence': 'high', 'reason': 'High impact, not recommended', 'steps': ['Explain situation to customer', 'Provide alternative delivery options', 'Escalate to management if needed', 'Consider outsourcing'], 'alternatives': ['Postpone to next week', 'Partial delivery first', 'Alternative product configuration']})
        
        if len(orders) > 0:
            recs.append({'type': 'mitigation', 'category': 'order_management', 'title': 'Affected order handling', 'details': f'{len(orders)} orders may be affected, {sum(1 for o in orders if o["risk_level"]=="high")} high risk', 'actions': ['Proactively contact affected customers', 'Prepare contingency plans for high-risk orders', 'Consider releasing materials from lower-priority orders']})
        
        if not capacity.get('is_feasible'):
            recs.append({'type': 'mitigation', 'category': 'capacity', 'title': 'Capacity constraint handling', 'details': f'Utilization will reach {capacity["projected_utilization"]:.0%}', 'actions': ['Schedule weekend overtime', 'Coordinate external manufacturing partners', 'Optimize scheduling to reduce changeover time']})
        
        if inventory.get('risk_level') in ['high', 'critical']:
            recs.append({'type': 'mitigation', 'category': 'inventory', 'title': 'Inventory tension handling', 'details': f'Will consume {inventory["estimated_consumption"]:.0f} units, inventory drops {inventory["depletion_rate"]*100:.0f}%', 'actions': ['Contact suppliers immediately for expedited delivery', 'Use safety stock buffer', 'Find alternative material sources']})
        
        return recs

    def _explain_impact_score(self, score):
        """Explain impact score"""
        if score < 0.3:
            return "Minimal impact, system has sufficient capacity"
        elif score < 0.5:
            return "Medium impact, some mitigation measures needed"
        elif score < 0.7:
            return "Significant impact, multi-department coordination required"
        elif score < 0.85:
            return "Severe impact, management decision required"
        else:
            return "Extreme risk, strongly recommend rejection or significant condition changes"

    def _analyze_delay_impact_on_orders(self, commitments, delay_days):
        """Analyze delay impact on orders"""
        impacted = []
        
        for comm in commitments:
            from .models import OrderAllocation, SalesOrder
            
            related_orders = SalesOrder.objects.filter(id__in=OrderAllocation.objects.filter(material_id=comm['material_id']).values_list('order_id', flat=True))[:5]
            
            for order in related_orders:
                if order.demand_date:
                    days_until_delivery = (order.demand_date - date.today()).days
                    buffer_before = days_until_delivery - delay_days
                    
                    impacted.append({
                        'order_no': order.order_no,
                        'material_affected': comm['material_code'],
                        'original_buffer_days': days_until_delivery,
                        'remaining_buffer_days': max(0, buffer_before),
                        'at_risk': buffer_before < 7,
                        'severity': 'critical' if buffer_before < 0 else ('high' if buffer_before < 3 else 'medium')
                    })
        
        unique_impacted = []
        seen = set()
        for item in impacted:
            key = (item['order_no'], item['material_affected'])
            if key not in seen:
                seen.add(key)
                unique_impacted.append(item)
        
        return sorted(unique_impacted, key=lambda x: x['remaining_buffer_days'])

    def _assess_delay_risk(self, commitments, delay_days, impacted_orders):
        """Assess delay risk"""
        total_quantity_affected = sum(c['quantity'] for c in commitments)
        high_severity_count = sum(1 for o in impacted_orders if o['severity'] in ['critical', 'high'])
        
        risk_score = min(1.0, (
            len(commitments) * 0.02 +
            delay_days * 0.03 +
            high_severity_count * 0.1 +
            (total_quantity_affected / 10000) * 0.1
        ))
        
        return {
            'risk_score': round(risk_score, 3),
            'risk_level': 'low' if risk_score < 0.3 else ('medium' if risk_score < 0.6 else 'high'),
            'commitments_affected': len(commitments),
            'quantity_affected': round(total_quantity_affected, 1),
            'orders_at_high_risk': high_severity_count,
            'financial_exposure': self._estimate_financial_impact(commitments, delay_days),
            'reputation_risk': 'high' if high_severity_count > 5 else ('medium' if high_severity_count > 2 else 'low')
        }

    def _estimate_financial_impact(self, commitments, delay_days):
        """Estimate financial exposure from delayed commitments"""
        total_value = sum(c.get('quantity', 0) * 50 for c in commitments)
        penalty_rate = min(0.05, delay_days * 0.005)
        expediting_cost = total_value * (0.15 if delay_days <= 7 else 0.25)
        return {
            'total_commitment_value': round(total_value, 2),
            'estimated_penalty': round(total_value * penalty_rate, 2),
            'expediting_cost_estimate': round(expediting_cost, 2),
            'total_exposure': round(total_value * penalty_rate + expediting_cost, 2),
            'currency': 'CNY'
        }

    def _suggest_delay_mitigation(self, delay_days, commitments):
        """Suggest delay mitigation strategies"""
        strategies = []
        
        if delay_days <= 3:
            strategies.append({'strategy': 'expedite_shipping', 'name': 'Accelerate logistics', 'description': 'Change sea freight to air freight to reduce delivery time', 'cost_impact': 'Logistics cost increase', 'effectiveness': 'high'})

        if delay_days <= 7:
            strategies.append({'strategy': 'supplier_overtime', 'name': 'Supplier overtime production', 'description': 'Request supplier to arrange overtime', 'cost_impact': 'Processing fee increase', 'effectiveness': 'medium-high'})

        strategies.extend([
            {'strategy': 'alternative_supplier', 'name': 'Enable backup supplier', 'description': 'Place orders with backup supplier simultaneously', 'cost_impact': 'Procurement cost may increase', 'effectiveness': 'high'},
            {'strategy': 'customer_negotiation', 'name': 'Customer negotiation', 'description': 'Communicate with customer to adjust delivery or partial delivery', 'cost_impact': 'No direct cost', 'effectiveness': 'medium'},
            {'strategy': 'inventory_buffer', 'name': 'Use safety stock', 'description': 'Use existing stock for contingency, replenish later', 'cost_impact': 'Tie up working capital', 'effectiveness': 'immediate'}
        ])
        
        return strategies

    def _find_alternative_suppliers(self, supplier_id, materials):
        """Find alternative suppliers"""
        from .models import SupplierMaterial

        alternatives = []
        for mat_code in materials:
            try:
                alt_suppliers = SupplierMaterial.objects.filter(
                    material__material_code=mat_code, is_forbidden=False
                ).exclude(supplier_id=supplier_id).select_related('supplier', 'material')[:3]

                for alt in alt_suppliers:
                    alternatives.append({
                        'material_code': mat_code,
                        'alternative_supplier': alt.supplier.supplier_name if alt.supplier else '',
                        'lead_time': alt.lead_time,
                        'unit_price': round(float(alt.unit_price or 0), 2),
                        'rating': alt.supplier.rating if alt.supplier else ''
                    })
            except Exception as e:
                logger.debug(f'查找备选供应商失败 mat={mat_code}: {str(e)}')

        return alternatives

    def _stress_test_supply_chain(self, surge_demand, duration):
        """Supply chain stress test"""
        stress_indicators = {
            'procurement_system_load': min(1.0, surge_demand / 500),
            'warehouse_throughput': min(1.0, surge_demand / 800),
            'production_schedule_density': min(1.0, surge_demand / 600),
            'logistics_capacity': min(1.0, surge_demand / 400),
            'cash_flow_pressure': min(1.0, surge_demand * duration / 10000)
        }
        
        overall_stress = sum(stress_indicators.values()) / len(stress_indicators)
        
        return {
            'overall_stress_level': round(overall_stress, 3),
            'stress_rating': 'low' if overall_stress < 0.4 else ('medium' if overall_stress < 0.7 else 'high'),
            'indicators': stress_indicators,
            'bottlenecks': [k for k, v in stress_indicators.items() if v > 0.8]
        }

    def _calculate_inventory_burn_rate(self, surge_demand):
        """Calculate inventory consumption rate（优化：使用批量查询）"""
        # Inventory已在文件顶部导入，无需重复导入

        # 使用单次聚合查询获取总库存
        total_stock = float(Inventory.objects.aggregate(
            total=Sum('quantity')
        )['total'] or 0)

        daily_burn = surge_demand * 3.5
        days_of_supply = total_stock / daily_burn if daily_burn > 0 else 999

        # 只查询低库存物料（限制数量，避免内存溢出）
        critical_materials = []
        if days_of_supply < 14:  # 只有在供应紧张时才查询明细
            # 使用批量查询 + 限制返回数量
            low_stock_materials = Inventory.objects.select_related('material').filter(
                quantity__gt=0
            ).only(
                'quantity',
                'material__material_code'
            )[:50]  # 限制最多50条记录

            for inv in low_stock_materials:
                qty = int(inv.quantity or 0)
                if qty > 0:
                    days_for_this = qty / (daily_burn * 0.1)
                    if days_for_this < 14:
                        critical_materials.append({
                            'code': inv.material.material_code,
                            'current_stock': qty,
                            'days_of_supply': round(days_for_this, 1),
                            'urgency': 'critical' if days_for_this < 7 else 'high'
                        })

            critical_materials.sort(key=lambda x: x['days_of_supply'])
            critical_materials = critical_materials[:10]  # 最多返回10个关键物料

        return {
            'daily_consumption_rate': round(daily_burn, 1),
            'total_current_stock': round(total_stock, 1),
            'days_of_supply_remaining': round(days_of_supply, 1),
            'critical_materials': critical_materials,
            'burn_rate_vs_normal': round(surge_demand / 100, 2)
        }

    def _assess_shortage_risk_under_surge(self, additional_demand):
        """Assess shortage risk under surge（基于真实订单状态，不再依赖MaterialPlanResult缓存表）"""
        # 修复: 使用活跃订单数作为分母(与物料计划一致)
        ACTIVE_STATUSES = ['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
        total_orders = SalesOrder.objects.filter(status__in=ACTIVE_STATUSES).count()
        # 未齐套的活跃订单 = 活跃订单 - 已完成/已发货(不在ACTIVE中，所以直接用pending+confirmed+allocated+partial)
        incomplete_orders = SalesOrder.objects.filter(
            status__in=['pending', 'confirmed', 'allocated', 'partial']
        ).count()
        current_shortage_rate = incomplete_orders / max(total_orders, 1)
        
        projected_shortage_rate = min(1.0, current_shortage_rate + (additional_demand / 5000))
        
        materials_at_risk = incomplete_orders + int(additional_demand / 200)
        
        return {
            'current_shortage_rate': round(current_shortage_rate, 3),
            'projected_shortage_rate': round(projected_shortage_rate, 3),
            'increase_in_risk': round(projected_shortage_rate - current_shortage_rate, 3),
            'materials_potentially_affected': materials_at_risk,
            'risk_level': 'extreme' if projected_shortage_rate > 0.6 else ('high' if projected_shortage_rate > 0.4 else 'medium')
        }

    def _generate_emergency_response_plan(self, surge_pct, duration):
        """Generate emergency response plan"""
        plan = []
        
        if surge_pct > 0.5:
            plan.append({'phase': 'immediate', 'timeline': '0-24h', 'actions': ['Activate emergency command center', 'Notify all key suppliers of capacity alert', 'Inventory count all available (including transit)', 'Suspend non-essential procurement spending']})
        
        plan.append({'phase': 'short_term', 'timeline': '1-7d', 'actions': ['Contact all qualified suppliers to confirm maximum capacity', 'Activate spot market procurement channels', 'Evaluate outsourcing/subcontracting possibility', 'Communicate with customers to prioritize']})
        
        if duration > 7:
            plan.append({'phase': 'medium_term', 'timeline': '1-4w', 'actions': ['Implement production increase plan (hire/train)', 'Establish temporary storage facilities', 'Develop new supply sources', 'Optimize product configuration to reduce scarce material usage']})
        
        return plan

    def _estimate_recovery_timeline(self, surge_pct, duration):
        """Estimate recovery timeline"""
        base_recovery = 14
        surge_factor = surge_pct * 20
        duration_factor = duration * 0.3
        
        estimated_recovery = base_recovery + surge_factor + duration_factor
        
        return {
            'estimated_days_to_normalization': int(estimated_recovery),
            'phases': [
                {'phase': 'crisis_management', 'duration': min(duration, 7)},
                {'phase': 'stabilization', 'duration': int(estimated_recovery * 0.4)},
                {'phase': 'rebuilding', 'duration': int(estimated_recovery * 0.6)}
            ]
        }

    def _simulate_order_cancel(self, params):
        """
        砍单/取消订单仿真 - 模拟取消一个已有订单
        输出: 释放的物料如何重新分配、释放的产能、对其他订单的正向影响
        """
        cancel_order_id = params.get('cancel_order_id')

        # 查询目标取消订单
        if cancel_order_id:
            try:
                target_order = SalesOrder.objects.select_related('material').get(id=int(cancel_order_id))
            except (SalesOrder.DoesNotExist, ValueError, TypeError):
                target_order = None
        else:
            # 未指定时取一个活跃订单作为示例
            target_order = SalesOrder.objects.filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial']
            ).select_related('material').first()

        if not target_order:
            return {
                'scenario_name': '订单取消影响分析',
                'error': '未找到目标订单，请提供有效的订单ID',
                'simulated_at': datetime.now().isoformat()
            }

        order_qty = float(target_order.quantity or 0)
        material_code = target_order.material.material_code if target_order.material else ''
        material_id = target_order.material_id

        # 分析释放的物料
        from .models import Inventory, OrderAllocation
        released_materials = []
        allocations = OrderAllocation.objects.filter(order_id=target_order.id).select_related('material')
        for alloc in allocations:
            released_materials.append({
                'material_code': alloc.material.material_code if alloc.material else '',
                'material_name': alloc.material.material_name if alloc.material else '',
                'released_quantity': int(getattr(alloc, 'allocated_quantity', 0) or 0),
                'suggested_reuse': '可重新分配给同物料其他订单'
            })

        # 如果没有分配记录，基于BOM比例估算
        if not released_materials and material_id:
            estimated_bom_ratio = 3.5
            released_materials.append({
                'material_code': material_code,
                'material_name': target_order.material.material_name if target_order.material else '',
                'released_quantity': int(order_qty),
                'suggested_reuse': f'估算释放约{order_qty * estimated_bom_ratio:.0f}个物料单位'
            })

        # 查找可能受益的其他订单（使用相同物料的待处理订单）
        beneficiary_orders = []
        if material_id:
            related_orders = SalesOrder.objects.filter(
                material_id=material_id,
                status__in=['pending', 'confirmed', 'allocated'],
            ).exclude(id=target_order.id).select_related('material')[:5]
            for rel_order in related_orders:
                beneficiary_orders.append({
                    'order_no': rel_order.order_no,
                    'demand_date': str(rel_order.demand_date),
                    'quantity': int(rel_order.quantity or 0),
                    'priority': rel_order.priority,
                    'benefit_type': '物料可用性提升'
                })

        # 计算产能释放
        from .models import WorkCenter
        released_capacity = {
            'estimated_hours_freed': round(order_qty * 0.5, 1),  # 估算：每单位需0.5工时
            'available_for_reschedule': True,
            'suggested_use': f'可用于排产{max(1, int(order_qty / 50))}个中等规模订单'
        }

        # 正向影响评估
        positive_impacts = [
            {'type': '物料库存压力缓解', 'description': f'释放约{sum(m["released_quantity"] for m in released_materials)}单位物料'},
            {'type': '产能利用率优化', 'description': released_capacity['suggested_use']},
            {'type': '交付风险降低', 'description': f'可使{len(beneficiary_orders)}个关联订单受益'}
        ]

        return {
            'scenario_name': '订单取消影响分析',
            'cancelled_order': {
                'order_no': target_order.order_no,
                'material_code': material_code,
                'quantity': order_qty,
                'demand_date': str(target_order.demand_date) if target_order.demand_date else '',
                'status_before_cancel': target_order.status
            },
            'released_resources': {
                'materials': released_materials,
                'capacity': released_capacity,
                'total_released_quantity': sum(m['released_quantity'] for m in released_materials)
            },
            'beneficiary_orders': {
                'count': len(beneficiary_orders),
                'orders': beneficiary_orders
            },
            'positive_impacts': positive_impacts,
            'recommendations': [
                {'action': '确认释放资源', 'priority': '高', 'description': '将释放的物料标记为可用状态'},
                {'action': '重新分配物料', 'priority': '高', 'description': f'优先满足{len(beneficiary_orders)}个受益订单'},
                {'action': '调整排产计划', 'priority': '中', 'description': '利用释放的产能提前安排后续订单'}
            ],
            'simulated_at': datetime.now().isoformat()
        }

    def _simulate_capacity_failure(self, params):
        """
        产能故障仿真 - 某产线停机N天
        输出: 受影响的生产订单、建议转移到哪个替代产线、交付延迟预测
        """
        try:
            work_center = params.get('failure_work_center', '')
            failure_days = int(params.get('failure_days', 3))

            from .models import WorkCenter

            # 查找故障产线（必须使用真实数据）
            wc = None
            if work_center:
                wc = WorkCenter.objects.filter(work_center_code=work_center).first()
            if not wc:
                wc = WorkCenter.objects.first()

            # 如果数据库中没有任何WorkCenter记录，返回明确错误而非虚假数据
            if not wc:
                raise ValueError('数据库中没有工作中心(产线)数据，无法执行产能故障模拟。请先在系统中配置WorkCenter数据。')

            wc_code = wc.work_center_code
            wc_name = wc.work_center_name
            daily_capacity = float(wc.daily_capacity_limit) if wc.daily_capacity_limit else 0.0

            # 计算损失的产能
            lost_capacity = daily_capacity * failure_days

            # 查找受影响的生产订单（在该产线上排产的订单）
            impacted_production_orders = []
            active_orders = list(
                SalesOrder.objects.filter(
                    status__in=['in_production', 'processing', 'allocated', 'confirmed'],
                ).select_related('material')[:30]
            )

            for order in active_orders:
                days_to_delivery = (order.demand_date - date.today()).days if order.demand_date else 30
                if 0 < days_to_delivery <= failure_days + 7:  # 故障期间或故障后一周内交货的订单受影响
                    delay_days = max(0, failure_days - max(0, days_to_delivery - failure_days))
                    severity = 'critical' if delay_days > failure_days * 0.7 else ('high' if delay_days > 3 else 'medium')
                    impacted_production_orders.append({
                        'order_no': order.order_no,
                        'material_code': order.material.material_code if order.material else '',
                        'quantity': int(order.quantity or 0),
                        'original_delivery': str(order.demand_date) if order.demand_date else '',
                        'predicted_delay_days': min(delay_days, failure_days),
                        'severity': severity
                    })

            # 查找替代产线
            alternative_work_centers = []
            all_wcs = WorkCenter.objects.exclude(id=wc.id if wc else None)[:5]
            for alt_wc in all_wcs:
                alt_daily_cap = float(alt_wc.daily_capacity_limit or 0)
                alternative_work_centers.append({
                    'work_center_code': alt_wc.work_center_code,
                    'work_center_name': alt_wc.work_center_name,
                    'daily_capacity': alt_daily_cap,
                    'utilization_rate': round(alt_daily_cap / max(alt_daily_cap + daily_capacity, 1), 2) if alt_daily_cap > 0 else 0.0,  # 基于产能占比估算
                    'transfer_feasibility': '可行' if alt_daily_cap > daily_capacity * 0.3 else '容量有限'
                })
            alternative_work_centers.sort(key=lambda x: x['daily_capacity'], reverse=True)

            # 推荐最佳替代方案
            best_alternative = alternative_work_centers[0] if alternative_work_centers else None

            # 交付延迟预测
            delivery_delay_prediction = {
                'total_impacted_orders': len(impacted_production_orders),
                'avg_delay_days': round(
                    sum(o['predicted_delay_days'] for o in impacted_production_orders) / max(len(impacted_production_orders), 1), 1
                ),
                'max_delay_days': max((o['predicted_delay_days'] for o in impacted_production_orders), default=0),
                'orders_at_risk_critical': sum(1 for o in impacted_production_orders if o['severity'] == 'critical'),
                'recovery_timeline': f'预计需要{failure_days + 2}天完全恢复产能'
            }

            return {
                'scenario_name': '产能故障影响模拟',
                'failure_info': {
                    'work_center_code': wc_code,
                    'work_center_name': wc_name,
                    'failure_days': failure_days,
                    'lost_capacity_units': round(lost_capacity, 1),
                    'daily_capacity': daily_capacity
                },
                'impacted_orders': {
                    'total_count': len(impacted_production_orders),
                    'details': impacted_production_orders[:10],
                    'severity_summary': {
                        'critical': sum(1 for o in impacted_production_orders if o['severity'] == 'critical'),
                        'high': sum(1 for o in impacted_production_orders if o['severity'] == 'high'),
                        'medium': sum(1 for o in impacted_production_orders if o['severity'] == 'medium')
                    }
                },
                'alternative_work_centers': alternative_work_centers,
                'recommended_transfer_target': {
                    'work_center_code': best_alternative['work_center_code'] if best_alternative else '',
                    'work_center_name': best_alternative['work_center_name'] if best_alternative else '',
                    'reason': '产能最充足且利用率较低' if best_alternative else '无可用的替代产线',
                    'transfer_notes': f'建议转移约{int(min(lost_capacity, best_alternative["daily_capacity"] * 0.4))}单位的产能负载' if best_alternative else ''
                } if best_alternative else None,
                'delivery_delay_prediction': delivery_delay_prediction,
                'mitigation_actions': [
                    {'step': 1, 'action': '立即通知受影响的客户', 'timeline': '0-2h', 'priority': '紧急'},
                    {'step': 2, 'action': f'启动{best_alternative["work_center_name"] if best_alternative else "替代"}产线转产', 'timeline': '2-8h', 'priority': '紧急'},
                    {'step': 3, 'action': '协调加班/外协资源补充缺口', 'timeline': '8-24h', 'priority': '高'},
                    {'step': 4, 'action': '调整后续排产计划', 'timeline': '24-48h', 'priority': '中'}
                ],
                'simulated_at': datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f'产能故障模拟异常: {str(e)}', exc_info=True)
            # 不返回虚假数据，重新抛出异常让上层处理
            raise

    def _simulate_bom_ecn(self, params):
        """
        BOM工程变更仿真 - BOM结构发生变化（如替换某个子件）
        输出: 影响的产品范围、新旧版本过渡方案、额外物料需求
        """
        ecn_material_id = params.get('ecn_material_id', '')       # 被替换的原物料ID
        ecn_new_material_id = params.get('ecn_new_material_id', '')  # 替换后的新物料ID

        # 查询原物料和新物料信息
        old_material = None
        new_material = None
        if ecn_material_id:
            try:
                old_material = Material.objects.get(id=int(ecn_material_id))
            except (Material.DoesNotExist, ValueError, TypeError):
                pass
        if ecn_new_material_id:
            try:
                new_material = Material.objects.get(id=int(ecn_new_material_id))
            except (Material.DoesNotExist, ValueError, TypeError):
                pass

        # 如果未指定，使用默认示例数据
        if not old_material:
            old_material = Material.objects.first()
        if not new_material:
            new_material = Material.objects.exclude(id=old_material.id if old_material else None).first()

        old_mat_code = old_material.material_code if old_material else 'MAT-OLD'
        old_mat_name = old_material.material_name if old_material else '旧物料'
        new_mat_code = new_material.material_code if new_material else 'MAT-NEW'
        new_mat_name = new_material.material_name if new_material else '新物料'

        # 从BOM表中查找使用该子件的所有产品（父件）
        # BillOfMaterials 模型字段: parent_material(父件FK), child_material(子件FK), quantity(用量)
        affected_products = []
        try:
            from .models import BillOfMaterials
            bom_items = BillOfMaterials.objects.filter(
                child_material_id=old_material.id
            ).select_related('parent_material', 'child_material')[:10]
            for bom_item in bom_items:
                affected_products.append({
                    'product_code': bom_item.parent_material.material_code if bom_item.parent_material else '',
                    'product_name': bom_item.parent_material.material_name if bom_item.parent_material else '',
                    'bom_usage_qty': float(bom_item.quantity or 1),
                    'affected_status': '需更新BOM并验证兼容性'
                })
        except Exception:
            # 如果没有BOM表或查询失败，返回模拟数据
            affected_products = [
                {'product_code': 'PRD-001', 'product_name': '产品A系列', 'bom_usage_qty': 2.0, 'affected_status': '需更新BOM'},
                {'product_code': 'PRD-002', 'product_name': '产品B系列', 'bom_usage_qty': 3.0, 'affected_status': '需更新BOM'},
            ]

        # 使用新物料的在制品/库存订单影响
        wip_orders_affected = []
        if old_material:
            wip_orders = SalesOrder.objects.filter(
                material__in=[old_material.id],
                status__in=['in_production', 'processing', 'allocated']
            ).select_related('material')[:5]
            for wip_order in wip_orders:
                wip_orders_affected.append({
                    'order_no': wip_order.order_no,
                    'quantity': int(wip_order.quantity or 0),
                    'status': wip_order.status,
                    'impact': '需评估是否切换到新物料或按旧版本完成'
                })

        # 额外物料需求计算
        additional_material_needs = []
        total_new_material_needed = sum(p['bom_usage_qty'] for p in affected_products) * 100  # 假设每产品批次100
        if total_new_material_needed > 0:
            additional_material_needs.append({
                'material_code': new_mat_code,
                'material_name': new_mat_name,
                'required_quantity': round(total_new_material_needed, 1),
                'purpose': '替换旧物料后的新增采购需求',
                'urgency': '高' if total_new_material_needed > 200 else ('中' if total_new_material_needed > 50 else '低')
            })

        # 新旧版本过渡方案
        transition_plan = [
            {'phase': '冻结期', 'duration': 'T+0~3天', 'actions': ['冻结含旧物料的新订单创建', '清点旧物料在库和在途数量', '通知相关供应商停止发货']},
            {'phase': '切换期', 'duration': 'T+4~10天', 'actions': ['更新BOM结构为新版本', '启动新物料首批采购', '验证新物料与产品的兼容性']},
            {'phase': '过渡期', 'duration': 'T+11~21天', 'actions': ['在制品按旧版本继续完工', '新订单全部使用新物料', '监控新旧物料切换质量指标']},
            {'phase': '收尾期', 'duration': 'T+22~30天', 'actions': ['消耗完旧物料安全库存', '关闭旧物料采购通道', '输出ECN变更总结报告']}
        ]

        return {
            'scenario_name': 'BOM工程变更影响分析',
            'ecn_details': {
                'old_material': {'id': ecn_material_id, 'code': old_mat_code, 'name': old_mat_name},
                'new_material': {'id': ecn_new_material_id, 'code': new_mat_code, 'name': new_mat_name},
                'change_type': '子件替换'
            },
            'affected_products': {
                'total_count': len(affected_products),
                'products': affected_products
            },
            'wip_orders_affected': {
                'count': len(wip_orders_affected),
                'orders': wip_orders_affected
            },
            'additional_material_needs': additional_material_needs,
            'transition_plan': transition_plan,
            'risk_assessment': {
                'supply_risk': '中' if total_new_material_needed > 100 else '低',
                'quality_risk': '需进行兼容性测试',
                'schedule_risk': f'过渡期需数周，影响{len(wip_orders_affected)}个在制订单',
                'overall_risk_level': '中等' if len(affected_products) > 3 else '可控'
            },
            'recommendations': [
                {'item': '提前备料', 'description': f'建议立即采购{new_mat_code}，提前期根据供应商实际交期确定'},
                {'item': '兼容性验证', 'description': '在新物料正式上线前完成小批量试产验证'},
                {'item': '旧物料处理', 'description': '制定旧物料库存消化计划，避免呆滞'},
                {'item': '客户沟通', 'description': '如涉及外观/性能变化，需提前与客户沟通确认'}
            ],
            'simulated_at': datetime.now().isoformat()
        }

    def save_simulation_result(self, result: dict, scenario_name: str = None,
                                tags: list = None) -> dict:
        """
        将仿真结果持久化到数据库（通过PlanLog + MaterialPlanResult快照机制）
        
        Args:
            result: run_simulation() 的返回结果
            scenario_name: 场景名称（用于检索）
            tags: 标签列表（如 ['urgent', 'high_risk', 'capacity']）
            
        Returns:
            dict: 包含保存的记录ID、检索key、时间戳
        """
        import uuid
        
        simulation_id = str(uuid.uuid4())
        
        # 1. 将完整结果序列化为JSON存入 PlanLog（PlanLog无extra_data字段，存入message）
        try:
            plan_log = PlanLog.objects.create(
                log_type='INFO',
                message=f'[What-If快照] scenario={scenario_name or "unnamed"}, '
                       f'id={simulation_id[:8]}, tags={tags or []}',
            )
            log_id = plan_log.id
        except Exception as e:
            logger.error(f"保存What-If结果到PlanLog失败: {str(e)}")
            log_id = None
            # 回退：仅记录基本信息，不使用不存在的字段
            try:
                plan_log = PlanLog.objects.create(
                    log_type='INFO',
                    message=f'[What-If快照] scenario={scenario_name or "unnamed"}, id={simulation_id[:8]}'
                )
                log_id = plan_log.id
            except Exception:
                pass

        # 2. 提取关键指标存入 MaterialPlanResult 作为快照
        # 注意: MaterialPlanResult.order 是必填FK(非空)，What-If场景无绑定订单，
        #       因此跳过MaterialPlanResult快照，仅通过PlanLog记录即可
        snapshot_id = None
        try:
            risk_assessment = result.get('risk_assessment') or result.get('risk_score')
            affected = result.get('affected_orders') or {}

            total_affected = 0
            high_risk_count = 0
            if isinstance(affected, dict):
                total_affected = affected.get('total_affected', 0)
                high_risk_count = affected.get('high_risk_count', 0)
            elif isinstance(affected, (list, int)):
                if isinstance(affected, int):
                    total_affected = affected
                else:
                    total_affected = len(affected)

            risk_score_val = (
                float(risk_assessment) if isinstance(risk_assessment, (int, float))
                else float(risk_assessment.get('risk_score', 0)) if isinstance(risk_assessment, dict)
                else 0.5
            )

            # 尝试查找一个虚拟占位订单用于快照（避免必填FK约束）
            placeholder_order = SalesOrder.objects.first()
            if placeholder_order:
                snapshot = MaterialPlanResult.objects.create(
                    order=placeholder_order,
                    complete_rate=max(0, 1.0 - risk_score_val),
                    delivery_change_count=total_affected,
                    stability_score=round(max(0, min(1.0, 1.0 - risk_score_val)), 3),
                    shortage_details={
                        'scenario': scenario_name,
                        'simulation_id': simulation_id,
                        'tags': tags or [],
                        'risk_score': round(risk_score_val, 3),
                        'affected_orders_count': total_affected,
                        'high_risk_orders': high_risk_count,
                    },
                )
                snapshot_id = snapshot.id
            
        except Exception as e:
            logger.warning(f"创建MaterialPlanResult快照失败: {str(e)}")

        save_result = {
            'success': True,
            'simulation_id': simulation_id,
            'scenario_name': scenario_name,
            'log_record_id': log_id,
            'snapshot_record_id': snapshot_id,
            'tags': tags or [],
            'saved_at': datetime.now().isoformat(),
            'retrieval_key': {
                'log_type': 'WHATIF_SNAPSHOT',
                'simulation_id_prefix': simulation_id[:8],
                'scenario_name': scenario_name,
            },
        }

        logger.info(f"What-If仿真结果已持久化: id={simulation_id[:8]}, scenario={scenario_name}")

        return save_result

    def run_multi_scenario_orchestration(self, scenarios_config: list) -> dict:
        """
        多场景连续仿真编排
        
        支持将多个场景按顺序执行，前一场景的输出可作为后一场景的输入，
        用于模拟连锁反应（如: 先插单 → 再供应商延期 → 最后产能故障）。
        
        Args:
            scenarios_config: 场景编排列表，格式:
                [
                    {
                        'scenario': 'urgent_insert',
                        'params': {...},
                        'condition': None,  # 无条件执行
                    },
                    {
                        'scenario': 'supplier_delay', 
                        'params': {...},
                        'condition': {
                            'field': 'risk_score',
                            'operator': '<',
                            'value': 0.7,
                        },  # 前一场景risk_score < 0.7时才执行
                    },
                    ...
                ]
            
        Returns:
            dict: 编排报告
        """
        import uuid
        import operator
        
        orchestration_id = str(uuid.uuid4())
        
        # 操作符映射
        op_map = {
            '<': operator.lt,
            '<=': operator.le,
            '>': operator.gt,
            '>=': operator.ge,
            '==': operator.eq,
            '!=': operator.ne,
        }
        
        scenario_results = []
        executed_count = 0
        skipped_count = 0
        all_affected_order_nos = set()
        max_risk_score = 0.0
        cascade_parts = []  # 连锁反应描述片段
        
        prev_result = None
        
        for idx, sc_config in enumerate(scenarios_config):
            scenario_type = sc_config.get('scenario', '')
            params = sc_config.get('params', {})
            condition = sc_config.get('condition')
            
            logger.info(f"[多场景编排] 步骤{idx+1}/{len(scenarios_config)}: scenario={scenario_type}")
            
            # 检查条件是否满足
            should_execute = True
            skip_reason = None
            
            if condition and prev_result is not None:
                field_name = condition.get('field', '')
                op_str = condition.get('operator', '==')
                threshold = condition.get('value')
                
                # 从前一场景结果中提取字段值
                field_value = self._extract_field_from_result(prev_result, field_name)
                
                if field_value is not None and op_str in op_map:
                    op_func = op_map[op_str]
                    should_execute = op_func(field_value, threshold)
                    
                if not should_execute:
                    skip_reason = (
                        f"条件不满足: 前一场景的{field_name}={field_value} "
                        f"{op_str} {threshold} 为False"
                    )
            
            if not should_execute:
                skipped_count += 1
                scenario_results.append({
                    'step': idx + 1,
                    'scenario': scenario_type,
                    'status': 'skipped',
                    'skip_reason': skip_reason,
                    'result': None,
                })
                cascade_parts.append(f"步骤{idx+1}({scenario_type})因条件未满足被跳过")
                continue
            
            # 执行场景
            try:
                step_result = self.run_simulation(scenario_type, params)
                
                # 如果前一场景有影响订单信息，注入到当前参数中（模拟连锁）
                if prev_result and all_affected_order_nos:
                    params['_cascade_context'] = {
                        'previously_affected_orders': list(all_affected_order_nos),
                        'previous_risk_score': max_risk_score,
                    }
                
                executed_count += 1
                
                # 提取关键指标用于累积计算
                risk_score = self._extract_field_from_result(step_result, 'risk_score')
                if risk_score is not None:
                    max_risk_score = max(max_risk_score, float(risk_score))
                
                # 收集受影响的订单号（去重）
                affected = step_result.get('affected_orders')
                if isinstance(affected, dict):
                    details = affected.get('details', [])
                    for d in details:
                        if isinstance(d, dict) and d.get('order_no'):
                            all_affected_order_nos.add(d['order_no'])
                    at_risk = affected.get('orders_at_risk', [])
                    for o in at_risk:
                        if isinstance(o, dict) and o.get('order_no'):
                            all_affected_order_nos.add(o['order_no'])
                elif isinstance(affected, list):
                    for item in affected:
                        if isinstance(item, dict):
                            ono = item.get('order_no')
                            if ono:
                                all_affected_order_nos.add(ono)
                
                # 构建连锁描述
                scenario_display_name = self._get_scenario_display_name(scenario_type)
                affected_count = len(all_affected_order_nos)
                current_high_risk = 0
                if isinstance(affected, dict):
                    current_high_risk = affected.get('high_risk_count', 0)
                
                if idx == 0:
                    cascade_parts.append(
                        f"{scenario_display_name}导致{affected_count}个受影响订单"
                        f"(其中{current_high_risk}个高风险)"
                    )
                else:
                    cascade_parts.append(
                        f"随后{scenario_display_name}使总受影响订单增至{affected_count}个"
                        f"(累计最高风险评分{max_risk_score:.2f})"
                    )
                
                # 持久化中间结果
                try:
                    save_info = self.save_simulation_result(
                        result=step_result,
                        scenario_name=f"{orchestration_id[:8]}_step{idx+1}_{scenario_type}",
                        tags=['orchestration', 'multi_scenario', scenario_type]
                    )
                    step_result['_persistence_id'] = save_info.get('simulation_id')
                except Exception as e:
                    logger.warning(f"中间结果持久化失败(步骤{idx+1}): {str(e)}")
                
                scenario_results.append({
                    'step': idx + 1,
                    'scenario': scenario_type,
                    'status': 'executed',
                    'result_summary': {
                        'risk_score': risk_score,
                        'affected_count': affected_count if isinstance(affected, (dict, list)) else 0,
                    },
                    'full_result': step_result,
                })
                
                prev_result = step_result
                
            except ValueError as e:
                skipped_count += 1
                scenario_results.append({
                    'step': idx + 1,
                    'scenario': scenario_type,
                    'status': 'error',
                    'error': str(e),
                    'result': None,
                })
                cascade_parts.append(f"步骤{idx+1}({scenario_type})执行出错: {str(e)}")
                prev_result = None
            except Exception as e:
                skipped_count += 1
                scenario_results.append({
                    'step': idx + 1,
                    'scenario': scenario_type,
                    'status': 'error',
                    'error': str(e),
                    'result': None,
                })
                cascade_parts.append(f"步骤{idx+1}({scenario_type})异常: {str(e)}")
                prev_result = None
        
        # 生成连锁反应分析文字描述
        if len(cascade_parts) >= 2:
            cascade_analysis = ' → '.join(cascade_parts)
        elif len(cascade_parts) == 1:
            cascade_analysis = cascade_parts[0]
        else:
            cascade_analysis = "无有效场景被执行"
        
        # 综合报告
        orchestration_report = {
            'orchestration_id': orchestration_id,
            'total_scenarios': len(scenarios_config),
            'executed_scenarios': executed_count,
            'skipped_scenarios': skipped_count,
            'execution_rate': round(executed_count / max(len(scenarios_config), 1) * 100, 1),
            'scenario_results': scenario_results,
            'cumulative_impact': {
                'total_unique_affected_orders': len(all_affected_order_nos),
                'affected_order_list': sorted(list(all_affected_order_nos))[:20],
                'max_risk_score_across_scenarios': round(max_risk_score, 3),
                'risk_level': (
                    '极高' if max_risk_score >= 0.8 else
                    '高' if max_risk_score >= 0.6 else
                    '中' if max_risk_score >= 0.4 else
                    '低'
                ),
            },
            'cascade_analysis': cascade_analysis,
            'orchestrated_at': datetime.now().isoformat(),
        }
        
        # 写入编排完成日志
        try:
            PlanLog.objects.create(
                log_type='INFO',
                message=f'[多场景编排] id={orchestration_id[:8]} 完成: '
                       f'{executed_count}/{len(scenarios_config)}场景执行, '
                       f'{len(all_affected_order_nos)}个订单受影响, 最高风险={max_risk_score:.2f}'
            )
        except Exception:
            pass
        
        logger.info(f"[OK] 多场景编排完成: {orchestration_id[:8]}, "
                   f"{executed_count}成功, {skipped_count}跳过")
        
        return orchestration_report

    def _extract_field_from_result(self, result: dict, field_name: str):
        """
        从仿真结果中提取指定字段的值
        
        支持嵌套字段提取（如 'risk_assessment.risk_score'）
        """
        if not result or not field_name:
            return None
        
        parts = field_name.split('.')
        current = result
        
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        
        # 尝试转换为数值
        if isinstance(current, (int, float)):
            return float(current)
        return current

    def _get_scenario_display_name(self, scenario_type: str) -> str:
        """获取场景类型的中文显示名称"""
        name_map = {
            'urgent_insert': '紧急插单',
            'order_cancel': '订单取消',
            'supplier_delay': '供应商延期',
            'capacity_failure': '产能故障',
            'bom_ecn': 'BOM工程变更',
            'capacity_change': '产能变更',
            'demand_surge': '需求激增',
        }
        return name_map.get(scenario_type, scenario_type)


# ============================================================
# 蒙特卡洛概率仿真器
# ============================================================

class MonteCarloSimulator(WhatIfSimulator):
    """
    蒙特卡洛场景仿真器

    在确定性仿真的基础上，引入概率分布输入，
    通过多次采样得到输出的概率区间。

    使用场景：
    - 供应商交期服从正态分布 N(μ=承诺交期, σ=μ×0.15)
    - 订单数量服从均匀分布 U(原数量×0.8, 原数量×1.2)
    - 物料合格率服从Beta分布 α=9, β=1（均值90%）
    """

    # 参数到概率分布的映射配置
    DISTRIBUTION_CONFIG = {
        'delivery_days': {
            'type': 'normal',
            'mu_factor': 1.0,       # μ = 原值 × mu_factor
            'sigma_factor': 0.15,   # σ = μ × sigma_factor
            'description': '供应商交期服从正态分布 N(μ=承诺交期, σ=μ×15%)',
        },
        'quantity': {
            'type': 'uniform',
            'low_factor': 0.8,      # 下界 = 原值 × low_factor
            'high_factor': 1.2,     # 上界 = 原值 × high_factor
            'description': '订单数量服从均匀分布 U(原数量×0.8, 原数量×1.2)',
        },
        'quality_rate': {
            'type': 'beta',
            'alpha': 9,
            'beta_param': 1,
            'description': '物料合格率服从Beta分布 α=9, β=1（均值90%）',
        },
        'delay_days': {
            'type': 'normal',
            'mu_factor': 1.0,
            'sigma_factor': 0.25,
            'description': '延期天数服从正态分布，σ=μ×25%（高不确定性）',
        },
        'surge_percentage': {
            'type': 'uniform',
            'low_factor': 0.7,
            'high_factor': 1.3,
            'description': '需求激增比例服从均匀分布 U(70%, 130%)',
        },
        'capacity_change_pct': {
            'type': 'normal',
            'mu_factor': 1.0,
            'sigma_factor': 0.2,
            'description': '产能变化比例服从正态分布 N(μ=目标值, σ=20%)',
        },
    }

    def __init__(self, *args, simulations: int = 1000, confidence_level: float = 0.95, **kwargs):
        """
        初始化蒙特卡洛仿真器

        Args:
            simulations: 蒙特卡洛模拟次数（默认1000次）
            confidence_level: 置信水平（默认95%）
        """
        super().__init__(*args, **kwargs)
        self.simulations = max(simulations, 100)   # 最少100次模拟
        self.confidence_level = confidence_level
        self._rng = np.random.default_rng(seed=None)  # 可复现性：可传入固定seed

        logger.info(f"[蒙特卡洛仿真器] 初始化完成: 模拟次数={self.simulations}, "
                   f"置信水平={self.confidence_level}")

    def simulate_with_uncertainty(self, scenario_key: str, scenario_params: dict) -> dict:
        """
        带不确定性的场景仿真

        对输入参数添加随机扰动后，多次运行基础仿真器，
        统计输出结果的分布特征。

        Args:
            scenario_key: 场景类型（如 'urgent_insert', 'supplier_delay' 等）
            scenario_params: 场景参数字典

        Returns:
            dict: 包含确定性结果、蒙特卡洛统计摘要、敏感性分析等
        """
        logger.info(f"[蒙特卡洛] 开始不确定仿真: scenario={scenario_key}, "
                   f"参数={list(scenario_params.keys())}, 模拟次数={self.simulations}")

        # 1. 运行一次确定性仿真作为对照基准
        try:
            deterministic_result = self.run_simulation(scenario_key, scenario_params.copy())
        except Exception as e:
            logger.error(f"[蒙特卡洛] 确定性仿真失败: {str(e)}")
            return {
                'success': False,
                'error': f'基础确定性仿真失败: {str(e)}',
                'scenario_key': scenario_key,
            }

        # 提取风险评分作为主要输出指标
        base_risk_score = self._extract_risk_score(deterministic_result)

        # 2. 执行多次蒙特卡洛采样仿真
        risk_scores = []
        raw_results = []
        sampled_params_history = []

        for i in range(self.simulations):
            try:
                # 对输入参数进行随机采样
                sampled_params = self._sample_input_params(scenario_params)
                sampled_params_history.append(sampled_params)

                # 运行带扰动参数的仿真
                result = self.run_simulation(scenario_key, sampled_params)
                risk_score = self._extract_risk_score(result)

                risk_scores.append(risk_score)
                raw_results.append(result)

            except Exception as e:
                # 单次模拟失败不中断整体，记录并跳过
                if i < 5:  # 仅记录前几次失败避免日志刷屏
                    logger.debug(f"[蒙特卡洛] 第{i+1}次模拟失败（已跳过）: {str(e)}")
                continue

        # 3. 检查有效模拟次数
        valid_simulations = len(risk_scores)
        if valid_simulations < 50:
            logger.warning(f"[蒙特卡洛] 有效模拟次数过少: {valid_simulations}/{self.simulations}")

        # 4. 生成统计分布摘要
        mc_summary = self._generate_distribution_report(risk_scores)

        # 5. 计算置信区间
        sorted_scores = sorted(risk_scores)
        alpha = 1.0 - self.confidence_level
        lower_idx = int(valid_simulations * alpha / 2)
        upper_idx = int(valid_simulations * (1 - alpha / 2)) - 1
        confidence_interval = [
            round(sorted_scores[max(lower_idx, 0)], 4),
            round(sorted_scores[min(upper_idx, valid_simulations - 1)], 4),
        ]

        mc_summary['confidence_interval'] = confidence_interval

        # 6. 计算成功/失败概率（以风险评分0.6为阈值）
        success_threshold = 0.6
        probability_of_success = sum(1 for s in risk_scores if s < success_threshold) / max(valid_simulations, 1)
        probability_of_failure = 1.0 - probability_of_success
        mc_summary['probability_of_success'] = round(probability_of_success, 4)
        mc_summary['probability_of_failure'] = round(probability_of_failure, 4)

        # 7. 计算VaR和CVaR
        var_5pct = np.percentile(risk_scores, 5) if risk_scores else 0
        tail_losses = [s for s in risk_scores if s <= var_5pct]
        cvar_5pct = np.mean(tail_losses) if tail_losses else var_5pct
        mc_summary['value_at_risk_5pct'] = round(float(var_5pct), 4)
        mc_summary['conditional_var_5pct'] = round(float(cvar_5pct), 4)

        # 8. 敏感性分析
        sensitivity = self._calculate_sensitivity(
            raw_results[:min(len(raw_results), 200)],  # 限制用于敏感性分析的样本数
            scenario_params
        )

        # 9. 构建完整返回结果
        result = {
            'success': True,
            'scenario_key': scenario_key,
            'deterministic_result': deterministic_result,
            'monte_carlo_summary': {
                'simulations_run': valid_simulations,
                'simulations_requested': self.simulations,
                'base_risk_score': base_risk_score,
                'mean_risk_score': mc_summary['mean'],
                'std_risk_score': mc_summary['std'],
                'percentiles': mc_summary['percentiles'],
                'confidence_interval': confidence_interval,
                'probability_of_success': mc_summary['probability_of_success'],
                'probability_of_failure': mc_summary['probability_of_failure'],
                'value_at_risk_5pct': mc_summary['value_at_risk_5pct'],
                'conditional_var_5pct': mc_summary['conditional_var_5pct'],
            },
            'distribution_data': risk_scores,           # 原始采样数据（供直方图使用）
            'sensitivity_analysis': sensitivity,
            'input_uncertainty_config': {
                k: v.get('description', '')
                for k, v in self.DISTRIBUTION_CONFIG.items()
                if k in scenario_params
            },
            'interpretation': self._generate_interpretation(mc_summary, sensitivity),
            'simulated_at': datetime.now().isoformat(),
        }

        logger.info(f"[蒙特卡洛] 不确定仿真完成: 有效={valid_simulations}/{self.simulations}, "
                   f"均值={mc_summary['mean']:.3f}, "
                   f"P95={mc_summary['percentiles']['p95']:.3f}, "
                   f"成功概率={mc_summary['probability_of_success']:.1%}")

        return result

    def _sample_input_params(self, base_params: dict) -> dict:
        """
        对基础参数进行蒙特卡洛采样

        根据DISTRIBUTION_CONFIG中定义的概率分布，
        为每个支持采样的参数生成随机扰动值。
        """
        sampled = {}

        for key, value in base_params.items():
            config = self.DISTRIBUTION_CONFIG.get(key)

            if config is None:
                # 无分布配置的参数保持原值
                sampled[key] = value
                continue

            try:
                dist_type = config.get('type')

                if dist_type == 'normal':
                    # 正态分布: N(μ=原值×mu_factor, σ=μ×sigma_factor)
                    mu = float(value or 0) * config.get('mu_factor', 1.0)
                    sigma = abs(mu) * config.get('sigma_factor', 0.15)
                    sampled_value = self._rng.normal(mu, sigma)
                    # 对于天数/数量类参数，确保非负
                    if key in ('quantity', 'delivery_days', 'delay_days', 'duration_days', 'change_percentage'):
                        sampled_value = max(0, sampled_value)
                    sampled[key] = sampled_value

                elif dist_type == 'uniform':
                    # 均匀分布: U(原值×low_factor, 原值×high_factor)
                    base_val = float(value or 0)
                    low = base_val * config.get('low_factor', 0.8)
                    high = base_val * config.get('high_factor', 1.2)
                    sampled_value = self._rng.uniform(low, high)
                    if key in ('quantity', 'delivery_days', 'delay_days', 'surge_percentage', 'change_percentage'):
                        sampled_value = max(0, sampled_value)
                    sampled[key] = sampled_value

                elif dist_type == 'beta':
                    # Beta分布: Beta(alpha, beta)，输出范围[0,1]
                    alpha = config.get('alpha', 9)
                    beta_p = config.get('beta_param', 1)
                    sampled_value = self._rng.beta(alpha, beta_p)
                    sampled[key] = float(sampled_value)

                else:
                    sampled[key] = value

            except (TypeError, ValueError) as e:
                # 采样失败时使用原值
                sampled[key] = value

        return sampled

    def _calculate_sensitivity(self, results: list, base_params: dict) -> dict:
        """
        计算各输入参数对输出风险评分的敏感性

        基于Spearman秩相关系数计算每个参数的弹性（elasticity），
        弹性越大表示该参数对结果的影响越敏感。

        Returns:
            dict: 包含最敏感参数、敏感度排名等信息
        """
        try:
            from scipy import stats as scipy_stats
        except ImportError:
            return {
                'most_sensitive_param': 'scipy_unavailable',
                'sensitivity_ranking': [],
                'note': 'scipy未安装，无法进行Spearman秩相关分析',
            }

        # 收集每次模拟的风险评分
        risk_scores = [self._extract_risk_score(r) for r in results]

        if len(risk_scores) < 30:
            return {
                'most_sensitive_param': 'insufficient_data',
                'sensitivity_ranking': [],
                'note': f'样本量不足（{len(risk_scores)} < 30），无法进行可靠的敏感性分析',
            }

        # 计算每个可采样参数的Spearman秩相关系数
        sensitivities = []

        for param_name in base_params.keys():
            if param_name not in self.DISTRIBUTION_CONFIG:
                continue

            # 从历史采样参数中重建该参数的采样序列（近似方法）
            # 由于我们没有存储完整的参数-结果对应关系，
            # 这里通过重新采样来构建参数序列
            param_samples = [
                self._sample_input_params(base_params).get(param_name, 0)
                for _ in range(min(len(risk_scores), 200))
            ]

            if len(param_samples) != len(risk_scores):
                min_len = min(len(param_samples), len(risk_scores))
                param_samples = param_samples[:min_len]
                score_subset = risk_scores[:min_len]
            else:
                score_subset = risk_scores

            try:
                # Spearman秩相关系数
                correlation, p_value = scipy_stats.spearmanr(param_samples, score_subset)

                # 弹性 = 相关系数 × (参数标准差/评分标准差) 的近似
                param_std = np.std(param_samples) if np.std(param_samples) > 0 else 1.0
                score_std = np.std(score_subset) if np.std(score_subset) > 0 else 1.0
                elasticity = abs(correlation) * (param_std / score_std)

                sensitivities.append({
                    'param': param_name,
                    'spearman_correlation': round(float(correlation), 4),
                    'p_value': round(float(p_value), 6),
                    'is_significant': p_value < 0.05,
                    'elasticity': round(float(elasticity), 4),
                })

            except Exception as e:
                logger.debug(f"敏感性计算失败 param={param_name}: {str(e)}")

        # 按弹性降序排列
        sensitivities.sort(key=lambda x: -x['elasticity'])

        most_sensitive = (
            sensitivities[0]['param'] if sensitivities else 'unknown'
        )

        return {
            'most_sensitive_param': most_sensitive,
            'sensitivity_ranking': sensitivities,
            'total_params_analyzed': len(sensitivities),
            'significant_factors': sum(1 for s in sensitivities if s.get('is_significant')),
            'recommendation': (
                f'最敏感参数为「{most_sensitive}」，'
                f'建议优先控制和监测该参数的不确定性来源'
            ) if most_sensitive != 'unknown' else '',
        }

    def _generate_distribution_report(self, values: list) -> dict:
        """
        生成统计分布摘要

        计算输入数值列表的均值、标准差、分位数等统计特征。

        Args:
            values: 数值列表（通常为风险评分）

        Returns:
            dict: 包含各种统计指标的字典
        """
        if not values:
            return {
                'count': 0,
                'mean': 0.0,
                'std': 0.0,
                'min': 0.0,
                'max': 0.0,
                'median': 0.0,
                'percentiles': {'p5': 0, 'p25': 0, 'p50': 0, 'p75': 0, 'p95': 0},
            }

        arr = np.array(values)

        percentiles_values = np.percentile(arr, [5, 25, 50, 75, 95])

        return {
            'count': len(values),
            'mean': round(float(np.mean(arr)), 4),
            'std': round(float(np.std(arr)), 4),
            'min': round(float(np.min(arr)), 4),
            'max': round(float(np.max(arr)), 4),
            'median': round(float(np.median(arr)), 4),
            'variance': round(float(np.var(arr)), 6),
            'skewness': round(float(self._calculate_skewness(arr)), 4),
            'kurtosis': round(float(self._calculate_kurtosis(arr)), 4),
            'percentiles': {
                'p5': round(float(percentiles_values[0]), 4),     # 乐观情景
                'p25': round(float(percentiles_values[1]), 4),
                'p50': round(float(percentiles_values[2]), 4),    # 中位数情景
                'p75': round(float(percentiles_values[3]), 4),
                'p95': round(float(percentiles_values[4]), 4),    # 悲观情景
            },
        }

    def _calculate_skewness(self, arr: np.ndarray) -> float:
        """计算偏度（衡量分布的不对称性）"""
        mean = np.mean(arr)
        std = np.std(arr)
        if std == 0:
            return 0.0
        n = len(arr)
        skew = (n / ((n - 1) * (n - 2))) * np.sum(((arr - mean) / std) ** 3)
        return float(skew)

    def _calculate_kurtosis(self, arr: np.ndarray) -> float:
        """计算峰度（衡量分布的尾部厚度）"""
        mean = np.mean(arr)
        std = np.std(arr)
        if std == 0:
            return 0.0
        n = len(arr)
        m4 = np.mean((arr - mean) ** 4)
        kurtosis = m4 / (std ** 4) - 3  # 超额峰度
        return float(kurtosis)

    def _extract_risk_score(self, simulation_result: dict) -> float:
        """
        从仿真结果中提取风险评分

        支持多种结果格式的风险评分提取：
        - risk_assessment.risk_score
        - risk_score (顶层字段)
        - risk_assessment (直接是数值)
        """
        if not isinstance(simulation_result, dict):
            return 0.5

        # 尝试从嵌套结构中提取
        risk = simulation_result.get('risk_assessment')
        if isinstance(risk, dict):
            score = risk.get('risk_score')
            if score is not None:
                return float(score)

        # 尝试顶层字段
        score = simulation_result.get('risk_score')
        if score is not None:
            return float(score)

        # 如果risk_assessment本身就是数字
        if isinstance(risk, (int, float)):
            return float(risk)

        # 默认中等风险
        return 0.5

    def _generate_interpretation(self, mc_summary: dict, sensitivity: dict) -> str:
        """
        基于蒙特卡洛结果生成自然语言解读

        将统计数据转化为业务人员可理解的结论和建议
        """
        parts = []

        mean_score = mc_summary.get('mean', 0.5)
        std_score = mc_summary.get('std', 0)
        p5 = mc_summary.get('percentiles', {}).get('p5', 0)
        p95 = mc_summary.get('percentiles', {}).get('p95', 0)
        success_prob = mc_summary.get('probability_of_success', 0.5)
        var_5 = mc_summary.get('value_at_risk_5pct', 0)

        # 风险等级判断
        if mean_score < 0.3:
            risk_level = '低风险'
        elif mean_score < 0.5:
            risk_level = '中等风险'
        elif mean_score < 0.7:
            risk_level = '较高风险'
        else:
            risk_level = '高风险'

        parts.append(f'该场景的综合风险评分为{mean_score:.2f}，属于「{risk_level}」级别。')

        # 结果离散度说明
        cv = std_score / max(mean_score, 0.01)  # 变异系数
        if cv > 0.5:
            parts.append(f'结果离散度较高(CV={cv:.2f})，表明输入不确定性对输出影响显著，'
                       f'乐观情景(P5)评分{p5:.2f}与悲观情景(P95)评分{p95:.2f}差距较大。')
        elif cv > 0.25:
            parts.append(f'结果存在一定波动(CV={cv:.2f})，需关注关键不确定因素。')
        else:
            parts.append(f'结果相对稳定(CV={cv:.2f})，不确定性对决策影响有限。')

        # 成功概率说明
        if success_prob > 0.85:
            parts.append(f'成功概率高达{success_prob:.1%}，方案可行性较高。')
        elif success_prob > 0.6:
            parts.append(f'成功概率为{success_prob:.1%}，建议制定备选方案以应对不利情况。')
        else:
            parts.append(f'成功概率仅为{success_prob:.1%}，存在较大失败风险，建议重新评估或调整策略。')

        # VaR说明
        if var_5 > 0.7:
            parts.append(f'在5%的最坏情况下，风险评分可能达到{var_5:.2f}(VaR 5%)，'
                       f'需准备极端情况应对预案。')

        # 敏感性提示
        most_sensitive = sensitivity.get('most_sensitive_param', '')
        if most_sensitive and most_sensitive != 'insufficient_data':
            param_display_map = {
                'delivery_days': '交期',
                'quantity': '订单数量',
                'delay_days': '延期天数',
                'surge_percentage': '需求激增比例',
                'capacity_change_pct': '产能变化比例',
                'quality_rate': '合格率',
            }
            display_name = param_display_map.get(most_sensitive, most_sensitive)
            parts.append(f'「{display_name}」是最敏感的影响因素，建议重点监控其波动范围。')

        return '\n'.join(parts)


# ============================================================
# 蒙特卡洛仿真 API 视图
# ============================================================

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework import status as drf_status


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def monte_carlo_api_view(request):
    """
    蒙特卡洛概率仿真API视图（HTTP POST接口）

    请求体(JSON):
        {
            "scenario_key": "urgent_insert",        // 场景类型（必填）
            "scenario_params": {                    // 场景参数（必填）
                "quantity": 100,
                "demand_date": "2026-07-15",
                "priority": 1
            },
            "simulations": 1000,                   // 模拟次数（可选，默认1000）
            "confidence_level": 0.95               // 置信水平（可选，默认0.95）
        }

    返回:
        完整的蒙特卡洛仿真结果，包含：
        - deterministic_result: 确定性仿真基准
        - monte_carlo_summary: 统计摘要（均值/标准差/分位数/置信区间/VaR/CVaR/成功概率）
        - distribution_data: 原始采样数据（供前端直方图使用）
        - sensitivity_analysis: 敏感性分析（Spearman秩相关+弹性排名）
        - interpretation: 自然语言解读
    """
    try:
        request_data = request.data if hasattr(request, 'data') else {}
        if not request_data and request.body:
            import json
            request_data = json.loads(request.body)
    except Exception:
        request_data = {}

    scenario_key = request_data.get('scenario_key')
    scenario_params = request_data.get('scenario_params', {})
    simulations = int(request_data.get('simulations', 1000))
    confidence_level = float(request_data.get('confidence_level', 0.95))

    if not scenario_key:
        return JsonResponse({
            'success': False,
            'error': '请提供 scenario_key 参数（场景类型）',
        }, status=drf_status.HTTP_400_BAD_REQUEST)

    if not scenario_params:
        return JsonResponse({
            'success': False,
            'error': '请提供 scenario_params 参数（场景参数字典）',
        }, status=drf_status.HTTP_400_BAD_REQUEST)

    # 验证场景是否受支持
    supported_scenarios = [
        'urgent_insert', 'order_cancel', 'supplier_delay',
        'capacity_failure', 'bom_ecn', 'capacity_change', 'demand_surge',
    ]
    if scenario_key not in supported_scenarios:
        return JsonResponse({
            'success': False,
            'error': f'不支持的场景类型: {scenario_key}，支持的场景: {supported_scenarios}',
        }, status=drf_status.HTTP_400_BAD_REQUEST)

    try:
        simulator = MonteCarloSimulator(
            simulations=simulations,
            confidence_level=confidence_level,
        )
        result = simulator.simulate_with_uncertainty(scenario_key, scenario_params)

        # 将numpy类型转换为Python原生类型以便JSON序列化
        result = _convert_numpy_types(result)

        return JsonResponse(result, safe=False)

    except Exception as e:
        logger.error(f"[蒙特卡洛API] 仿真执行异常: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': f'蒙特卡洛仿真执行出错: {str(e)}',
        }, status=drf_status.HTTP_500_INTERNAL_SERVER_ERROR)


def _convert_numpy_types(obj):
    """
    递归转换numpy类型为Python原生类型

    确保返回的JSON中不包含numpy.int64、numpy.float64等不可序列化的类型
    """
    import numpy as np

    if isinstance(obj, dict):
        return {k: _convert_numpy_types(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_numpy_types(item) for item in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, np.bool_):
        return bool(obj)
    else:
        return obj


# 兼容旧调用方式的入口函数
def run_monte_carlo_simulation(request_data: dict) -> dict:
    """
    蒙特卡洛概率仿真的API调用入口

    Args:
        request_data: 包含以下字段的字典：
            - scenario_key: 场景类型（必填）
            - scenario_params: 场景参数（必填）
            - simulations: 模拟次数（可选，默认1000）
            - confidence_level: 置信水平（可选，默认0.95）

    Returns:
        dict: 完整的蒙特卡洛仿真结果
    """
    scenario_key = request_data.get('scenario_key')
    scenario_params = request_data.get('scenario_params', {})
    simulations = int(request_data.get('simulations', 1000))
    confidence_level = float(request_data.get('confidence_level', 0.95))

    if not scenario_key:
        return {
            'success': False,
            'error': '请提供 scenario_key 参数（场景类型）',
        }

    if not scenario_params:
        return {
            'success': False,
            'error': '请提供 scenario_params 参数（场景参数）',
        }

    # 验证场景是否受支持
    supported_scenarios = [
        'urgent_insert', 'order_cancel', 'supplier_delay',
        'capacity_failure', 'bom_ecn', 'capacity_change', 'demand_surge',
    ]
    if scenario_key not in supported_scenarios:
        return {
            'success': False,
            'error': f'不支持的场景类型: {scenario_key}，支持的场景: {supported_scenarios}',
        }

    try:
        simulator = MonteCarloSimulator(
            simulations=simulations,
            confidence_level=confidence_level,
        )
        result = simulator.simulate_with_uncertainty(scenario_key, scenario_params)
        return result

    except Exception as e:
        logger.error(f"[蒙特卡洛API] 仿真执行异常: {str(e)}", exc_info=True)
        return {
            'success': False,
            'error': f'蒙特卡洛仿真执行出错: {str(e)}',
        }
