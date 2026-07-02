"""
AI Prediction and Intelligent Analysis API
Provides: Demand forecasting, anomaly detection, intelligent recommendations, What-If simulation
"""

import json
from datetime import datetime, timedelta, date
from collections import defaultdict
import logging

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required  # 新增：用于API认证
from django.http import JsonResponse

logger = logging.getLogger(__name__)

from django.db import models as django_models
import numpy as np

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from ..ai_engine import (
    DemandForecaster,
    AnomalyDetector,
    IntelligentDecisionEngine,
    get_ai_engine,
    run_demand_prediction,
    run_anomaly_detection,
    generate_intelligent_recommendations,
    _ensure_native_dict
)
from ..material_planning import MaterialPlanner, MultiObjectiveOptimizer
from ..what_if_scenarios import WhatIfSimulator
from ..models import SalesOrder, Material, MaterialPlanResult, PlanLog, Inventory


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def ai_demand_forecast(request):
    """
    Demand Forecasting API
    
    GET: Get latest forecast results (if cached)
    POST: Trigger new prediction calculation
    
    Parameters:
        - days: Forecast days (default 30)
        - material_id: Specific material ID (optional)
        - force_retrain: Whether to retrain model (true/false)
    """
    try:
        if request.method == 'POST':
            days = int(request.data.get('days', 30))
            material_id = request.data.get('material_id')
            force_retrain_val = request.data.get('force_retrain', False)
            force_retrain = bool(force_retrain_val) or str(force_retrain_val).lower() == 'true'

            # 输入验证：限制预测天数范围
            if not (1 <= days <= 90):
                return Response({
                    'success': False,
                    'error': '预测天数必须在1-90天之间',
                    'supported_range': '1-90天'
                }, status=status.HTTP_400_BAD_REQUEST)

            # 获取AI引擎（force_retrain时强制刷新单例，避免使用内存中的旧损坏模型）
            engine = get_ai_engine(force_reload=force_retrain)
            forecaster = engine.demand_forecaster

            # Train or retrain model
            if force_retrain or not forecaster.is_trained:
                train_result = forecaster.train(force_retrain=force_retrain)
                if not train_result.get('success'):
                    logger.warning(f'模型训练失败: {train_result}')
                    return Response({
                        'success': False,
                        'error': '模型训练失败，请检查数据完整性',
                        'details': f'原因: {train_result.get("reason", "未知")}'
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # Execute prediction (带零结果检测和自动重试)
            max_retries = 1
            result = None
            for attempt in range(max_retries + 1):
                try:
                    if material_id:
                        # Material-level prediction（验证material_id）
                        try:
                            material_id_int = int(material_id)
                            if not Material.objects.filter(id=material_id_int).exists():
                                return Response({
                                    'success': False,
                                    'error': f'物料ID {material_id} 不存在',
                                    'suggestion': '请检查物料ID是否正确'
                                }, status=status.HTTP_400_BAD_REQUEST)
                        except (ValueError, TypeError):
                            return Response({
                                'success': False,
                                'error': '物料ID格式错误，必须为数字'
                            }, status=status.HTTP_400_BAD_REQUEST)

                        result = forecaster.predict_material_requirements(
                            material_id=material_id_int,
                            future_days=days
                        )
                    else:
                        result = forecaster.predict(future_days=days)

                    # 零结果检测：如果预测值全为0，强制刷新引擎重试
                    if result.get('success') and attempt < max_retries:
                        fc_list = result.get('forecast', [])
                        if fc_list:
                            total_demand = sum(r.get('predicted_demand', 0) for r in fc_list)
                            if total_demand == 0:
                                logger.warning(f'预测结果全为零(第{attempt+1}次)，强制刷新AI引擎重试')
                                engine = get_ai_engine(force_reload=True)
                                forecaster = engine.demand_forecaster
                                forecaster.train(force_retrain=True)
                                continue

                    break  # 结果正常或已用完重试次数，退出循环

                except Exception as pred_err:
                    logger.error(f'需求预测执行异常(第{attempt+1}次): {type(pred_err).__name__}: {pred_err}')
                    if attempt < max_retries:
                        engine = get_ai_engine(force_reload=True)
                        forecaster = engine.demand_forecaster
                        continue
                    return Response({
                        'success': False,
                        'error': f'预测执行失败: {type(pred_err).__name__}',
                        'details': str(pred_err)[:200]
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # 后处理：补充summary中前端期望的字段 + 确保所有类型可JSON序列化
            _enrich_forecast_summary(result)
            result = _ensure_native_dict(result)

            return Response(result)

        else:  # GET request
            return Response({
                'message': 'Please use POST request to trigger prediction calculation',
                'parameters': {
                    'days': 'Forecast days(1-90)',
                    'material_id': 'Optional - Specific material ID',
                    'force_retrain': 'Whether to force retraining (true/false)'
                }
            })
    
    except Exception as e:
        # 安全：不泄露详细的异常堆栈信息
        logger.error(f'Demand Forecast API error: {str(e)}', exc_info=True)
        PlanLog.objects.create(
            log_type='ERROR',
            message=f'需求预测API异常: {type(e).__name__}'
        )
        return Response({
            'success': False,
            'error': '需求预测服务暂时不可用，请稍后重试或联系系统管理员'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ai_anomaly_detection(request):
    """
    Anomaly Detection API
    
    Detects anomaly patterns in material allocation and inventory levels
    
    Parameters:
        - detection_type: Detection type ('allocation' | 'inventory' | 'all')
    """
    try:
        detection_type = request.data.get('detection_type', 'all')
        engine = get_ai_engine()
        
        results = {}
        
        if detection_type in ['allocation', 'all']:
            allocations = _get_allocations_for_detection()
            allocation_result = engine.anomaly_detector.detect_allocation_anomalies(allocations)
            results['allocation_anomalies'] = allocation_result
        
        if detection_type in ['inventory', 'all']:
            inventory_data = _get_inventory_for_detection()
            inventory_result = engine.anomaly_detector.detect_inventory_anomalies(inventory_data)
            results['inventory_anomalies'] = inventory_result
        
        total_anomalies = sum(
            len(r.get('anomalies', []))
            for r in results.values()
        )
        
        results['summary'] = {
            'total_anomalies_detected': total_anomalies,
            'detection_types_checked': list(results.keys()),
            'timestamp': datetime.now().isoformat(),
            'risk_level': 'low' if total_anomalies < 5 else ('medium' if total_anomalies < 15 else 'high')
        }
        
        PlanLog.objects.create(
            log_type='INFO',
            message=f'Anomaly Detection completed: Found {total_anomalies} anomalies'
        )
        
        return Response({
            'success': True,
            **results
        })

    except Exception as e:
        # 安全：不泄露详细异常信息
        logger.error(f'Anomaly Detection API error: {str(e)}', exc_info=True)
        return Response({
            'success': False,
            'error': '异常检测服务暂时不可用，请稍后重试'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ai_comprehensive_analysis(request):
    """
    Comprehensive Analysis API
    
    Executes complete AI analysis pipeline:
    1. Demand forecasting
    2. Anomaly detection
    3. Risk assessment
    4. Generate intelligent recommendations
    
    Parameters:
        - include_prediction: Whether to include demand forecasting (default true)
        - generate_action_plan: Whether to generate procurement action plan (default false)
    """
    try:
        include_prediction_val = request.data.get('include_prediction', True)
        generate_action_plan_val = request.data.get('generate_action_plan', False)
        include_prediction = bool(include_prediction_val) if include_prediction_val is not None else True
        generate_action_plan = bool(generate_action_plan_val) or str(generate_action_plan_val).lower() == 'true'
        
        engine = get_ai_engine()
        
        analysis_report = engine.run_comprehensive_analysis(
            include_prediction=include_prediction
        )
        
        if generate_action_plan:
            # 使用MaterialPlanner生成缺料报告（使用最近执行时的策略，保持一致性）
            from ..material_planning import MaterialPlanner
            from ..utils.safe_cache import safe_get as _safe_get

            strategy_info = _safe_get('latest_planning_strategy') or {}
            consumption_priority = strategy_info.get('consumption_priority', 'FIFO')

            planner = MaterialPlanner(consumption_priority=consumption_priority)
            shortage_report = {'material_shortages': []}
            try:
                planner.load_material_info_cache()
                planner.load_supplier_info_cache()
                from ..models import SalesOrder, Inventory
                from django.db.models import Sum
                orders = SalesOrder.objects.filter(
                    status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
                ).select_related('material')[:50]
                for order in orders:
                    stock = float(Inventory.objects.filter(
                        material_id=order.material_id
                    ).aggregate(total=Sum('quantity'))['total'] or 0)
                    order_demand = float(order.quantity or 0)
                    shortage_qty = max(0, order_demand - stock)
                    if shortage_qty > 0:
                        result = planner.analyze_shortage(order, [{
                            'material_id': order.material_id,
                            'required': order_demand,
                            'allocated': stock,
                            'shortage': shortage_qty
                        }])
                        if result and result.get('material_shortages'):
                            shortage_report['material_shortages'].extend(result['material_shortages'])
            except Exception as shortage_err:
                logger.warning(f'生成缺料报告失败（可跳过）: {shortage_err}')

            prediction = analysis_report.get('components', {}).get('demand_prediction')
            action_plan = engine.generate_procurement_action_plan(shortage_report, prediction)
            analysis_report['action_plan'] = action_plan
        
        return Response({
            'success': True,
            'report': analysis_report
        })

    except Exception as e:
        # 安全：不泄露详细异常信息
        logger.error(f'Comprehensive Analysis API error: {str(e)}', exc_info=True)
        PlanLog.objects.create(
            log_type='ERROR',
            message=f'综合分析API异常: {type(e).__name__}'
        )
        return Response({
            'success': False,
            'error': '综合分析服务暂时不可用，请稍后重试'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def whatif_simulation(request):
    """
    What-If Scenario Simulation API
    
    Supports five simulation scenarios:
    1. Urgent insert order - 紧急插单影响分析
    2. Order cancel - 砍单/取消订单影响
    3. Supplier delay - 供应商延期评估
    4. Capacity failure - 产能故障模拟
    5. BOM ECN - BOM工程变更影响
    
    Returns comprehensive impact scores and decision recommendations.
    """
    try:
        scenario = request.data.get('scenario') or request.data.get('scenario_type')
        parameters = request.data.get('parameters', {})

        # 支持的7种仿真场景
        supported_scenarios = [
            'urgent_insert',      # 紧急插单
            'order_cancel',       # 砍单/取消订单
            'supplier_delay',     # 供应商延期
            'capacity_failure',   # 产能故障
            'bom_ecn',            # BOM工程变更
            'capacity_change',    # 产能变化分析
            'demand_surge',       # 需求激增压力测试
        ]

        if scenario not in supported_scenarios:
            return Response({
                'success': False,
                'error': f'不支持的场景类型: {scenario}',
                'supported_scenarios': [
                    'urgent_insert - 紧急插单（高优先级订单插入）',
                    'order_cancel - 订单取消（释放物料与产能）',
                    'supplier_delay - 供应商延期（交期延后评估）',
                    'capacity_failure - 产能故障（产线停机模拟）',
                    'bom_ecn - BOM工程变更（子件替换影响）',
                    'capacity_change - 产能变化分析（增减产能影响评估）',
                    'demand_surge - 需求激增压力测试'
                ]
            }, status=status.HTTP_400_BAD_REQUEST)

        simulator = WhatIfSimulator()
        simulation_result = simulator.run_simulation(scenario, parameters)

        # 后处理：补充前端期望的字段（兼容前端WhatIfResult接口）
        _ensure_whatif_frontend_compat(simulation_result)

        PlanLog.objects.create(
            log_type='INFO',
            message=f'What-If Simulation completed: Scenario={scenario}'
        )

        return Response({
            'success': True,
            'scenario': scenario,
            'parameters': parameters,
            'result': simulation_result
        })

    except Exception as e:
        # 安全：不泄露详细异常信息
        logger.error(f'What-If Simulation API error: {str(e)}', exc_info=True)
        return Response({
            'success': False,
            'error': '模拟服务暂时不可用，请检查参数是否正确后重试'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _ensure_whatif_frontend_compat(result):
    """
    确保WhatIf模拟结果包含前端期望的字段。
    基于模拟器返回的真实数据生成合理的默认值（非虚构数据）。
    前端 WhatIfResult 接口需要:
      - overall_impact_score (综合影响评分 0~1)
      - recommendations (建议列表, 每项含 action/reason/steps)
      - decision_support (决策支持: can_accept/accept_conditionally/should_decline)
    """
    if not isinstance(result, dict):
        return

    # 1. overall_impact_score: 从各场景真实数据映射计算
    if 'overall_impact_score' not in result:
        ra = result.get('risk_assessment')
        if isinstance(ra, dict) and 'risk_score' in ra:
            result['overall_impact_score'] = float(ra['risk_score'])
        elif isinstance(ra, (int, float)):
            result['overall_impact_score'] = float(ra)
        # capacity_failure: 从 lost_capacity_units + impacted_orders 映射
        elif 'failure_info' in result and 'lost_capacity_units' in result.get('failure_info', {}):
            lost = result['failure_info'].get('lost_capacity_units', 0)
            impacted = result.get('impacted_orders', {})
            total = impacted.get('total_count', 0) if isinstance(impacted, dict) else 0
            critical = impacted.get('severity_summary', {}).get('critical', 0) if isinstance(impacted, dict) else 0
            if lost > 0 or total > 0 or critical > 0:
                score = min(1.0, (lost / 10000.0) * 0.5 + (total / 50.0) * 0.3 + (critical / 10.0) * 0.2)
                result['overall_impact_score'] = round(score, 4)
        # order_cancel: 从 beneficiary_orders.count + released_resources 映射
        elif 'beneficiary_orders' in result:
            count = result.get('beneficiary_orders', {}).get('count', 0) if isinstance(result.get('beneficiary_orders'), dict) else 0
            released = result.get('released_resources', {}).get('total_released_quantity', 0) if isinstance(result.get('released_resources'), dict) else 0
            if count > 0 or released > 0:
                score = min(1.0, (count / 20.0) * 0.5 + (released / 10000.0) * 0.5)
                result['overall_impact_score'] = round(score, 4)
        # supplier_delay: 从 risk_assessment.risk_level 或 impacted_commitments 数量
        elif 'impacted_commitments' in result:
            ic = result.get('impacted_commitments', [])
            count = len(ic) if isinstance(ic, list) else 0
            delay_days = result.get('input_parameters', {}).get('delay_days', 7)
            if count > 0:
                score = min(1.0, (count / 30.0) * 0.6 + (delay_days / 60.0) * 0.4)
                result['overall_impact_score'] = round(score, 4)
        # bom_ecn: 从 affected_products.total_count 映射
        elif 'affected_products' in result:
            ap = result.get('affected_products', {})
            total_count = ap.get('total_count', 0) if isinstance(ap, dict) else 0
            additional = result.get('additional_material_needs', [])
            need_count = len(additional) if isinstance(additional, list) else 0
            if total_count > 0 or need_count > 0:
                score = min(1.0, (total_count / 50.0) * 0.5 + (need_count / 20.0) * 0.3 + 0.15)
                result['overall_impact_score'] = round(score, 4)
        # capacity_change: 从 delivery_impact 或 production_impact 映射
        elif 'delivery_impact' in result:
            di = result.get('delivery_impact', {})
            delayed = di.get('orders_potentially_delayed', 0) if isinstance(di, dict) else 0
            wc = result.get('work_center', {})
            change_pct = abs(wc.get('change_percentage', 0)) if isinstance(wc, dict) else 0
            if delayed > 0 or change_pct > 0:
                score = min(1.0, (delayed / 100.0) * 0.5 + (change_pct / 100.0) * 0.35 + 0.1)
                result['overall_impact_score'] = round(score, 4)
        # demand_surge: 从 supply_chain_stress 或 shortage_risk_assessment 映射
        elif 'supply_chain_stress' in result:
            scs = result.get('supply_chain_stress', {})
            stress_level = scs.get('stress_level', '') if isinstance(scs, dict) else ''
            surge_pct = result.get('input_parameters', {}).get('surge_percentage', 50)
            if stress_level:
                level_map = {'low': 0.25, 'medium': 0.5, 'high': 0.75, 'critical': 0.95}
                score = level_map.get(str(stress_level).lower(), min(surge_pct / 100.0, 1.0))
                result['overall_impact_score'] = round(score, 4)

    # 2. recommendations: 始终将各场景的建议数据归一化为标准格式
    #    标准格式: {action: 'ACCEPT|NEGOTIATE|DECLINE', reason: str, steps: list}
    recs = []
    existing_raw = result.get('recommendations', []) if isinstance(result.get('recommendations', []), list) else []

    # === 归一化已有 recommendations (处理不同场景的字段差异) ===
    for r in existing_raw[:6]:
        if not isinstance(r, dict):
            continue
        action_raw = r.get('action', '')
        reason = r.get('reason') or r.get('description') or r.get('item') or r.get('strategy') or ''
        steps = r.get('steps') or r.get('implementation_steps') or []

        # 判断 action 是标准值还是中文描述
        if action_raw in ('ACCEPT', 'DECLINE', 'NEGOTIATE', 'RETRY'):
            normalized_action = action_raw
        else:
            # 中文 action 或 item 字段（如 '提前备料'/'确认释放资源'），统一映射为 ACCEPT
            normalized_action = 'ACCEPT'
            if not reason and isinstance(action_raw, str) and action_raw not in ('ACCEPT', 'DECLINE', 'NEGOTIATE'):
                reason = action_raw  # 中文 action 作为 reason

        if reason or steps:
            recs.append({
                'action': normalized_action,
                'reason': reason,
                'steps': steps if isinstance(steps, list) else [],
                'confidence': r.get('confidence', '中'),
                'priority': r.get('priority', 'P2'),
                'description': r.get('description', reason),
            })

    # === 从 reallocation_suggestions 映射 (urgent_insert场景) ===
    for sug in result.get('reallocation_suggestions', []):
        recs.append({
            'action': 'ACCEPT',
            'reason': sug.get('description', sug.get('strategy', '')),
            'steps': [],
            'confidence': '高' if str(sug.get('feasibility', '')).startswith('高') else
                    ('中' if str(sug.get('feasibility', '')).startswith('中') else '低'),
            'priority': 'P1',
            'description': sug.get('description', ''),
        })

    # === 从 mitigation_strategies 映射 (supplier_delay等场景) ===
    for ms in result.get('mitigation_strategies', []):
        recs.append({
            'action': 'NEGOTIATE',
            'reason': ms.get('description', ms.get('strategy', '')),
            'steps': ms.get('steps', []) if isinstance(ms.get('steps'), list) else [],
            'confidence': '中',
            'priority': 'P2',
            'description': ms.get('description', ''),
        })

    # === 从 emergency_response_plan 映射 (demand_surge场景) ===
    erp = result.get('emergency_response_plan')
    if isinstance(erp, dict):
        for action in erp.get('immediate_actions', [])[:3]:
            recs.append({
                'action': 'ACCEPT',
                'reason': action.get('action', ''),
                'steps': [],
                'confidence': '高',
                'priority': 'P1',
                'description': action.get('action', ''),
            })

    # === 从 transition_plan 映射 (bom_ecn场景) ===
    for phase in result.get('transition_plan', [])[:3]:
        recs.append({
            'action': 'ACCEPT',
            'reason': f"{phase.get('phase', '')}: {', '.join(phase.get('actions', [])[:2])}",
            'steps': phase.get('actions', []),
            'confidence': '中',
            'priority': 'P2',
            'description': phase.get('phase', ''),
        })

    # === 从 alternative_suppliers 映射 (supplier_delay场景) ===
    for alt in result.get('alternative_suppliers', [])[:3]:
        recs.append({
            'action': 'NEGOTIATE',
            'reason': f"备选供应商: {alt.get('supplier_name', alt.get('code', ''))}",
            'steps': [],
            'confidence': '中',
            'priority': 'P2',
            'description': f"切换至{alt.get('supplier_name', '')}可降低延期风险",
        })

    # === 从 impacted_orders 的 suggestion 补充 ===
    impacted = result.get('impacted_orders', [])
    if isinstance(impacted, dict):
        impacted = impacted.get('details', [])
    if not isinstance(impacted, list):
        impacted = []
    for order in impacted[:3]:
        if order.get('suggestion') or order.get('recommendation'):
            recs.append({
                'action': 'NEGOTIATE',
                'reason': order.get('suggestion', order.get('recommendation', '')),
                'steps': [],
                'confidence': '中',
                'priority': 'P2' if order.get('severity') != 'critical' else 'P1'
            })

    if recs:
        result['recommendations'] = recs

    # 3. decision_support: 基于已有数据生成决策建议
    if 'decision_support' not in result or not result.get('decision_support'):
        score = result.get('overall_impact_score')
        scenario_name = result.get('scenario_name', '')

        if score is not None:
            score_val = float(score)
            if score_val < 0.3:
                decision = {'can_accept': True, 'accept_conditionally': False, 'should_decline': False,
                           'reasoning': f'综合影响评分较低({score_val:.0%})，风险可控，建议接受该操作。'}
            elif score_val < 0.65:
                decision = {'can_accept': False, 'accept_conditionally': True, 'should_decline': False,
                           'reasoning': f'综合影响评分中等({score_val:.0%})，存在一定风险，建议有条件接受并采取缓解措施。'}
            else:
                decision = {'can_accept': False, 'accept_conditionally': False, 'should_decline': True,
                           'reasoning': f'综合影响评分较高({score_val:.0%})，风险较大，建议谨慎评估后决定或拒绝。'}
            result['decision_support'] = decision
        elif 'beneficiary_orders' in result:
            # 订单取消场景：正面影响
            result['decision_support'] = {
                'can_accept': True, 'accept_conditionally': False, 'should_decline': False,
                'reasoning': '取消订单将释放物料和产能资源，可重新分配给其他紧急订单。'
            }
        elif 'emergency_response_plan' in result:
            # 需求激增：需要应对措施
            result['decision_support'] = {
                'can_accept': False, 'accept_conditionally': True, 'should_decline': False,
                'reasoning': '需求激增将对供应链造成压力，建议启动应急预案并密切监控库存水位。'
            }
        elif 'transition_plan' in result:
            # BOM变更：需要过渡期
            result['decision_support'] = {
                'can_accept': False, 'accept_conditionally': True, 'should_decline': False,
                'reasoning': 'BOM工程变更需要过渡期管理，建议分阶段实施并确保新旧物料供应平稳衔接。'
            }


def _flatten_rl_result(result):
    """
    扁平化 RL 推荐结果，将嵌套的 recommendation 字段提升到顶层。
    前端 AIAnalysis.vue 读取: rlResult.action_name, rlResult.confidence,
    rlResult.recommended_action, rlResult.anomaly_detected 等。
    后端 rl_agent 返回: {recommendation: {primary_recommendation: {name, confidence, ...}}}
    """
    if not isinstance(result, dict):
        return

    rec = result.get('recommendation', {})
    if isinstance(rec, dict):
        primary = rec.get('primary_recommendation', {})
        if isinstance(primary, dict):
            # 提取到顶层
            if 'action_name' not in result and 'name' in primary:
                result['action_name'] = primary['name']
            if 'recommended_action' not in result and 'name' in primary:
                result['recommended_action'] = primary['name']
            if 'confidence' not in result:
                result['confidence'] = float(primary.get('confidence', 0))
            if 'q_value' not in result:
                result['q_value'] = float(primary.get('q_value', 0))

        # reasoning 也提取
        if 'reasoning' not in result and 'reasoning' in rec:
            result['reasoning'] = rec['reasoning']

        # state_analysis 提取部分字段
        sa = rec.get('state_analysis', {})
        if isinstance(sa, dict) and 'overall_risk' not in result:
            result['overall_risk'] = sa.get('overall_risk')

    # 确保 anomaly_detected 在顶层
    if 'anomaly_detected' not in result:
        result['anomaly_detected'] = False

    # 确保 confidence 在顶层（默认值，防止前端进度条显示异常）
    if 'confidence' not in result or result['confidence'] is None:
        result['confidence'] = 0.5


def _enrich_forecast_summary(result):
    """
    补充需求预测结果中前端期望但后端可能缺失的summary字段。
    前端 ForecastResult 接口需要 summary:
      - total_predicted_demand, avg_daily_demand, peak_demand_value, peak_demand_day, growth_rate
    """
    if not isinstance(result, dict):
        return
    summary = result.get('summary')
    if not isinstance(summary, dict):
        return

    forecast_list = result.get('forecast', [])
    if isinstance(forecast_list, list) and len(forecast_list) > 0:
        # peak_demand_value / peak_demand_day
        if 'peak_demand_value' not in summary or 'peak_demand_day' not in summary:
            peak_item = max(forecast_list, key=lambda x: x.get('predicted_demand', 0))
            summary.setdefault('peak_demand_value', peak_item.get('predicted_demand', 0))
            summary.setdefault('peak_demand_day', peak_item.get('date', ''))

        # growth_rate: 简单线性趋势估算
        if 'growth_rate' not in summary:
            values = [f.get('predicted_demand', 0) for f in forecast_list]
            if len(values) >= 2:
                first_half = values[:len(values)//2]
                second_half = values[len(values)//2:]
                avg_first = sum(first_half) / len(first_half)
                avg_second = sum(second_half) / len(second_half)
                growth = (avg_second - avg_first) / abs(avg_first) if avg_first else 0
                summary['growth_rate'] = round(growth, 4)
            else:
                summary['growth_rate'] = 0.0

    # 确保 confidence 存在（仅当后端已提供时保留，不伪造）
    # confidence 应由 ai_engine.predict() 返回真实值


# Helper functions
def _get_allocations_for_detection():
    """Get allocation data for anomaly detection"""
    try:
        from ..models import OrderAllocation
        allocations = OrderAllocation.objects.select_related('order', 'material').all()
        return [
            {
                'id': alloc.id,
                'material_id': alloc.material_id,
                'quantity': int(getattr(alloc, 'allocated_quantity', 0)),
                'order_id': alloc.order_id,
                'order_priority': alloc.order.priority if alloc.order else 5,
                'reliability_factor': float(getattr(alloc, 'reliability_factor', 1.0)),
                'is_alternative': getattr(alloc, 'is_alternative', False),
                'is_safety_stock': getattr(alloc, 'is_safety_stock', False)
            }
            for alloc in allocations
        ]
    except Exception as e:
        logger.error(f"Failed to load allocation data: {str(e)}")
        return []


def _get_inventory_for_detection():
    """Get inventory data for anomaly detection"""
    try:
        from ..models import Inventory
        inventories = Inventory.objects.select_related('material').all()
        return [
            {
                'id': inv.id,
                'material_id': inv.material_id,
                'quantity': int(inv.quantity or 0),
                'type': inv.inventory_type,
                'warehouse': inv.warehouse,
                'expiry_date': inv.expiry_date,
                'is_hold': inv.is_hold
            }
            for inv in inventories
        ]
    except Exception as e:
        logger.error(f"Failed to load inventory data: {str(e)}")
        return []


@csrf_exempt
@require_http_methods(["POST"])
@login_required  # 安全修复：要求用户登录才能创建订单
def forecast_to_orders(request):
    """将需求预测结果转为预留订单"""
    try:
        data = json.loads(request.body)
        material_id = data.get('material_id')
        forecast_days = data.get('forecast_days', 14)
        customer_name = data.get('customer_name', '预测预留')

        if not material_id:
            return JsonResponse({'error': '物料ID不能为空'}, status=400)

        material = Material.objects.filter(id=material_id).first()
        if not material:
            return JsonResponse({'error': '物料不存在'}, status=400)

        from ..ai_engine import DemandForecaster
        forecaster = DemandForecaster()
        forecast_result = forecaster.forecast_demand(material_id, days=forecast_days)

        if 'error' in forecast_result:
            return JsonResponse({'error': forecast_result['error']}, status=400)

        created_orders = []
        forecast_data = forecast_result.get('forecast', [])

        from datetime import date, timedelta
        today = date.today()

        for i, day_forecast in enumerate(forecast_data[:forecast_days]):
            forecast_qty = day_forecast.get('yhat', 0)
            if forecast_qty <= 0:
                continue

            demand_date = today + timedelta(days=i + 1)
            order_no = f'FC-{material.material_code}-{demand_date.strftime("%Y%m%d")}'

            if SalesOrder.objects.filter(order_no=order_no).exists():
                continue

            order = SalesOrder.objects.create(
                order_no=order_no,
                customer_name=customer_name,
                material=material,
                quantity=round(forecast_qty, 2),
                unit_price=material.sales_price or 0,
                total_amount=round(forecast_qty * float(material.sales_price or 0), 2),
                order_date=today,
                demand_date=demand_date,
                status='pending',
                priority=5,
                shipping_method='land',
                shipping_days=45,
                production_lead_time=2,
                is_forecast=True
            )
            created_orders.append({
                'order_no': order.order_no,
                'demand_date': str(order.demand_date),
                'quantity': int(order.quantity or 0),
                'status': order.status
            })

        return JsonResponse({
            'success': True,
            'message': f'成功创建 {len(created_orders)} 个预留订单',
            'orders': created_orders,
            'material_code': material.material_code,
            'material_name': material.material_name
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': '无效的JSON数据'}, status=400)
    except Exception as e:
        logger.error(f'预测转订单API异常: {str(e)}', exc_info=True)
        return JsonResponse({'error': '预测转订单服务暂时不可用，请稍后重试'}, status=500)


# ============================================================
# RL 强化学习智能体 API
# ============================================================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def rl_recommendation(request):
    """
    RL智能推荐API — 基于真实数据的强化学习决策推荐
    
    调用 get_realtime_rl_recommendation() 获取基于当前供应链状态的RL动作推荐
    
    返回:
        {
            'success': True,
            'recommended_action': 推荐动作ID,
            'action_name': 动作名称,
            'confidence': 置信度,
            'current_state': 当前13维状态向量,
            'anomaly_detected': 是否检测到异常,
            'db_effects': 数据库执行效果列表,
            ...
        }
    """
    try:
        from ..rl_agent import get_realtime_rl_recommendation
        
        result = get_realtime_rl_recommendation()

        # 扁平化：将嵌套的 recommendation 提取到顶层，兼容前端 rlResult.xxx 读取
        _flatten_rl_result(result)

        PlanLog.objects.create(
            log_type='INFO',
            message=f'RL智能推荐: 动作={result.get("action_name", "未知")}, 置信度={result.get("confidence", 0):.2f}'
        )

        return Response({
            'success': True,
            'data': result
        })
    except Exception as e:
        logger.error(f'RL推荐API错误: {str(e)}', exc_info=True)
        return Response({
            'success': False,
            'error': f'RL推荐服务异常: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def rl_train(request):
    """
    RL训练API — 基于历史数据训练强化学习Agent
    
    POST参数:
        days: 训练数据天数（默认30）
    
    返回:
        {
            'success': True,
            'training_result': { episodes, final_reward, convergence, ... }
        }
    """
    try:
        from ..rl_agent import train_on_historical_data
        
        data = request.data if hasattr(request, 'data') else {}
        days = int(data.get('days', 30))
        
        result = train_on_historical_data(days=days)

        # 映射训练结果字段名，匹配前端期望的 episodes / final_reward
        tr = result.get('training_result', {})
        if isinstance(tr, dict):
            if 'episodes' not in result:
                result['episodes'] = tr.get('episodes_trained', tr.get('episodes', 0))
            if 'final_reward' not in result:
                result['final_reward'] = tr.get('average_reward_last_100', tr.get('final_reward', 0))
        
        PlanLog.objects.create(
            log_type='INFO',
            message=f'RL训练完成: {result.get("episodes", 0)}轮, 最终奖励={result.get("final_reward", 0):.2f}'
        )
        
        return Response({
            'success': True,
            'data': result
        })
    except Exception as e:
        logger.error(f'RL训练API错误: {str(e)}', exc_info=True)
        return Response({
            'success': False,
            'error': f'RL训练异常: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ============================================================
# NSGA-II 多目标优化 API
# ============================================================

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def multi_objective_optimize(request):
    """
    NSGA-II多目标优化API — 运行完整的帕累托前沿优化
    
    同时优化4个目标：
    1. 最大化按时交付率
    2. 最小化交期变更次数  
    3. 最小化库存水位
    4. 最大化报缺时间精准度
    
    POST参数:
        population_size: 种群大小（默认50）
        generations: 进化代数（默认100）
        preference: 偏好模式 (delivery_first/inventory_first/stability_first/cost_first/expiry_first/supplier_first)
    
    返回:
        {
            'success': True,
            'pareto_front': [...],      # 帕累托最优解集
            'recommended_solution': {...}, # 推荐方案
            'optimization_report': {...}   # 优化报告
        }
    """
    try:
        from ..multi_objective_optimizer import run_multi_objective_optimization, get_recommended_planning_strategy

        data = request.data if hasattr(request, 'data') else {}
        preference = data.get('preference', 'delivery_first')
        population_size = int(data.get('population_size', 50))
        generations = int(data.get('generations', 100))

        # 所有偏好模式都运行完整NSGA-II优化，用preference影响推荐解选择
        # 从数据库加载真实数据
        _orders = list(SalesOrder.objects.filter(
            status__in=['pending', 'confirmed', 'allocated', 'partial',
                       'in_production', 'processing']
        ).order_by('priority', 'demand_date')[:200])
        _inventory = list(Inventory.objects.all()[:500])

        optimizer = run_multi_objective_optimization(
            population_size=population_size,
            generations=generations,
            orders=_orders,
            inventory=_inventory
        )

        pareto_front = optimizer.get_pareto_front()
        report = optimizer.generate_optimization_report()
        recommended = optimizer.recommend_solution(preference=preference)

        # 序列化 recommended_solution（可能是 OptimizationIndividual 对象或字典）
        if recommended is not None:
            if hasattr(recommended, 'objectives') and hasattr(recommended, 'decision_vars'):
                # OptimizationIndividual 对象 → 字典
                recommended = {
                    'strategy_name': recommended.decision_vars.get('primary_strategy', '优化方案'),
                    'objectives': recommended.objectives.tolist() if hasattr(recommended.objectives, 'tolist') else list(recommended.objectives),
                    'decision_vars': dict(recommended.decision_vars)
                }
            # elif isinstance(recommended, dict): 已经是字典，直接使用
        # 如果recommended为None，保持为None，不伪造数据

        result_data = {
            'recommended_solution': recommended,  # 可能是 None
            'pareto_front': [
                {
                    'objectives': [float(x) for x in (ind.objectives.tolist() if hasattr(ind.objectives, 'tolist') else list(ind.objectives))],
                    'decision_vars': {k: float(v) if isinstance(v, (int, float, np.floating, np.integer)) else v
                                     for k, v in ind.decision_vars.items()}
                }
                for ind in (pareto_front or [])[:20]
            ],
            'report': {
                'total_generations': generations,
                'pareto_front_size': len(pareto_front) if pareto_front else 0,
                'convergence_rate': float((report or {}).get('convergence_rate', 0) or 0)
            }
        }

        # 确保 recommended_solution 中的 objectives 也是原生类型
        if recommended and isinstance(recommended, dict) and 'objectives' in recommended:
            recommended['objectives'] = [float(x) for x in recommended['objectives']]
        
        PlanLog.objects.create(
            log_type='INFO',
            message=f'NSGA-II多目标优化完成: 偏好={preference}, 种群={population_size}, 代数={generations}'
        )
        
        return Response({
            'success': True,
            'preference': preference,
            'data': result_data
        })
    except Exception as e:
        logger.error(f'NSGA-II优化API错误: {str(e)}', exc_info=True)
        return Response({
            'success': False,
            'error': f'多目标优化服务异常: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
