"""
多轮模拟稳定性分析器

用于验证系统在多次连续运行下的指标稳定性，
确保不会因随机因素导致生产计划大幅波动。

核心功能：
1. 连续执行N轮物料计划
2. 记录每轮的四大数据标
3. 分析指标趋势和波动情况
4. 输出稳定性报告和判定

使用场景：
- 系统上线前的回归测试
- 参数调整后的影响评估
- 定期健康检查和监控
"""

import json
import logging
import time
import argparse
from datetime import datetime, date
from typing import Dict, List, Optional, Any
from collections import defaultdict
import numpy as np

# Django相关导入
from django.db import transaction
from django.core.cache import cache

# 导入系统内部模块
from .material_planning import MaterialPlanner
from .models import (
    SalesOrder, MaterialPlanResult, PlanLog,
    OrderAllocation, Inventory
)

logger = logging.getLogger(__name__)


class StabilityAnalyzer:
    """
    稳定性分析器主类

    通过连续执行多轮物料计划，收集关键性能指标，
    并进行统计分析以评估系统的稳定性和可靠性。

    Attributes:
        rounds (int): 模拟轮数
        order_sample_size (int): 每轮使用的订单采样量（0表示全量）
        metrics_history (dict): 历史指标记录
        planner (MaterialPlanner): 物料计划器实例
    """

    # 关键指标名称定义
    METRIC_NAMES = [
        'on_time_rate',          # 准时交付率
        'delivery_changes',      # 交期变更次数
        'inventory_level',       # 库存水平
        'shortage_precision',    # 缺料预测精度
        'complete_rate',         # 齐套率
        'execution_time'         # 执行时间(秒)
    ]

    # 稳定性判定阈值
    STABILITY_THRESHOLDS = {
        'cv_stable': 0.05,       # 变异系数 < 5% 视为稳定
        'cv_acceptable': 0.15,   # 变异系数 < 15% 视为可接受
        'max_single_change': 0.2 # 单次变化不超过20%
    }

    def __init__(self, rounds: int = 10, order_sample_size: int = 5000, consumption_priority: str = 'FIFO'):
        """
        初始化稳定性分析器

        Args:
            rounds: 模拟轮数，默认10轮
            order_sample_size: 每轮使用的订单采样量（0表示全量），默认5000
            consumption_priority: 库存消耗优先级策略，默认FIFO（与物料计划页面保持一致）
        """
        self.rounds = max(1, min(rounds, 50))  # 限制在1-50轮之间
        self.order_sample_size = max(0, order_sample_size)
        self.metrics_history: Dict[int, Dict[str, Any]] = {}
        self.planner = MaterialPlanner(consumption_priority=consumption_priority)
        self._start_time: Optional[datetime] = None
        self._end_time: Optional[datetime] = None

        logger.info(f'初始化稳定性分析器: 轮数={self.rounds}, 订单采样量={self.order_sample_size}, 策略={consumption_priority}')

    def run_stability_analysis(self) -> Dict[str, Any]:
        """
        执行完整的稳定性分析流程

        该方法会按顺序执行以下步骤：
        1. 初始化环境和缓存
        2. 连续执行N轮物料计划模拟
        3. 收集并记录每轮的关键指标
        4. 进行统计分析（均值、标准差、变异系数等）
        5. 判断趋势方向（基于线性回归斜率）
        6. 计算相邻轮次间的Plan-to-Plan波动率
        7. 给出最终稳定性判定和建议

        Returns:
            dict: 包含完整的稳定性分析结果，结构如下：
                {
                    'rounds_completed': int,           # 完成的轮数
                    'metrics_history': {               # 每轮的指标记录
                        'round_n': {
                            'on_time_rate': float,
                            'delivery_changes': int,
                            'inventory_level': float,
                            'shortage_precision': float,
                            'complete_rate': float,
                            'execution_time': float,
                        }
                    },
                    'statistics': {                    # 统计分析结果
                        'mean': {...},                 # 各指标均值
                        'std': {...},                  # 各指标标准差
                        'cv': {...},                   # 各指标变异系数 (std/mean)
                        'trend': {...}                 # 各指标趋势 ('stable'/'increasing'/'decreasing'/'volatile')
                    },
                    'plan_to_plan_volatility': [...],  # 相邻轮次间的波动率列表
                    'stability_verdict': str,          # 最终判定: 'STABLE' / 'ACCEPTABLE' / 'UNSTABLE'
                    'confidence_level': float,         # 对稳定性判定的置信度 (0-1)
                    'recommendations': [...],          # 改进建议列表
                    'execution_summary': {             # 执行摘要
                        'total_time_seconds': float,
                        'avg_time_per_round': float,
                        'success_rate': float
                    }
                }
        """
        self._start_time = datetime.now()
        logger.info('=' * 80)
        logger.info(f'开始执行稳定性分析: 共{self.rounds}轮, 订单采样量={self.order_sample_size}')
        logger.info('=' * 80)

        completed_rounds = 0
        failed_rounds = 0
        total_execution_time = 0.0

        try:
            # 预加载缓存数据以提高性能
            logger.info('预加载基础数据...')
            self.planner.load_material_info_cache()
            self.planner.load_supplier_info_cache()
            self.planner.load_inventory_cache()
            self.planner.load_bom_cache()
            self.planner.load_workcenter_info_cache()
            self.planner.load_factory_calendar()
            logger.info('基础数据加载完成')

            # 执行多轮模拟
            for round_num in range(1, self.rounds + 1):
                try:
                    logger.info('-' * 80)
                    logger.info(f'开始第 {round_num}/{self.rounds} 轮模拟...')

                    round_start_time = time.time()

                    # 执行单轮模拟
                    round_metrics = self._execute_single_round(round_num)

                    round_end_time = time.time()
                    execution_time = round_end_time - round_start_time
                    round_metrics['execution_time'] = execution_time

                    # 记录本轮结果
                    self.metrics_history[round_num] = round_metrics
                    completed_rounds += 1
                    total_execution_time += execution_time

                    logger.info(f'第{round_num}轮完成: 执行时间={execution_time:.2f}秒, '
                               f'齐套率={round_metrics.get("complete_rate", 0):.2%}')

                    # 单轮间短暂休息，避免系统过载
                    if round_num < self.rounds:
                        time.sleep(0.5)

                except Exception as e:
                    failed_rounds += 1
                    logger.error(f'第{round_num}轮执行失败: {str(e)}', exc_info=True)

                    # 记录失败日志到PlanLog
                    PlanLog.objects.create(
                        log_type='ERROR',
                        message=f'稳定性分析第{round_num}轮执行失败: {str(e)}'
                    )

                    # 单轮失败不应中断整个分析，继续下一轮
                    continue

            # 执行统计分析
            statistics = self._calculate_statistics()

            # 计算Plan-to-Plan波动率
            volatility_list = self._calculate_plan_to_plan_volatility()

            # 判定稳定性
            verdict, confidence = self._determine_stability(statistics, volatility_list)

            # 生成改进建议
            recommendations = self._generate_recommendations(statistics, volatility_list, verdict)

            self._end_time = datetime.now()
            total_analysis_time = (self._end_time - self._start_time).total_seconds()

            result = {
                'rounds_completed': completed_rounds,
                'total_rounds': self.rounds,
                'failed_rounds': failed_rounds,
                'metrics_history': dict(self.metrics_history),
                'statistics': statistics,
                'plan_to_plan_volatility': volatility_list,
                'stability_verdict': verdict,
                'confidence_level': confidence,
                'recommendations': recommendations,
                'execution_summary': {
                    'total_time_seconds': total_analysis_time,
                    'avg_time_per_round': total_execution_time / max(completed_rounds, 1),
                    'success_rate': completed_rounds / self.rounds if self.rounds > 0 else 0
                },
                'analysis_timestamp': self._end_time.isoformat() if self._end_time else None
            }

            logger.info('=' * 80)
            logger.info(f'稳定性分析完成: 完成{completed_rounds}轮, 失败{failed_rounds}轮, '
                       f'总耗时={total_analysis_time:.2f}秒')
            logger.info(f'稳定性判定: {verdict} (置信度: {confidence:.2%})')
            logger.info('=' * 80)

            return result

        except Exception as e:
            logger.error(f'稳定性分析过程发生严重错误: {str(e)}', exc_info=True)
            raise

    def _execute_single_round(self, round_num: int) -> Dict[str, Any]:
        """
        执行单轮物料计划模拟

        在每一轮中：
        1. 加载订单数据（支持采样）
        2. 执行物料计划
        3. 收集关键性能指标
        4. 返回本轮的指标字典

        Args:
            round_num: 当前轮次编号（从1开始）

        Returns:
            dict: 本轮的关键指标，包含以下字段：
                - on_time_rate: 准时交付率 (0-1)
                - delivery_changes: 交期变更次数
                - inventory_level: 平均库存水平
                - shortage_precision: 缺料预测精度 (0-1)
                - complete_rate: 平均齐套率 (0-1)
        """
        from django.db.models import Avg, Count, Q

        # 1. 获取订单数据
        orders_query = SalesOrder.objects.filter(
            status__in=['pending', 'confirmed', 'allocated', 'partial']
        ).select_related('material')

        # 应用采样限制
        if self.order_sample_size > 0:
            total_orders = orders_query.count()
            if total_orders > self.order_sample_size:
                # 使用简单的顺序采样（前N个）或随机采样
                # 这里使用ID排序后取前N个，保证可重复性
                order_ids = list(orders_query.order_by('id')[:self.order_sample_size].values_list('id', flat=True))
                orders_query = SalesOrder.objects.filter(id__in=order_ids)

        orders = list(orders_query)

        if not orders:
            logger.warning(f'第{round_num}轮: 未找到符合条件的订单')
            return {
                'on_time_rate': 0.0,
                'delivery_changes': 0,
                'inventory_level': 0.0,
                'shortage_precision': 0.0,
                'complete_rate': 0.0
            }

        logger.info(f'第{round_num}轮: 加载了 {len(orders)} 个订单')

        # 2. 执行物料计划
        results = self.planner.run_planning(
            orders=orders,
            parallel=False,  # 稳定性分析中串行执行以确保可重复性
            max_workers=1
        )

        # 3. 收集关键指标
        metrics = self._extract_round_metrics(results, orders, round_num)

        return metrics

    def _extract_round_metrics(self, results: List[Dict], orders: List, round_num: int) -> Dict[str, Any]:
        """
        从单轮计划结果中提取关键性能指标

        Args:
            results: run_planning返回的结果列表
            orders: 本轮处理的订单列表
            round_num: 当前轮次

        Returns:
            dict: 提取出的指标字典
        """
        from django.db.models import Avg, Sum, Count

        metrics = {}

        try:
            # 1. 计算准时交付率（假设需求日期在未来）
            today = date.today()
            on_time_count = 0
            total_count = len(results)

            for result in results:
                order_id = result.get('order_id')
                if not order_id:
                    continue

                # 从数据库获取订单信息（确保最新状态）
                try:
                    order = SalesOrder.objects.get(id=order_id)
                    if hasattr(order, 'demand_date') and order.demand_date:
                        # 如果订单已完成且未逾期，视为准时
                        if result.get('is_complete') and order.demand_date >= today:
                            on_time_count += 1
                        elif result.get('is_complete') and order.demand_date < today:
                            # 完成但逾期，根据超期天数扣分
                            overdue_days = (today - order.demand_date).days
                            if overdue_days <= 3:  # 允许3天内延迟
                                on_time_count += 0.5
                except SalesOrder.DoesNotExist:
                    pass

            metrics['on_time_rate'] = on_time_count / max(total_count, 1)

            # 2. 统计交期变更次数（从MaterialPlanResult获取）
            delivery_changes = MaterialPlanResult.objects.filter(
                order__id__in=[r.get('order_id') for r in results if r.get('order_id')]
            ).aggregate(total=Sum('delivery_change_count'))['total'] or 0

            metrics['delivery_changes'] = delivery_changes

            # 3. 计算库存水平（平均可用库存）
            inventory_stats = Inventory.objects.filter(
                inventory_type='local',
                is_hold=False
            ).aggregate(
                avg_available=Avg('available_quantity'),
                total_quantity=Sum('quantity')
            )

            metrics['inventory_level'] = float(inventory_stats['avg_available'] or 0)
            metrics['total_inventory'] = int(inventory_stats['total_quantity'] or 0)

            # 4. 计算缺料预测精度（通过比较预测缺料与实际缺料）
            # 这里简化处理：使用齐套率的反向作为缺料精度的代理
            complete_results = [r for r in results if r.get('complete_rate') is not None]
            if complete_results:
                avg_complete_rate = sum(r['complete_rate'] for r in complete_results) / len(complete_results)
                metrics['shortage_precision'] = avg_complete_rate  # 齐套率高则缺料预测准
                metrics['complete_rate'] = avg_complete_rate
            else:
                metrics['shortage_precision'] = 0.0
                metrics['complete_rate'] = 0.0

            # 5. 记录额外统计信息
            metrics['orders_processed'] = total_count
            metrics['orders_completed'] = sum(1 for r in results if r.get('is_complete'))
            metrics['round_number'] = round_num

            logger.debug(f'第{round_num}轮指标提取完成: {metrics}')

        except Exception as e:
            logger.error(f'提取第{round_num}轮指标时出错: {str(e)}', exc_info=True)
            # 返回默认值
            metrics = {
                'on_time_rate': 0.0,
                'delivery_changes': 0,
                'inventory_level': 0.0,
                'shortage_precision': 0.0,
                'complete_rate': 0.0,
                'orders_processed': 0,
                'round_number': round_num
            }

        return metrics

    def _calculate_statistics(self) -> Dict[str, Any]:
        """
        计算所有指标的统计特征

        包括：均值、标准差、变异系数(CV)、最小值、最大值、趋势判断

        Returns:
            dict: 统计分析结果
        """
        if not self.metrics_history:
            return {'mean': {}, 'std': {}, 'cv': {}, 'trend': {}, 'min': {}, 'max': {}}

        statistics = {
            'mean': {},
            'std': {},
            'cv': {},
            'trend': {},
            'min': {},
            'max': {}
        }

        for metric_name in self.METRIC_NAMES:
            values = [
                self.metrics_history[round_num].get(metric_name, 0)
                for round_num in sorted(self.metrics_history.keys())
            ]

            if not values or all(v is None for v in values):
                continue

            # 过滤掉None值
            valid_values = [v for v in values if v is not None]

            if not valid_values:
                continue

            values_array = np.array(valid_values, dtype=float)

            # 计算基本统计量
            mean_val = np.mean(values_array)
            std_val = np.std(values_array, ddof=1)  # 使用样本标准差
            cv_val = std_val / mean_val if mean_val != 0 else float('inf')

            statistics['mean'][metric_name] = float(mean_val)
            statistics['std'][metric_name] = float(std_val)
            statistics['cv'][metric_name] = float(cv_val)
            statistics['min'][metric_name] = float(np.min(values_array))
            statistics['max'][metric_name] = float(np.max(values_array))

            # 判断趋势
            trend = self._calculate_trend(valid_values)
            statistics['trend'][metric_name] = trend

        logger.info(f'统计分析完成: {len(statistics["mean"])} 个指标已计算')

        return statistics

    def _calculate_trend(self, values: List[float]) -> str:
        """
        判断指标的趋势方向

        基于简单线性回归的斜率来判断趋势：
        - 斜率接近0且变异系数小 → stable（稳定）
        - 斜率显著为正 → increasing（上升）
        - 斜率显著为负 → decreasing（下降）
        - 斜率不稳定或变异系数大 → volatile（波动）

        Args:
            values: 指标值的时间序列列表

        Returns:
            str: 趋势判定，取值为 'stable'/'increasing'/'decreasing'/'volatile'
        """
        if len(values) < 3:
            return 'stable'  # 数据点不足，默认稳定

        x = np.arange(len(values))
        y = np.array(values, dtype=float)

        # 计算线性回归斜率
        slope, intercept = np.polyfit(x, y, 1)

        # 计算相对斜率（斜率/均值）
        mean_y = np.mean(y)
        relative_slope = slope / mean_y if mean_y != 0 else 0

        # 计算R²值（拟合优度）
        y_pred = slope * x + intercept
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0

        # 根据相对斜率和R²判断趋势
        if abs(relative_slope) < 0.02:  # 相对变化小于2%
            return 'stable'
        elif r_squared < 0.5:  # 拟合度低，说明波动大
            return 'volatile'
        elif relative_slope > 0.02:
            return 'increasing'
        else:
            return 'decreasing'

    def _calculate_plan_to_plan_volatility(self) -> List[Dict[str, Any]]:
        """
        计算相邻轮次间的Plan-to-Plan波动率

        波动率定义为：(当前值 - 上轮值) / 上轮值 * 100%

        Returns:
            list: 波动率记录列表，每个元素包含：
                - round_from: 起始轮次
                - round_to: 结束轮次
                - metric_volatilities: 各指标的波动率字典
                - max_volatility: 最大波动率
                - avg_volatility: 平均波动率
        """
        volatility_list = []
        sorted_rounds = sorted(self.metrics_history.keys())

        for i in range(len(sorted_rounds) - 1):
            round_from = sorted_rounds[i]
            round_to = sorted_rounds[i + 1]

            prev_metrics = self.metrics_history[round_from]
            curr_metrics = self.metrics_history[round_to]

            metric_volatilities = {}
            max_vol = 0.0
            vol_sum = 0.0
            vol_count = 0

            for metric_name in self.METRIC_NAMES:
                prev_val = prev_metrics.get(metric_name)
                curr_val = curr_metrics.get(metric_name)

                if prev_val is None or curr_val is None:
                    continue

                if prev_val == 0:
                    # 避免除零错误
                    if curr_val == 0:
                        vol = 0.0
                    else:
                        vol = abs(curr_val) * 100  # 从0变为非0，视为100%变化
                else:
                    vol = abs((curr_val - prev_val) / prev_val) * 100

                metric_volatilities[metric_name] = round(vol, 2)
                max_vol = max(max_vol, vol)
                vol_sum += vol
                vol_count += 1

            avg_vol = vol_sum / max(vol_count, 1)

            volatility_record = {
                'round_from': round_from,
                'round_to': round_to,
                'metric_volatilities': metric_volatilities,
                'max_volatility': round(max_vol, 2),
                'avg_volatility': round(avg_vol, 2)
            }

            volatility_list.append(volatility_record)

        logger.info(f'计算出 {len(volatility_list)} 组Plan-to-Plan波动率')

        return volatility_list

    def _determine_stability(self, statistics: Dict, volatility_list: List[Dict]) -> tuple:
        """
        判定整体稳定性等级

        判定规则：
        1. 所有指标的CV都 < 5% 且最大单次波动 < 20% → STABLE（稳定）
        2. 大部分指标CV < 15% 或偶尔有波动但可恢复 → ACCEPTABLE（可接受）
        3. 存在指标CV >= 15% 或频繁大幅波动 → UNSTABLE（不稳定）

        Args:
            statistics: 统计分析结果
            volatility_list: 波动率列表

        Returns:
            tuple: (verdict, confidence)
                - verdict: 'STABLE' / 'ACCEPTABLE' / 'UNSTABLE'
                - confidence: 置信度 (0-1)
        """
        cv_data = statistics.get('cv', {})
        trend_data = statistics.get('trend', {})

        if not cv_data:
            return 'UNSTABLE', 0.0

        # 计算各指标的CV评分
        cv_scores = []
        for metric_name, cv_value in cv_data.items():
            if cv_value == float('inf'):
                cv_scores.append(0.0)  # 无穷大CV给最低分
            elif cv_value <= self.STABILITY_THRESHOLDS['cv_stable']:
                cv_scores.append(1.0)  # 完全稳定
            elif cv_value <= self.STABILITY_THRESHOLDS['cv_acceptable']:
                # 线性插值：5%->1.0, 15%->0.6
                score = 1.0 - (cv_value - self.STABILITY_THRESHOLDS['cv_stable']) / \
                        (self.STABILITY_THRESHOLDS['cv_acceptable'] - self.STABILITY_THRESHOLDS['cv_stable']) * 0.4
                cv_scores.append(max(0.6, score))
            else:
                # 超过可接受范围，快速下降
                score = max(0.0, 0.6 - (cv_value - self.STABILITY_THRESHOLDS['cv_acceptable']) * 0.1)
                cv_scores.append(score)

        avg_cv_score = sum(cv_scores) / len(cv_scores) if cv_scores else 0

        # 检查最大单次波动
        max_single_volatility = 0.0
        if volatility_list:
            max_single_volatility = max(v.get('max_volatility', 0) for v in volatility_list)

        volatility_penalty = 0.0
        if max_single_volatility > self.STABILITY_THRESHOLDS['max_single_change'] * 100:
            volatility_penalty = min(0.3, (max_single_volatility - 20) * 0.01)

        # 检查趋势稳定性（volatile趋势会降低置信度）
        volatile_trends = sum(1 for t in trend_data.values() if t == 'volatile')
        trend_penalty = volatile_trends * 0.05

        # 综合置信度计算
        confidence = max(0.0, min(1.0, avg_cv_score - volatility_penalty - trend_penalty))

        # 判定等级
        if confidence >= 0.85 and max_single_volatility <= 20:
            verdict = 'STABLE'
        elif confidence >= 0.6:
            verdict = 'ACCEPTABLE'
        else:
            verdict = 'UNSTABLE'

        logger.info(f'稳定性判定: {verdict}, 置信度={confidence:.2%}, '
                   f'最大单次波动={max_single_volatility:.1f}%')

        return verdict, confidence

    def _generate_recommendations(self, statistics: Dict, volatility_list: List[Dict],
                                   verdict: str) -> List[str]:
        """
        根据分析结果生成改进建议

        Args:
            statistics: 统计分析结果
            volatility_list: 波动率列表
            verdict: 稳定性判定结果

        Returns:
            list: 改进建议字符串列表
        """
        recommendations = []

        if verdict == 'STABLE':
            recommendations.append('✅ 系统运行稳定，各项指标表现良好，无需紧急调整。')
            recommendations.append('💡 建议：继续保持当前参数配置，定期执行稳定性检查。')

        elif verdict == 'ACCEPTABLE':
            recommendations.append('⚠️ 系统基本稳定，但存在轻微波动，建议关注以下方面：')

            # 分析具体问题
            cv_data = statistics.get('cv', {})
            problematic_metrics = [
                (name, cv) for name, cv in cv_data.items()
                if cv > self.STABILITY_THRESHOLDS['cv_acceptable']
            ]

            if problematic_metrics:
                for metric_name, cv in problematic_metrics[:3]:  # 只显示前3个问题指标
                    metric_display_names = {
                        'on_time_rate': '准时交付率',
                        'delivery_changes': '交期变更次数',
                        'inventory_level': '库存水平',
                        'shortage_precision': '缺料预测精度',
                        'complete_rate': '齐套率'
                    }
                    display_name = metric_display_names.get(metric_name, metric_name)
                    recommendations.append(
                        f'   • {display_name}波动较大(CV={cv:.1%})，建议优化相关算法或增加缓冲'
                    )

            recommendations.append('💡 建议：考虑调整安全库存策略或优化优先级规则。')

        else:  # UNSTABLE
            recommendations.append('❌ 系统稳定性不足，需要立即关注和优化！')
            recommendations.append('')
            recommendations.append('可能的原因及建议：')

            # 检查高波动指标
            if volatility_list:
                max_vol_record = max(volatility_list, key=lambda x: x.get('max_volatility', 0))
                worst_metric = max(
                    max_vol_record.get('metric_volatilities', {}).items(),
                    key=lambda x: x[1],
                    default=(None, 0)
                )
                if worst_metric[0]:
                    metric_display_names = {
                        'on_time_rate': '准时交付率',
                        'delivery_changes': '交期变更次数',
                        'inventory_level': '库存水平',
                        'shortage_precision': '缺料预测精度',
                        'complete_rate': '齐套率'
                    }
                    display_name = metric_display_names.get(worst_metric[0], worst_metric[0])
                    recommendations.append(
                        f'   🔴 {display_name}出现剧烈波动({worst_metric[1]:.1f}%)，需重点排查'
                    )

            recommendations.append('   📋 建议：')
            recommendations.append('      1. 检查输入数据的完整性和一致性')
            recommendations.append('      2. 调整算法参数（如优先级权重、消耗策略等）')
            recommendations.append('      3. 增加安全库存缓冲')
            recommendations.append('      4. 考虑启用更稳定的分配策略（如FIFO）')
            recommendations.append('      5. 排查是否存在外部依赖的不确定性')

        return recommendations


def generate_stability_report(analyzer_result: Dict[str, Any], output_format: str = 'text') -> str:
    """
    生成格式化的稳定性报告

    将稳定性分析的结果转换为易读的报告格式。

    Args:
        analyzer_result: StabilityAnalyzer.run_stability_analysis()返回的结果字典
        output_format: 输出格式，可选值为：
            - 'text': 纯文本格式（默认）
            - 'json': JSON格式
            - 'markdown': Markdown格式

    Returns:
        str: 格式化后的报告字符串
    """
    if output_format.lower() == 'json':
        return json.dumps(analyzer_result, ensure_ascii=False, indent=2, default=str)

    # 通用数据准备
    verdict = analyzer_result.get('stability_verdict', 'UNKNOWN')
    confidence = analyzer_result.get('confidence_level', 0)
    rounds_completed = analyzer_result.get('rounds_completed', 0)
    total_rounds = analyzer_result.get('total_rounds', 0)
    failed_rounds = analyzer_result.get('failed_rounds', 0)
    statistics = analyzer_result.get('statistics', {})
    volatility_list = analyzer_result.get('plan_to_plan_volatility', [])
    recommendations = analyzer_result.get('recommendations', [])
    exec_summary = analyzer_result.get('execution_summary', {})

    if output_format.lower() == 'markdown':
        return _generate_markdown_report(
            verdict, confidence, rounds_completed, total_rounds, failed_rounds,
            statistics, volatility_list, recommendations, exec_summary, analyzer_result
        )
    else:
        # 默认文本格式
        return _generate_text_report(
            verdict, confidence, rounds_completed, total_rounds, failed_rounds,
            statistics, volatility_list, recommendations, exec_summary
        )


def _generate_text_report(verdict: str, confidence: float, rounds_completed: int,
                          total_rounds: int, failed_rounds: int,
                          statistics: Dict, volatility_list: List[Dict],
                          recommendations: List[str], exec_summary: Dict) -> str:
    """生成纯文本格式的报告"""
    lines = []
    lines.append('=' * 80)
    lines.append('多轮模拟稳定性分析报告')
    lines.append('=' * 80)
    lines.append('')

    # 基本信息
    lines.append('【基本信息】')
    lines.append(f'  分析时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'  总轮数: {total_rounds}')
    lines.append(f'  成功轮数: {rounds_completed}')
    lines.append(f'  失败轮数: {failed_rounds}')
    lines.append(f'  成功率: {exec_summary.get("success_rate", 0):.1%}')
    lines.append(f'  总耗时: {exec_summary.get("total_time_seconds", 0):.2f}秒')
    lines.append(f'  平均耗时: {exec_summary.get("avg_time_per_round", 0):.2f}秒/轮')
    lines.append('')

    # 判定结果
    verdict_symbols = {'STABLE': '✅', 'ACCEPTABLE': '⚠️', 'UNSTABLE': '❌'}
    symbol = verdict_symbols.get(verdict, '❓')
    lines.append('【稳定性判定】')
    lines.append(f'  {symbol} 判定结果: {verdict}')
    lines.append(f'  置信度: {confidence:.2%}')
    lines.append('')

    # 统计摘要
    lines.append('【指标统计】')
    lines.append(f'  {"指标":<20} {"均值":>12} {"标准差":>12} {"变异系数":>10} {"趋势":>12}')
    lines.append(f'  {"-"*66}')

    metric_display_names = {
        'on_time_rate': '准时交付率',
        'delivery_changes': '交期变更次数',
        'inventory_level': '库存水平',
        'shortage_precision': '缺料预测精度',
        'complete_rate': '齐套率',
        'execution_time': '执行时间(秒)'
    }

    mean_data = statistics.get('mean', {})
    std_data = statistics.get('std', {})
    cv_data = statistics.get('cv', {})
    trend_data = statistics.get('trend', {})

    for metric_name in StabilityAnalyzer.METRIC_NAMES:
        display_name = metric_display_names.get(metric_name, metric_name)
        mean_val = mean_data.get(metric_name, 0)
        std_val = std_data.get(metric_name, 0)
        cv_val = cv_data.get(metric_name, 0)
        trend = trend_data.get(metric_name, 'N/A')

        # 格式化显示
        if metric_name in ['execution_time', 'delivery_changes']:
            mean_str = f'{mean_val:.2f}'
            std_str = f'{std_val:.2f}'
        else:
            mean_str = f'{mean_val:.4f}'
            std_str = f'{std_val:.4f}'

        cv_str = f'{cv_val:.2%}' if cv_val != float('inf') else '∞'

        lines.append(f'  {display_name:<20} {mean_str:>12} {std_str:>12} {cv_str:>10} {trend:>12}')

    lines.append('')

    # 波动率信息
    if volatility_list:
        lines.append('【Plan-to-Plan波动率】')
        for i, vol in enumerate(volatility_list[-5:], 1):  # 只显示最近5组
            lines.append(f'  第{vol["round_from"]}→{vol["round_to"]}轮: '
                        f'平均波动={vol["avg_volatility"]:.1f}%, '
                        f'最大波动={vol["max_volatility"]:.1f}%')
        lines.append('')

    # 改进建议
    if recommendations:
        lines.append('【改进建议】')
        for rec in recommendations:
            lines.append(f'  {rec}')
        lines.append('')

    lines.append('=' * 80)
    lines.append('报告生成完毕')
    lines.append('=' * 80)

    return '\n'.join(lines)


def _generate_markdown_report(verdict: str, confidence: float, rounds_completed: int,
                              total_rounds: int, failed_rounds: int,
                              statistics: Dict, volatility_list: List[Dict],
                              recommendations: List[str], exec_summary: Dict,
                              full_result: Dict) -> str:
    """生成Markdown格式的报告"""
    lines = []
    lines.append('# 多轮模拟稳定性分析报告\n')
    lines.append(f'> 分析时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n')

    # 判定结果
    verdict_emoji = {'STABLE': '✅', 'ACCEPTABLE': '⚠️', 'UNSTABLE': '❌'}
    emoji = verdict_emoji.get(verdict, '❓')
    lines.append(f'## 📊 稳定性判定\n')
    lines.append(f'| 项目 | 值 |')
    lines.append('|------|----|')
    lines.append(f'| 判定结果 | **{emoji} {verdict}** |')
    lines.append(f'| 置信度 | **{confidence:.2%}** |')
    lines.append(f'| 总轮数 | {total_rounds} |')
    lines.append(f'| 成功轮数 | {rounds_completed} |')
    lines.append(f'| 成功率 | {exec_summary.get("success_rate", 0):.1%} |')
    lines.append(f'| 总耗时 | {exec_summary.get("total_time_seconds", 0):.2f}秒 |\n')

    # 统计表格
    lines.append('## 📈 指标统计详情\n')
    lines.append('| 指标 | 均值 | 标准差 | 变异系数(CV) | 趋势 |')
    lines('|------|------|--------|-------------|------|')

    metric_display_names = {
        'on_time_rate': '准时交付率',
        'delivery_changes': '交期变更次数',
        'inventory_level': '库存水平',
        'shortage_precision': '缺料预测精度',
        'complete_rate': '齐套率',
        'execution_time': '执行时间(秒)'
    }

    mean_data = statistics.get('mean', {})
    std_data = statistics.get('std', {})
    cv_data = statistics.get('cv', {})
    trend_data = statistics.get('trend', {})

    for metric_name in StabilityAnalyzer.METRIC_NAMES:
        display_name = metric_display_names.get(metric_name, metric_name)
        mean_val = mean_data.get(metric_name, 0)
        std_val = std_data.get(metric_name, 0)
        cv_val = cv_data.get(metric_name, 0)
        trend = trend_data.get(metric_name, 'N/A')

        if metric_name in ['execution_time', 'delivery_changes']:
            mean_str = f'{mean_val:.2f}'
            std_str = f'{std_val:.2f}'
        else:
            mean_str = f'{mean_val:.4f}'
            std_str = f'{std_val:.4f}'

        cv_str = f'{cv_val:.2%}' if cv_val != float('inf') else '∞'

        trend_emoji_map = {
            'stable': '➡️',
            'increasing': '📈',
            'decreasing': '📉',
            'volatile': '〰️'
        }
        trend_emoji = trend_emoji_map.get(trend, '')

        lines.append(f'| {display_name} | {mean_str} | {std_str} | {cv_str} | {trend} {trend_emoji} |')

    lines.append('')

    # 波动率
    if volatility_list:
        lines.append('## 🔄 Plan-to-Plan波动率\n')
        lines.append('| 轮次区间 | 平均波动率 | 最大波动率 |')
        lines('|----------|-----------|-----------|')
        for vol in volatility_list[-10:]:  # 显示最近10组
            lines.append(f'| 第{vol["round_from"]}→{vol["round_to"]}轮 '
                        f'| {vol["avg_volatility"]:.1f}% '
                        f'| **{vol["max_volatility"]:.1f}%** |')
        lines.append('')

    # 建议
    if recommendations:
        lines.append('## 💡 改进建议\n')
        for rec in recommendations:
            lines.append(f'- {rec}')
        lines.append('')

    lines.append('---\n')
    lines.append('*报告由多轮模拟稳定性分析器自动生成*\n')

    return '\n'.join(lines)


# CLI入口
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='多轮模拟稳定性分析工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例用法:
  python stability_analyzer.py --rounds 10 --orders 5000 --output text
  python stability_analyzer.py --rounds 20 --output markdown
  python stability_analyzer.py --rounds 5 --orders 0 --output json
        '''
    )

    parser.add_argument(
        '--rounds', type=int, default=10,
        help='模拟轮数（默认10轮，范围1-50）'
    )
    parser.add_argument(
        '--orders', type=int, default=5000,
        help='每轮订单采样量（0表示全量，默认5000）'
    )
    parser.add_argument(
        '--strategy', type=str, default='FIFO',
        choices=['FIFO', 'LIFO', 'EXPIRY_FIRST', 'PRIORITY', 'INVENTORY_FIRST'],
        help='库存消耗优先级策略（默认FIFO）'
    )
    parser.add_argument(
        '--output', choices=['text', 'json', 'markdown'], default='text',
        help='输出格式（默认text）'
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='启用详细日志输出'
    )

    args = parser.parse_args()

    # 配置日志级别
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    try:
        print(f'\n🚀 启动稳定性分析: {args.rounds}轮, 订单采样={args.orders}, 策略={args.strategy}\n')
        print('=' * 80 + '\n')

        # 创建分析器实例
        analyzer = StabilityAnalyzer(rounds=args.rounds, order_sample_size=args.orders, consumption_priority=args.strategy)

        # 执行分析
        result = analyzer.run_stability_analysis()

        # 生成并输出报告
        report = generate_stability_report(result, args.output)
        print(report)

        # 将报告保存到文件（可选）
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'stability_report_{timestamp}.{args.output}'
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(report)

        print(f'\n📄 报告已保存至: {filename}\n')

    except KeyboardInterrupt:
        print('\n\n⚠️ 用户中断分析')
    except Exception as e:
        print(f'\n\n❌ 分析过程中发生错误: {str(e)}')
        logging.error('稳定性分析失败', exc_info=True)
        exit(1)
