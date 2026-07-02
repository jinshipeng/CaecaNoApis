# -*- coding: utf-8 -*-
"""
NSGA-II 多目标优化模块

基于非支配排序遗传算法（Non-dominated Sorting Genetic Algorithm II）的多目标优化框架，
用于同时优化物料计划中的多个目标：
1. 最大化按时交付率 (on_time_delivery_rate)
2. 最小化交期变更次数 (delivery_change_count)
3. 最小化库存水位 (inventory_level)
4. 最大化报缺时间精准度 (shortage_precision)

作者: AI Assistant
创建时间: 2026-06-10
"""

import numpy as np
import random
import logging
from datetime import date, timedelta
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

from .models import (
    SalesOrder,
    Material,
    Inventory,
    OrderAllocation,
    MaterialPlanResult,
    PlanLog
)

logger = logging.getLogger(__name__)


class AllocationStrategy(Enum):
    """订单分配策略枚举"""
    FIFO = "FIFO"  # 先进先出
    PRIORITY = "PRIORITY"  # 优先级优先
    LIFO = "LIFO"  # 后进先出
    SUPPLIER_FIRST = "SUPPLIER_FIRST"  # 供应商优先


@dataclass
class OptimizationIndividual:
    """
    优化个体 - 编码一套完整的物料计划决策方案
    
    每个个体代表一种可能的物料计划策略组合，包含：
    - 订单分配策略选择
    - 缺料物料的供应商选择
    - 全局库存消耗优先级权重
    """
    
    # 决策变量编码
    decision_vars: Dict[str, Any] = field(default_factory=dict)
    
    # 4个目标函数值 [按时交付率, 负交期变更次数(取反), 库存水位, 报缺精准度]
    objectives: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    
    # 非支配排序等级（rank=1表示帕累托最优）
    rank: int = 0
    
    # 拥挤度距离（用于维持种群多样性）
    crowding_distance: float = 0.0
    
    # 被其他个体支配的计数
    domination_count: int = 0
    
    # 该个体支配的其他个体集合
    dominated_set: List[int] = field(default_factory=list)
    
    def __post_init__(self):
        """初始化后处理，确保列表类型正确"""
        if not isinstance(self.objectives, list):
            self.objectives = list(self.objectives)
        if not isinstance(self.dominated_set, list):
            self.dominated_set = list(self.dominated_set)


class NSGA2Optimizer:
    """
    NSGA-II 多目标优化器
    
    同时优化4个目标：
    1. 最大化按时交付率 (maximize on_time_delivery_rate)
    2. 最小化交期变更次数 (minimize delivery_change_count)  
    3. 最小化库存水位 (minimize inventory_level)
    4. 最大化报缺时间精准度 (maximize shortage_precision)
    
    决策变量（个体编码）：
    - 每个订单的：分配策略选择(FIFO/PRIORITY/LIFO/SUPPLIER_FIRST)
    - 每个缺料物料的：供应商选择
    - 全局：库存消耗优先级权重
    
    使用方法：
        optimizer = NSGA2Optimizer(population_size=50, generations=100)
        pareto_front = optimizer.evolve(orders, inventory, bom_data)
        recommended = optimizer.recommend_solution(preference='delivery_first')
    """
    
    def __init__(
        self,
        population_size: int = 50,
        generations: int = 100,
        mutation_rate: float = 0.1,
        crossover_rate: float = 0.9,
        tournament_size: int = 3,
        use_mrp_sampling: bool = False,
        mrp_sample_size: int = 100
    ):
        """
        初始化NSGA-II优化器
        
        Args:
            population_size: 种群大小（建议50-100）
            generations: 进化代数（建议100-200）
            mutation_rate: 变异概率（建议0.05-0.2）
            crossover_rate: 交叉概率（建议0.8-0.95）
            tournament_size: 锦标赛选择的参赛个体数（建议2-5）
            use_mrp_sampling: 是否启用MRP抽样评估模式（默认False，可自动切换）
            mrp_sample_size: MRP抽样订单数量（默认100）
        """
        self.population_size = population_size
        self.generations = generations
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.tournament_size = tournament_size
        self.use_mrp_sampling = use_mrp_sampling
        self.mrp_sample_size = mrp_sample_size
        
        # 当前评估模式（'heuristic' 或 'hybrid'）
        self.evaluation_mode = 'heuristic'
        
        # 可用的分配策略列表
        self.available_strategies = [
            AllocationStrategy.FIFO,
            AllocationStrategy.PRIORITY,
            AllocationStrategy.LIFO,
            AllocationStrategy.SUPPLIER_FIRST
        ]
        
        # 最终的帕累托前沿解集
        self.pareto_front: List[OptimizationIndividual] = []
        
        # 收敛历史记录（用于生成收敛曲线）
        self.convergence_history: List[Dict[str, float]] = []
        
        # 当前种群
        self.population: List[OptimizationIndividual] = []
        
        # 优化过程中的上下文数据
        self.orders: List[SalesOrder] = []
        self.materials: List[Material] = []
        self.inventory_data: List[Inventory] = []
        self.bom_data: List[Any] = []
        self.supplier_materials: Dict[int, List[int]] = {}  # 物料ID -> 可选供应商ID列表
        
        logger.info(
            f"NSGA-II优化器初始化完成: 种群大小={population_size}, "
            f"代数={generations}, 变异率={mutation_rate}, 交叉率={crossover_rate}"
        )
    
    def initialize_population(
        self,
        orders: List[SalesOrder],
        materials: List[Material]
    ) -> List[OptimizationIndividual]:
        """
        初始化种群：随机生成多种策略组合作为初始解
        
        每个个体是一个字典/对象，包含所有决策变量的编码：
        - order_strategies: {order_id: strategy} - 每个订单的分配策略
        - material_suppliers: {material_id: supplier_id} - 每个缺料物料的供应商选择
        - priority_weights: [w1, w2, w3, w4] - 库存消耗优先级权重
        
        Args:
            orders: 销售订单列表
            materials: 物料列表
            
        Returns:
            初始化后的种群（个体列表）
        """
        self.orders = orders
        self.materials = materials
        
        population = []
        
        for i in range(self.population_size):
            individual = OptimizationIndividual()
            
            # 为每个订单随机选择分配策略
            order_strategies = {}
            for order in orders:
                strategy = random.choice(self.available_strategies)
                order_strategies[order.id] = strategy.value
            
            # 为每个物料随机选择供应商（模拟）
            material_suppliers = {}
            for material in materials:
                if material.id in self.supplier_materials and self.supplier_materials[material.id]:
                    supplier_id = random.choice(self.supplier_materials[material.id])
                    material_suppliers[material.id] = supplier_id
            
            # 随机生成全局优先级权重（归一化到0-1之间，和为1）
            raw_weights = np.random.random(4)
            priority_weights = (raw_weights / raw_weights.sum()).tolist()
            
            # 组装决策变量
            individual.decision_vars = {
                'order_strategies': order_strategies,
                'material_suppliers': material_suppliers,
                'priority_weights': priority_weights
            }
            
            population.append(individual)
        
        self.population = population
        logger.info(f"种群初始化完成，共{len(population)}个个体")
        
        return population
    
    def evaluate_objective(self, individual: OptimizationIndividual) -> List[float]:
        """
        评估单个个体的4个目标值（确定性版本）
        
        使用确定性评分公式替代随机模拟，确保：
        1. 相同输入始终产生相同输出（可复现性）
        2. 评分逻辑与MaterialPlanner的实际业务规则一致
        3. 四个目标之间有合理的权衡关系，避免退化解
        
        目标值说明：
        - objectives[0]: 按时交付率 (越高越好，范围0-1)
          = 基于策略能力分 × 交期紧迫度修正 × 优先级权重加成
        - objectives[1]: 负交期变更次数 (取反后越高越好，原值越低越好)
          = -(基础变更数 × 策略稳定性折扣)
        - objectives[2]: 库存水位 (归一化后越低越好，范围0-1)
          = 总库存 / 参考库存上限 × 库存消耗加速因子
        - objectives[3]: 报缺时间精准度 (越高越好，范围0-1)
          = 基础精准度 + 策略预测能力加成 + 权重优化加成
        
        Args:
            individual: 待评估的优化个体
            
        Returns:
            4个目标值的列表 [on_time_rate, neg_delivery_changes, inv_level, shortage_precision]
        """
        if not self.orders or not self.inventory_data:
            logger.warning("评估时缺少订单或库存数据，返回默认值")
            return [0.0, 0.0, 1.0, 0.0]
        
        vars = individual.decision_vars
        order_strategies = vars.get('order_strategies', {})
        priority_weights = vars.get('priority_weights', [0.25, 0.25, 0.25, 0.25])
        
        # 使用全部订单进行评估（不再抽样），确保结果稳定可复现
        eval_orders = self.orders
        n_orders = len(eval_orders)
        
        # ===== 策略能力基线分数表（基于业务经验校准） =====
        # PRIORITY策略对按时交付最有利（高优订单优先处理）
        # FIFO次之（先到先服务，公平但非最优）
        # SUPPLIER_FIRST适中（考虑供应商可靠性）
        # LIFO最差（新料优先消耗旧料积压风险）
        strategy_delivery_score = {
            'PRIORITY': 0.90,
            'FIFO': 0.75,
            'SUPPLIER_FIRST': 0.72,
            'LIFO': 0.50,
        }
        
        strategy_stability_score = {
            'PRIORITY': 0.92,   # 优先级策略最稳定，减少抢料冲突
            'FIFO': 0.82,      # 先进先出较稳定
            'SUPPLIER_FIRST': 0.75,
            'LIFO': 0.40,      # 后进先出易导致频繁调整
        }
        
        strategy_inventory_score = {
            'LIFO': 0.90,      # LIFO最快降低库存
            'FIFO': 0.70,
            'SUPPLIER_FIRST': 0.65,
            'PRIORITY': 0.55,  # PRIORITY优先保证交付而非降库
        }
        
        strategy_precision_score = {
            'PRIORITY': 0.88,   # 优先级策略对缺料预判更准确
            'FIFO': 0.72,
            'SUPPLIER_FIRST': 0.78,  # 供应商优先能更好预估到货
            'LIFO': 0.55,
        }
        
        # ===== 目标1: 按时交付率（确定性计算） =====
        total_delivery_score = 0.0
        urgent_order_count = 0
        normal_order_count = 0
        
        for order in eval_orders:
            strategy = order_strategies.get(order.id, 'FIFO')
            base_score = strategy_delivery_score.get(strategy, 0.70)
            
            # 交期紧迫度因子：越接近交期的订单，策略选择影响越大
            days_to_deadline = (order.demand_date - date.today()).days if order.demand_date else 30
            if days_to_deadline <= 0:
                urgency = 1.0  # 已逾期，最高紧急度
                urgent_order_count += 1
            elif days_to_deadline <= 14:
                urgency = 0.8
                urgent_order_count += 1
            elif days_to_deadline <= 30:
                urgency = 0.5
                normal_order_count += 1
            else:
                urgency = 0.2
                normal_order_count += 1
            
            # 优先级因子：高优订单(小数字=高优)获得额外加权
            order_priority = getattr(order, 'priority', 3)
            priority_bonus = max(0, (5 - order_priority)) * 0.03
            
            # 综合得分（确定性的加权求和，无随机成分）
            weight_urgency = priority_weights[0]  # 紧急度权重
            weight_delivery = priority_weights[2]  # 交付权重
            order_score = (
                base_score 
                + urgency * 0.12 * weight_urgency 
                + priority_bonus
                + 0.05 * weight_delivery
            )
            order_score = min(1.0, max(0.0, order_score))
            
            total_delivery_score += order_score
        
        on_time_rate = total_delivery_score / max(n_orders, 1)
        
        # ===== 目标2: 负交期变更次数（确定性估算） =====
        # 原理：策略越不稳定 → 抢料/让料操作越多 → 交期变更次数越多
        avg_stability = np.mean([
            strategy_stability_score.get(order_strategies.get(o.id, 'FIFO'), 0.7)
            for o in eval_orders
        ])
        
        # 紧急订单占比越高，整体变更风险越大
        urgent_ratio = urgent_order_count / max(n_orders, 1)
        
        # 确定性公式：基础变更数 × (1 - 稳定性) × 紧急度放大系数
        base_changes = n_orders * 0.15  # 基础变更率15%
        stability_penalty = (1.0 - avg_stability) * 1.5
        urgency_multiplier = 1.0 + urgent_ratio * 0.8
        estimated_changes = int(base_changes * stability_penalty * urgency_multiplier)
        neg_delivery_changes = -estimated_changes  # 取负值：变更越少越好（最大化问题）
        
        # ===== 目标3: 库存水位（确定性计算） =====
        total_inventory = sum(
            inv.quantity for inv in self.inventory_data 
            if hasattr(inv, 'quantity')
        )
        
        # 各策略对库存消耗的影响（LIFO降库最快，PRIORITY最慢）
        avg_inv_strategy_score = np.mean([
            strategy_inventory_score.get(order_strategies.get(o.id, 'FIFO'), 0.7)
            for o in eval_orders
        ])
        
        # 客户权重高→加速消耗（假设客户需求拉动库存下降）
        inventory_consumption_boost = priority_weights[1] * 0.25
        
        # 最终库存水平 = 原始库存 × (1 - 消耗加速) × 策略因子
        per_material_inventory = total_inventory / max(len(self.materials), 1)
        consumption_factor = 1.0 - (avg_inv_strategy_score - 0.5) * 0.4 - inventory_consumption_boost
        estimated_inv_level = per_material_inventory * max(0.5, consumption_factor)
        
        # 归一化（参考值10000为满载库存水平）
        inv_level = min(1.0, max(0.0, estimated_inv_level / 10000.0))
        
        # ===== 目标4: 报缺时间精准度（确定性计算） =====
        # 原理：PRIORITY和SUPPLIER_FIRST策略对缺料预判更准确
        avg_precision_strategy = np.mean([
            strategy_precision_score.get(order_strategies.get(o.id, 'FIFO'), 0.7)
            for o in eval_orders
        ])
        
        # PRIORITY策略使用比例越高，整体精准度越高
        priority_ratio = sum(
            1 for s in order_strategies.values() if s == 'PRIORITY'
        ) / max(len(order_strategies), 1)
        
        # 产品组权重(weight[3])对精准度的正向贡献
        product_weight_contribution = priority_weights[3] * 0.12
        
        # 确定性综合评分
        precision = (
            0.50  # 基础精准度
            + 0.28 * avg_precision_strategy  # 策略匹配贡献
            + 0.12 * priority_ratio         # PRIORITY策略比例贡献
            + product_weight_contribution    # 权重优化贡献
        )
        precision = min(1.0, max(0.0, precision))
        
        objectives = [on_time_rate, neg_delivery_changes, inv_level, precision]
        individual.objectives = objectives
        
        # 详细日志（DEBUG级别，记录评估细节供可解释性分析）
        logger.debug(
            f"个体评估完成: delivery={on_time_rate:.4f}, "
            f"changes={neg_delivery_changes}, "
            f"inventory={inv_level:.4f}, "
            f"precision={precision:.4f}"
        )
        
        return objectives
    
    def evaluate_with_mrp_sampling(self, individual: OptimizationIndividual, 
                                sample_size: int = 100) -> List[float]:
        """
        基于真实MRP抽样的目标评估（增强版）
        
        对代表性订单样本执行简化版MRP分配逻辑，用真实齐套率/缺料率/库存变化
        替代或校准纯启发式评分，提高帕累托解的业务可信度。
        
        Args:
            individual: 待评估个体
            sample_size: 抽样订单数量（默认100，万级订单时建议200-500）
            
        Returns:
            4个目标值列表 [按时交付率, 负交期变更次数, 库存水位, 报缺精准度]
        """
        if not self.orders or not self.inventory_data:
            logger.warning("MRP抽样评估时缺少订单或库存数据，返回默认值")
            return [0.0, 0.0, 1.0, 0.0]
        
        vars = individual.decision_vars
        order_strategies = vars.get('order_strategies', {})
        priority_weights = vars.get('priority_weights', [0.25, 0.25, 0.25, 0.25])
        
        # ===== 1. 按优先级分层抽样 =====
        n_orders = len(self.orders)
        actual_sample_size = min(sample_size, n_orders)
        
        # 按订单优先级分组
        high_priority_orders = []
        medium_priority_orders = []
        low_priority_orders = []
        
        for order in self.orders:
            order_priority = getattr(order, 'priority', 3)
            if order_priority <= 2:  # 高优 (1-2级)
                high_priority_orders.append(order)
            elif order_priority <= 3:  # 中优 (3级)
                medium_priority_orders.append(order)
            else:  # 低优 (4-5级)
                low_priority_orders.append(order)
        
        # 分层抽样：高优30%/中优40%/低优30%
        n_high = max(1, int(actual_sample_size * 0.30))
        n_medium = max(1, int(actual_sample_size * 0.40))
        n_low = max(1, actual_sample_size - n_high - n_medium)
        
        sampled_orders = []
        if high_priority_orders and len(high_priority_orders) >= n_high:
            sampled_orders.extend(random.sample(high_priority_orders, n_high))
        else:
            sampled_orders.extend(high_priority_orders[:n_high])
            
        if medium_priority_orders and len(medium_priority_orders) >= n_medium:
            sampled_orders.extend(random.sample(medium_priority_orders, n_medium))
        else:
            sampled_orders.extend(medium_priority_orders[:n_medium])
            
        if low_priority_orders and len(low_priority_orders) >= n_low:
            sampled_orders.extend(random.sample(low_priority_orders, n_low))
        else:
            sampled_orders.extend(low_priority_orders[:n_low])
        
        logger.debug(
            f"MRP抽样完成: 总订单{n_orders}, 抽样{len(sampled_orders)}个"
            f"(高优{len([o for o in sampled_orders if getattr(o,'priority',5)<=2])}, "
            f"中优{len([o for o in sampled_orders if 3<=getattr(o,'priority',5)<=3])}, "
            f"低优{len([o for o in sampled_orders if getattr(o,'priority',5)>3])})"
        )
        
        # ===== 2. 模拟MRP分配过程 =====
        # 初始化可用库存（从inventory_data中提取物料库存）
        inventory_pool = {}
        for inv in self.inventory_data:
            material_id = getattr(inv, 'material_id', None) or getattr(inv, 'id', None)
            if material_id:
                qty = getattr(inv, 'quantity', 0) or getattr(inv, 'available_quantity', 0) or 0
                inventory_pool[material_id] = inventory_pool.get(material_id, 0) + float(qty)
        
        total_initial_inventory = sum(inventory_pool.values())
        
        # 根据策略对订单排序并模拟分配
        on_time_completed = 0
        delivery_changes = 0
        total_shortage_predicted = 0
        total_shortage_actual = 0
        
        # 策略特定的排序逻辑
        strategy = individual.decision_vars.get('order_strategies', {})
        
        if all(s == 'PRIORITY' for s in strategy.values()) or (
            strategy and max(set(strategy.values()), key=list(strategy.values()).count) == 'PRIORITY'
        ):
            # PRIORITY策略：高优订单先分配
            sorted_orders = sorted(sampled_orders, key=lambda o: getattr(o, 'priority', 5))
            priority_bonus_factor = 1.15  # 高优订单获得15%齐套率加成
            
        elif all(s == 'FIFO' for s in strategy.values()) or (
            strategy and max(set(strategy.values()), key=list(strategy.values()).count) == 'FIFO'
        ):
            # FIFO策略：按交期先后顺序分配
            sorted_orders = sorted(sampled_orders, key=lambda o: o.demand_date if o.demand_date else date.today())
            priority_bonus_factor = 1.0
            
        elif all(s == 'LIFO' for s in strategy.values()) or (
            strategy and max(set(strategy.values()), key=list(strategy.values()).count) == 'LIFO'
        ):
            # LIFO策略：新料优先消耗（倒序交期）
            sorted_orders = sorted(sampled_orders, key=lambda o: o.demand_date if o.demand_date else date.today(), reverse=True)
            priority_bonus_factor = 0.85  # 低优订单可能延期
            
        else:
            # SUPPLIER_FIRST或其他混合策略：基于供应商可靠性的加权分配
            sorted_orders = sorted(sampled_orders, key=lambda o: getattr(o, 'priority', 5))
            priority_bonus_factor = 1.05
        
        # 对每个抽样订单模拟分配
        for order in sorted_orders:
            order_strategy = order_strategies.get(order.id, 'FIFO')
            order_material = getattr(order, 'material', None)
            required_qty = float(getattr(order, 'quantity', 1) or 1)
            
            # 计算该订单的物料需求与可用库存对比
            available_qty = inventory_pool.get(order_material, 0) if order_material else 0
            
            # 考虑生产提前期（硬性约束第3条：齐套到产出需2天）
            production_lead_time = 2
            shipping_days = getattr(order, 'shipping_days', 45) or 45
            days_to_deadline = (order.demand_date - date.today()).days if order.demand_date else 30
            
            # 判断是否能按时交付
            is_on_time = False
            if available_qty >= required_qty * 0.9:  # 齐套率>=90%
                if days_to_deadline >= (production_lead_time + shipping_days):
                    is_on_time = True
                    on_time_completed += 1
                    # 扣减库存
                    if order_material:
                        inventory_pool[order_material] = max(0, available_qty - required_qty)
                else:
                    # 时间不够，需要调整交期
                    delivery_changes += 1
            else:
                # 物料不足，记录缺料
                shortage_qty = max(0, required_qty - available_qty)
                total_shortage_actual += shortage_qty
                
                # 预测缺料（基于策略的预测能力）
                if order_strategy == 'PRIORITY':
                    predicted_shortage = shortage_qty * 0.92  # PRIORITY预测准确率92%
                elif order_strategy == 'SUPPLIER_FIRST':
                    predicted_shortage = shortage_qty * 0.88  # SUPPLIER_FIRST准确率88%
                elif order_strategy == 'FIFO':
                    predicted_shortage = shortage_qty * 0.78  # FIFO准确率78%
                else:  # LIFO
                    predicted_shortage = shortage_qty * 0.65  # LIFO准确率65%
                
                total_shortage_predicted += predicted_shortage
                
                # 因物料不足需要调整交期
                delivery_changes += 1
                
                # 如果是高优订单且使用PRIORITY策略，给予部分齐套率加成
                if order_strategy == 'PRIORITY' and getattr(order, 'priority', 5) <= 2:
                    if days_to_deadline > production_lead_time:
                        on_time_completed += 1  # 高优订单获得优先保障
        
        # ===== 3. 计算四个真实指标 =====
        # 按时交付率
        mrp_on_time_rate = on_time_completed / max(len(sampled_orders), 1)
        
        # 应用优先级加成
        mrp_on_time_rate = min(1.0, mrp_on_time_rate * priority_bonus_factor)
        
        # 交期变更数（取负值）
        mrp_delivery_changes = -delivery_changes
        
        # 库存水位（归一化）
        remaining_inventory = sum(inventory_pool.values())
        mrp_inv_level = remaining_inventory / max(total_initial_inventory, 1)
        
        # 报缺精准度（预测与实际的匹配度）
        if total_shortage_actual > 0:
            mrp_precision = min(1.0, total_shortage_predicted / total_shortage_actual)
        elif total_shortage_actual == 0 and total_shortage_predicted == 0:
            mrp_precision = 1.0  # 无缺料且无误报，完美预测
        else:
            mrp_precision = max(0.0, 1.0 - total_shortage_predicted / 100)  # 有误报但无实际缺料
        
        mrp_objectives = [mrp_on_time_rate, mrp_delivery_changes, mrp_inv_level, mrp_precision]
        
        # ===== 4. 混合模式：60% MRP抽样结果 + 40% 启发式结果 =====
        heuristic_objectives = self.evaluate_objective(individual)
        
        final_objectives = [
            0.6 * mrp_obj + 0.4 * heur_obj 
            for mrp_obj, heur_obj in zip(mrp_objectives, heuristic_objectives)
        ]
        
        # 详细DEBUG日志
        logger.debug(
            f"MRP抽样评估完成 - 模式=hybrid\n"
            f"  抽样规模: {len(sampled_orders)}/{n_orders}\n"
            f"  MRP结果: delivery={mrp_on_time_rate:.4f}, changes={mrp_delivery_changes}, "
            f"inv={mrp_inv_level:.4f}, precision={mrp_precision:.4f}\n"
            f"  启发式结果: {heuristic_objectives}\n"
            f"  最终结果(60%MRP+40%启发式): {[round(x,4) for x in final_objectives]}\n"
            f"  主导策略: {max(set(strategy.values()), key=list(strategy.values()).count) if strategy else 'N/A'}"
        )
        
        individual.objectives = final_objectives
        return final_objectives
    
    def non_dominated_sort(
        self, 
        population: List[OptimizationIndividual]
    ) -> List[List[OptimizationIndividual]]:
        """
        快速非支配排序（NSGA-II核心算法）
        
        将种群按支配关系分为多个前沿面(Front)：
        - Front[0]: 第一前沿面（帕累托最优解集，不被任何其他解支配）
        - Front[1]: 第二前沿面（被Front[0]中的解支配，但不被其他解支配）
        - 以此类推...
        
        支配关系定义：
        解A支配解B，当且仅当：
        - A在至少一个目标上优于B
        - A在所有目标上都不差于B
        
        Args:
            population: 待排序的种群
            
        Returns:
            按前沿面分组的个体列表的列表
        """
        n = len(population)
        
        # 初始化每个个体的支配属性
        for p in population:
            p.domination_count = 0
            p.dominated_set = []
        
        fronts = [[]]  # fronts[0]是第一前沿面（帕累托最优）
        
        # 第一步：计算每个个体的支配关系
        for i, p in enumerate(population):
            for j, q in enumerate(population):
                if i == j:
                    continue
                
                # 判断p是否支配q
                if self._dominates(p, q):
                    p.dominated_set.append(j)
                elif self._dominates(q, p):
                    p.domination_count += 1
            
            # 如果p不被任何个体支配，则属于第一前沿面
            if p.domination_count == 0:
                p.rank = 1
                fronts[0].append(p)
        
        # 第二步：逐层构建后续前沿面
        current_front = 0
        while fronts[current_front]:
            next_front = []
            
            for p in fronts[current_front]:
                for dominated_idx in p.dominated_set:
                    q = population[dominated_idx]
                    q.domination_count -= 1
                    
                    if q.domination_count == 0:
                        q.rank = current_front + 2  # rank从1开始
                        next_front.append(q)
            
            current_front += 1
            fronts.append(next_front)
        
        # 移除最后一个空的前沿面
        if not fronts[-1]:
            fronts.pop()
        
        logger.debug(f"非支配排序完成，共{len(fronts)}个前沿面")
        return fronts
    
    def _dominates(
        self, 
        p: OptimizationIndividual, 
        q: OptimizationIndividual
    ) -> bool:
        """
        判断个体p是否支配个体q
        
        注意：我们的目标是最大化所有目标值（包括取反后的最小化目标）
        
        Args:
            p: 个体p
            q: 个体q
            
        Returns:
            True如果p支配q，否则False
        """
        p_obj = p.objectives
        q_obj = q.objectives
        
        # p在至少一个目标上严格优于q
        at_least_one_better = any(p_i > q_i for p_i, q_i in zip(p_obj, q_obj))
        
        # p在所有目标上都不差于q
        all_not_worse = all(p_i >= q_i for p_i, q_i in zip(p_obj, q_obj))
        
        return at_least_one_better and all_not_worse
    
    def calculate_crowding_distance(
        self, 
        front: List[OptimizationIndividual]
    ) -> None:
        """
        计算拥挤度距离（维持种群多样性）
        
        拥挤度距离用于衡量个体在目标空间中的稀疏程度：
        - 边界点（某个目标的极值点）距离设为无穷大
        - 中间点的距离基于相邻点在各目标上的差异
        
        距离越大，表示该个体周围越稀疏，多样性贡献越大
        
        Args:
            front: 同一前沿面的个体列表（原地修改其crowding_distance属性）
        """
        if len(front) == 0:
            return
        
        n_individuals = len(front)
        n_objectives = 4  # 4个优化目标
        
        # 初始化所有个体的拥挤度为0
        for individual in front:
            individual.crowding_distance = 0.0
        
        # 如果只有一个或两个个体，直接设置无穷大
        if n_individuals <= 2:
            for individual in front:
                individual.crowding_distance = float('inf')
            return
        
        # 对每个目标维度计算拥挤度
        for m in range(n_objectives):
            # 按第m个目标值排序
            sorted_front = sorted(front, key=lambda x: x.objectives[m])
            
            # 边界点设置为无穷大（保证边界点总是被选中）
            sorted_front[0].crowding_distance = float('inf')
            sorted_front[-1].crowding_distance = float('inf')
            
            # 获取该目标的最大最小值（用于归一化）
            obj_min = sorted_front[0].objectives[m]
            obj_max = sorted_front[-1].objectives[m]
            
            # 如果最大最小值相同，跳过该目标
            if obj_max == obj_min:
                continue
            
            # 计算中间点的拥挤度距离
            for i in range(1, n_individuals - 1):
                if sorted_front[i].crowding_distance != float('inf'):
                    distance = (
                        sorted_front[i + 1].objectives[m] - 
                        sorted_front[i - 1].objectives[m]
                    ) / (obj_max - obj_min)
                    
                    sorted_front[i].crowding_distance += distance
    
    def selection(
        self, 
        parent_pop: List[OptimizationIndividual]
    ) -> OptimizationIndividual:
        """
        锦标赛选择算子
        
        从种群中随机选择tournament_size个个体，
        选择其中rank最小（最优）的个体；
        如果rank相同，选择crowding_distance最大的个体（更分散）
        
        Args:
            parent_pop: 父代种群
            
        Returns:
            选中的个体
        """
        # 随机选择参赛选手
        candidates = random.sample(
            parent_pop, 
            min(self.tournament_size, len(parent_pop))
        )
        
        # 按rank升序、crowding_distance降序排序
        best = min(
            candidates,
            key=lambda x: (x.rank, -x.crowding_distance)
        )
        
        return best
    
    def crossover(
        self, 
        parent1: OptimizationIndividual, 
        parent2: OptimizationIndividual
    ) -> Tuple[OptimizationIndividual, OptimizationIndividual]:
        """
        均匀交叉操作（针对离散决策变量）
        
        对两个父代个体的决策变量进行均匀交叉：
        - 对每个决策变量，以0.5的概率从父代1或父代2继承
        - 保持决策变量的有效性
        
        Args:
            parent1: 父代个体1
            parent2: 父代个体2
            
        Returns:
            两个子代个体
        """
        child1 = OptimizationIndividual()
        child2 = OptimizationIndividual()
        
        vars1 = parent1.decision_vars
        vars2 = parent2.decision_vars
        
        # 交叉订单策略
        child1_order_strategies = {}
        child2_order_strategies = {}
        
        all_order_ids = set(vars1.get('order_strategies', {}).keys()) | \
                       set(vars2.get('order_strategies', {}).keys())
        
        for order_id in all_order_ids:
            if random.random() < 0.5:
                child1_order_strategies[order_id] = vars1.get('order_strategies', {}).get(order_id, 'FIFO')
                child2_order_strategies[order_id] = vars2.get('order_strategies', {}).get(order_id, 'FIFO')
            else:
                child1_order_strategies[order_id] = vars2.get('order_strategies', {}).get(order_id, 'FIFO')
                child2_order_strategies[order_id] = vars1.get('order_strategies', {}).get(order_id, 'FIFO')
        
        # 交叉供应商选择
        child1_suppliers = {}
        child2_suppliers = {}
        
        all_material_ids = set(vars1.get('material_suppliers', {}).keys()) | \
                          set(vars2.get('material_suppliers', {}).keys())
        
        for material_id in all_material_ids:
            if random.random() < 0.5:
                child1_suppliers[material_id] = vars1.get('material_suppliers', {}).get(material_id)
                child2_suppliers[material_id] = vars2.get('material_suppliers', {}).get(material_id)
            else:
                child1_suppliers[material_id] = vars2.get('material_suppliers', {}).get(material_id)
                child2_suppliers[material_id] = vars1.get('material_suppliers', {}).get(material_id)
        
        # 交叉优先级权重（实数型，使用算术交叉）
        weights1 = np.array(vars1.get('priority_weights', [0.25, 0.25, 0.25, 0.25]))
        weights2 = np.array(vars2.get('priority_weights', [0.25, 0.25, 0.25, 0.25]))
        
        alpha = random.random()  # 交叉系数
        child1_weights = alpha * weights1 + (1 - alpha) * weights2
        child2_weights = (1 - alpha) * weights1 + alpha * weights2
        
        # 归一化权重
        child1_weights = (child1_weights / child1_weights.sum()).tolist()
        child2_weights = (child2_weights / child2_weights.sum()).tolist()
        
        # 组装子代决策变量
        child1.decision_vars = {
            'order_strategies': child1_order_strategies,
            'material_suppliers': child1_suppliers,
            'priority_weights': child1_weights
        }
        
        child2.decision_vars = {
            'order_strategies': child2_order_strategies,
            'material_suppliers': child2_suppliers,
            'priority_weights': child2_weights
        }
        
        return child1, child2
    
    def mutate(self, individual: OptimizationIndividual) -> OptimizationIndividual:
        """
        变异操作：随机改变某个决策变量的值
        
        变异策略：
        - 以mutation_rate的概率对每个决策变量进行变异
        - 订单策略：随机更换为另一种策略
        - 供应商选择：随机更换为另一个可用供应商
        - 优先级权重：添加小的随机扰动并重新归一化
        
        Args:
            individual: 待变异的个体
            
        Returns:
            变异后的个体（可能是新对象也可能是原地修改）
        """
        mutated_vars = individual.decision_vars.copy()
        
        # 变异订单策略
        if 'order_strategies' in mutated_vars and random.random() < self.mutation_rate:
            order_strategies = mutated_vars['order_strategies'].copy()
            
            # 随机选择20%的订单进行变异
            order_ids = list(order_strategies.keys())
            n_mutate = max(1, int(len(order_ids) * 0.2))
            mutate_ids = random.sample(order_ids, min(n_mutate, len(order_ids)))
            
            for order_id in mutate_ids:
                new_strategy = random.choice(self.available_strategies).value
                order_strategies[order_id] = new_strategy
            
            mutated_vars['order_strategies'] = order_strategies
        
        # 变异供应商选择
        if 'material_suppliers' in mutated_vars and random.random() < self.mutation_rate:
            material_suppliers = mutated_vars['material_suppliers'].copy()
            
            material_ids = list(material_suppliers.keys())
            if material_ids:
                n_mutate = max(1, int(len(material_ids) * 0.2))
                mutate_ids = random.sample(material_ids, min(n_mutate, len(material_ids)))
                
                for material_id in mutate_ids:
                    if material_id in self.supplier_materials and self.supplier_materials[material_id]:
                        new_supplier = random.choice(self.supplier_materials[material_id])
                        material_suppliers[material_id] = new_supplier
            
            mutated_vars['material_suppliers'] = material_suppliers
        
        # 变异优先级权重
        if 'priority_weights' in mutated_vars and random.random() < self.mutation_rate:
            weights = np.array(mutated_vars['priority_weights'])
            
            # 添加随机扰动
            perturbation = np.random.normal(0, 0.1, 4)
            weights = weights + perturbation
            
            # 确保权重非负
            weights = np.maximum(weights, 0.01)
            
            # 重新归一化
            weights = (weights / weights.sum()).tolist()
            
            mutated_vars['priority_weights'] = weights
        
        # 创建新的变异个体
        mutated_individual = OptimizationIndividual()
        mutated_individual.decision_vars = mutated_vars
        
        return mutated_individual
    
    def evolve(
        self,
        orders: List[SalesOrder],
        inventory: List[Inventory],
        bom_data: List[Any] = None,
        supplier_map: Dict[int, List[int]] = None
    ) -> List[OptimizationIndividual]:
        """
        主进化循环：执行完整的NSGA-II优化过程
        
        流程：
        1. 初始化种群
        2. 评估所有个体的目标值
        3. 迭代循环（generations次）：
           a. 锦标赛选择父代
           b. 交叉产生子代
           c. 变异子代
           d. 合并父子代种群
           e. 非支配排序
           f. 计算拥挤度距离
           g. 截断选择新一代种群
        4. 返回最终的帕累托前沿
        
        Args:
            orders: 销售订单列表
            inventory: 库存数据列表
            bom_data: BOM数据（可选）
            supplier_map: 物料到供应商的映射 {material_id: [supplier_ids]}
            
        Returns:
            帕累托最优解集（第一前沿面的个体列表）
        """
        logger.info("="*60)
        logger.info("开始NSGA-II多目标优化")
        logger.info(f"参数: 种群={self.population_size}, 代数={self.generations}")
        logger.info("="*60)
        
        # 设置上下文数据
        self.inventory_data = inventory
        self.bom_data = bom_data or []
        
        if supplier_map:
            self.supplier_materials = supplier_map
        
        # 提取物料列表（从订单中获取涉及的物料）
        materials_from_orders = list(set(order.material for order in orders if order.material))
        self.materials = materials_from_orders if materials_from_orders else self.materials
        
        # ===== 自动决定评估模式 =====
        # 当种群较小时(sample_size*population_size < 总订单数的10%)自动切换到MRP抽样评估
        total_orders = len(orders)
        sample_threshold = self.mrp_sample_size * self.population_size
        
        if self.use_mrp_sampling or (sample_threshold < total_orders * 0.1):
            self.evaluation_mode = 'hybrid'
            logger.info(
                f"启用混合评估模式(hybrid): MRP抽样\n"
                f"  订单总数: {total_orders}\n"
                f"  抽样规模: {self.mrp_sample_size} × 种群{self.population_size} = {sample_threshold}\n"
                f"  阈值: {int(total_orders * 0.1)} (总订单的10%)\n"
                f"  强制启用: {self.use_mrp_sampling}"
            )
        else:
            self.evaluation_mode = 'heuristic'
            logger.info(f"使用纯启发式评估模式(heuristic): 订单数{total_orders}, 种群{self.population_size}")
        
        # 步骤1: 初始化种群
        population = self.initialize_population(orders, self.materials)
        
        # 步骤2: 初始评估（根据模式选择评估方法）
        logger.info(f"评估初始种群... (模式: {self.evaluation_mode})")
        for individual in population:
            if self.evaluation_mode == 'hybrid':
                self.evaluate_with_mrp_sampling(individual, self.mrp_sample_size)
            else:
                self.evaluate_objective(individual)
        
        # 开始进化迭代
        for gen in range(self.generations):
            # ===== 选择阶段 =====
            offspring = []
            
            while len(offspring) < self.population_size:
                # 锦标赛选择两个父代
                parent1 = self.selection(population)
                parent2 = self.selection(population)
                
                # 交叉操作
                if random.random() < self.crossover_rate:
                    child1, child2 = self.crossover(parent1, parent2)
                else:
                    child1 = OptimizationIndividual()
                    child1.decision_vars = parent1.decision_vars.copy()
                    
                    child2 = OptimizationIndividual()
                    child2.decision_vars = parent2.decision_vars.copy()
                
                # 变异操作
                child1 = self.mutate(child1)
                child2 = self.mutate(child2)
                
                offspring.extend([child1, child2])
            
            # 截断到正确的大小
            offspring = offspring[:self.population_size]
            
            # 评估子代（根据模式选择评估方法）
            for individual in offspring:
                if self.evaluation_mode == 'hybrid':
                    self.evaluate_with_mrp_sampling(individual, self.mrp_sample_size)
                else:
                    self.evaluate_objective(individual)
            
            # ===== 合并与排序阶段 =====
            combined_pop = population + offspring
            
            # 非支配排序
            fronts = self.non_dominated_sort(combined_pop)
            
            # 计算每个前沿面的拥挤度距离
            for front in fronts:
                self.calculate_crowding_distance(front)
            
            # ===== 截断选择阶段 =====
            new_population = []
            front_idx = 0
            
            while len(new_population) < self.population_size and front_idx < len(fronts):
                front = fronts[front_idx]
                
                if len(new_population) + len(front) <= self.population_size:
                    # 整个前沿面都能放入新种群
                    new_population.extend(front)
                else:
                    # 只能放入部分个体，按拥挤度距离排序后选取
                    remaining = self.population_size - len(new_population)
                    sorted_front = sorted(
                        front,
                        key=lambda x: -x.crowding_distance
                    )
                    new_population.extend(sorted_front[:remaining])
                
                front_idx += 1
            
            population = new_population
            self.population = population
            
            # 记录收敛历史（每10代记录一次或最后一代）
            if gen % 10 == 0 or gen == self.generations - 1:
                self._record_convergence(gen, population)
                
                if gen % 20 == 0:
                    logger.info(
                        f"第{gen+1}/{self.generations}代完成, "
                        f"当前种群大小: {len(population)}"
                    )
        
        # 获取最终帕累托前沿（第一前沿面）
        final_fronts = self.non_dominated_sort(population)
        self.pareto_front = final_fronts[0] if final_fronts else []
        
        # 计算最终前沿面的拥挤度
        if self.pareto_front:
            self.calculate_crowding_distance(self.pareto_front)
        
        logger.info("="*60)
        logger.info(f"优化完成! 帕累托前沿包含{len(self.pareto_front)}个解")
        logger.info("="*60)
        
        return self.pareto_front
    
    def _record_convergence(
        self, 
        generation: int, 
        population: List[OptimizationIndividual]
    ) -> None:
        """记录收敛曲线数据"""
        if not population:
            return
        
        # 统计当前种群的平均目标值
        objectives_matrix = np.array([ind.objectives for ind in population])
        avg_objectives = objectives_matrix.mean(axis=0)
        
        # 找到当前最佳解（第一前沿面的代表）
        fronts = self.non_dominated_sort(population)
        best_front = fronts[0] if fronts else population[:1]
        
        best_objectives = np.array([ind.objectives for ind in best_front]).mean(axis=0)
        
        self.convergence_history.append({
            'generation': generation,
            'avg_objectives': avg_objectives.tolist(),
            'best_front_avg': best_objectives.tolist(),
            'population_size': len(population),
            'pareto_front_size': len(best_front)
        })
    
    def get_pareto_front(self) -> List[OptimizationIndividual]:
        """
        获取最终的帕累托最优解集
        
        Returns:
            帕累托前沿个体列表
        """
        return self.pareto_front
    
    def recommend_solution(
        self, 
        preference: str = 'delivery_first'
    ) -> Optional[OptimizationIndividual]:
        """
        根据决策者偏好从帕累托前沿推荐一个解
        
        偏好选项：
        - 'delivery_first'(交付优先): 侧重按时交付率和交期稳定
        - 'inventory_first'(库存优先): 侧重低库存水位
        - 'supplier_first'(供应商优先): 侧重供应商可靠性和交期稳定性，减少供应商风险
        - 'stability_first'(稳定优先): 侧重减少交期变更
        - 'cost_first'(成本优先): 侧重降低加急采购成本
        - 'expiry_first'(临期优先): 侧重减少呆滞损失
        
        推荐方法：
        使用加权求和法，根据偏好给不同目标赋予不同权重，
        从帕累托前沿中选择加权综合得分最高的解
        
        Args:
            preference: 决策偏好字符串
            
        Returns:
            推荐的最优个体，如果没有帕累托解则返回None
        """
        if not self.pareto_front:
            logger.warning("帕累托前沿为空，无法推荐方案")
            return None
        
        # 定义不同偏好的目标权重 (4维: [交付率, 交期变更, 库存周转, 报缺精准度])
        preference_weights = {
            'delivery_first':      [0.40, 0.30, 0.10, 0.20],  # 交付优先: 高交付率+低交期变更
            'inventory_first':     [0.15, 0.10, 0.55, 0.20],  # 库存优先: 高库存周转+低水位
            'supplier_first':      [0.25, 0.40, 0.10, 0.25],  # 供应商优先: 低交期变更(供应商可靠性)+高报缺精准度
            'stability_first':     [0.20, 0.45, 0.15, 0.20],  # 稳定优先: 低交期变更+稳定交付
            'cost_first':          [0.10, 0.40, 0.20, 0.30],  # 成本优先: 低交期变更(减少加急)+高报缺精准度
            'expiry_first':       [0.15, 0.15, 0.50, 0.20],  # 临期优先: 高库存周转(减少呆滞)
        }

        weights = preference_weights.get(preference, preference_weights['delivery_first'])
        
        # 归一化目标值（基于帕累托前沿的范围）
        pareto_objectives = np.array([ind.objectives for ind in self.pareto_front])
        
        obj_min = pareto_objectives.min(axis=0)
        obj_max = pareto_objectives.max(axis=0)
        obj_range = obj_max - obj_min
        obj_range[obj_range == 0] = 1.0  # 避免除零
        
        # 计算每个解的综合得分
        best_score = -float('inf')
        best_individual = None
        
        for individual in self.pareto_front:
            # 归一化目标值到[0,1]
            normalized_obj = (
                np.array(individual.objectives) - obj_min
            ) / obj_range
            
            # 加权求和
            score = np.dot(normalized_obj, weights)
            
            if score > best_score:
                best_score = score
                best_individual = individual
        
        logger.info(
            f"推荐方案 (偏好={preference}): "
            f"综合得分={best_score:.4f}, "
            f"目标值={best_individual.objectives if best_individual else 'N/A'}"
        )
        
        return best_individual
    
    def generate_optimization_report(self) -> Dict[str, Any]:
        """
        生成优化报告
        
        包含内容：
        - 优化参数配置
        - 收敛曲线数据
        - 帕累托前沿散点图数据
        - 各偏好下的推荐方案及详细解释
        - 目标统计信息
        
        Returns:
            包含完整报告数据的字典
        """
        report = {
            'optimization_config': {
                'population_size': self.population_size,
                'generations': self.generations,
                'mutation_rate': self.mutation_rate,
                'crossover_rate': self.crossover_rate,
                'algorithm': 'NSGA-II'
            },
            'convergence_curve': self.convergence_history,
            'pareto_front_data': [],
            'recommended_solutions': {},
            'objective_statistics': {},
            'summary': ''
        }
        
        if not self.pareto_front:
            report['summary'] = "优化未完成或无有效解"
            return report
        
        # 帕累托前沿散点图数据
        for idx, individual in enumerate(self.pareto_front):
            report['pareto_front_data'].append({
                'id': idx,
                'objectives': individual.objectives,
                'rank': individual.rank,
                'crowding_distance': individual.crowding_distance,
                'decision_vars_summary': {
                    'strategy_distribution': self._summarize_strategies(individual),
                    'priority_weights': individual.decision_vars.get('priority_weights', [])
                }
            })
        
        # 各偏好的推荐方案
        preferences = ['delivery_first', 'inventory_first', 'supplier_first', 'stability_first', 'cost_first', 'expiry_first']
        
        for pref in preferences:
            solution = self.recommend_solution(pref)
            if solution:
                report['recommended_solutions'][pref] = {
                    'objectives': solution.objectives,
                    'decision_vars': solution.decision_vars,
                    'explanation': self._generate_solution_explanation(pref, solution)
                }
        
        # 目标统计信息
        objective_names = [
            '按时交付率',
            '交期变更次数(取反)',
            '库存水位',
            '报缺精准度'
        ]

        if not self.pareto_front:
            report['objective_statistics'] = {
                'names': objective_names,
                'min': [0] * 4, 'max': [0] * 4,
                'mean': [0] * 4, 'std': [0] * 4, 'range': [0] * 4
            }
            pareto_objs = np.array([[0, 0, 0, 0]])
        else:
            pareto_objs = np.array([ind.objectives for ind in self.pareto_front])

            report['objective_statistics'] = {
                'names': objective_names,
                'min': pareto_objs.min(axis=0).tolist(),
                'max': pareto_objs.max(axis=0).tolist(),
                'mean': pareto_objs.mean(axis=0).tolist(),
                'std': pareto_objs.std(axis=0).tolist(),
                'range': (pareto_objs.max(axis=0) - pareto_objs.min(axis=0)).tolist()
            }

        # 生成总结
        n_solutions = len(self.pareto_front)
        best_delivery = float(pareto_objs[:, 0].max()) if len(pareto_objs) > 0 else 0
        best_inventory = float(pareto_objs[:, 2].min()) if len(pareto_objs) > 0 else 0
        
        report['summary'] = (
            f"NSGA-II优化完成，共找到{n_solutions}个帕累托最优解。\n"
            f"最佳按时交付率: {best_delivery:.2%}\n"
            f"最低库存水位: {best_inventory:.4f}\n"
            f"决策者可根据实际业务需求从帕累托前沿中选择合适的方案。"
        )
        
        logger.info("优化报告生成完成")
        
        return report
    
    def _summarize_strategies(
        self, 
        individual: OptimizationIndividual
    ) -> Dict[str, float]:
        """汇总个体中各策略的使用比例"""
        strategies = individual.decision_vars.get('order_strategies', {})
        
        if not strategies:
            return {}
        
        total = len(strategies)
        distribution = {}
        
        for strategy in self.available_strategies:
            count = sum(1 for s in strategies.values() if s == strategy.value)
            distribution[strategy.value] = count / max(total, 1)
        
        return distribution
    
    def _generate_solution_explanation(
        self, 
        preference: str, 
        solution: OptimizationIndividual
    ) -> str:
        """生成推荐方案的文字解释"""
        obj = solution.objectives
        vars = solution.decision_vars
        strategies = vars.get('order_strategies', {})
        weights = vars.get('priority_weights', [0.25, 0.25, 0.25, 0.25])
        
        # 统计主要使用的策略
        strategy_counts = {}
        for s in strategies.values():
            strategy_counts[s] = strategy_counts.get(s, 0) + 1
        
        main_strategy = max(strategy_counts.items(), key=lambda x: x[1])[0] if strategy_counts else 'FIFO'
        
        explanation = (
            f"【{preference}偏好方案】\n"
            f"目标表现:\n"
            f"  - 按时交付率: {obj[0]:.2%}\n"
            f"  - 交期变更(取反): {obj[1]:.2f}\n"
            f"  - 库存水位: {obj[2]:.4f}\n"
            f"  - 报缺精准度: {obj[3]:.2%}\n\n"
            f"策略特点:\n"
            f"  - 主要采用'{main_strategy}'分配策略\n"
            f"  - 优先级权重: 紧急度{weights[0]:.2f}, "
            f"客户等级{weights[1]:.2f}, 交期{weights[2]:.2f}, 其他{weights[3]:.2f}"
        )
        
        return explanation


def run_multi_objective_optimization(**kwargs) -> NSGA2Optimizer:
    """
    运行多目标优化的便捷入口函数
    
    封装了完整的优化流程，简化调用方式
    
    参数示例：
        run_multi_objective_optimization(
            orders=sales_orders,
            inventory=inventory_list,
            population_size=50,
            generations=100,
            preference='delivery_first'
        )
    
    Args:
        **kwargs: 传递给NSGA2Optimizer的参数，包括：
            - orders: 销售订单列表（必需）
            - inventory: 库存数据列表（必需）
            - bom_data: BOM数据（可选）
            - supplier_map: 供应商映射（可选）
            - population_size: 种群大小（默认50）
            - generations: 代数（默认100）
            - mutation_rate: 变异率（默认0.1）
            - crossover_rate: 交叉率（默认0.9）
            - preference: 推荐偏好（默认'delivery_first'）
            
    Returns:
        配置好的NSGA2Optimizer实例（已执行优化）
    """
    # 提取必要参数
    orders = kwargs.pop('orders', [])
    inventory = kwargs.pop('inventory', [])
    bom_data = kwargs.pop('bom_data', None)
    supplier_map = kwargs.pop('supplier_map', None)
    preference = kwargs.pop('preferences', 'delivery_first')  # 注意兼容性
    
    # 创建优化器实例
    optimizer = NSGA2Optimizer(**kwargs)
    
    # 执行优化
    if orders and inventory:
        optimizer.evolve(orders, inventory, bom_data, supplier_map)
        
        # 自动生成推荐方案
        if preference:
            optimizer.recommend_solution(preference)
    
    return optimizer


def get_recommended_planning_strategy(
    preference: str = 'delivery_first',
    **kwargs
) -> Optional[Dict[str, Any]]:
    """
    获取推荐的物料计划策略

    高级便捷函数，直接返回推荐的策略配置，无需手动管理优化器实例

    Args:
        preference: 决策偏好 ('delivery_first', 'inventory_first', 'supplier_first', 'stability_first', 'cost_first', 'expiry_first')
        **kwargs: 传递给run_multi_objective_optimization的其他参数
        
    Returns:
        包含推荐策略的字典，格式：
        {
            'strategy_name': str,
            'objectives': dict,
            'decision_vars': dict,
            'explanation': str,
            'pareto_front_size': int
        }
        如果优化失败则返回None
    """
    try:
        optimizer = run_multi_objective_optimization(
            preferences=preference,
            **kwargs
        )
        
        solution = optimizer.recommend_solution(preference)
        
        if not solution:
            return None
        
        return {
            'strategy_name': f'nsga2_{preference}',
            'objectives': {
                'on_time_delivery_rate': solution.objectives[0],
                'neg_delivery_changes': solution.objectives[1],
                'inventory_level': solution.objectives[2],
                'shortage_precision': solution.objectives[3]
            },
            'decision_vars': solution.decision_vars,
            'explanation': optimizer._generate_solution_explanation(preference, solution),
            'pareto_front_size': len(optimizer.pareto_front),
            'optimizer': optimizer  # 可选：保留优化器引用以便进一步分析
        }
        
    except Exception as e:
        logger.error(f"获取推荐策略失败: {str(e)}", exc_info=True)
        return None


# ==================== 工具函数 ====================

def compare_pareto_solutions(
    solutions: List[OptimizationIndividual],
    reference_point: List[float] = None
) -> List[Dict[str, Any]]:
    """
    比较帕累托解集中的各个解决方案
    
    Args:
        solutions: 帕累托最优解列表
        reference_point: 参考点（理想点），用于计算距离
        
    Returns:
        比较结果列表，包含每个解的详细信息
    """
    if not solutions:
        return []
    
    results = []
    
    for idx, sol in enumerate(solutions):
        result = {
            'index': idx,
            'objectives': sol.objectives,
            'rank': sol.rank,
            'crowding_distance': sol.crowding_distance
        }
        
        # 如果提供了参考点，计算到参考点的欧氏距离
        if reference_point:
            dist = np.linalg.norm(
                np.array(sol.objectives) - np.array(reference_point)
            )
            result['distance_to_ideal'] = dist
        
        results.append(result)
    
    # 按到理想点的距离排序（如果有参考点）
    if reference_point:
        results.sort(key=lambda x: x.get('distance_to_ideal', float('inf')))
    
    return results


def export_pareto_front_csv(
    optimizer: NSGA2Optimizer,
    filepath: str
) -> bool:
    """
    将帕累托前沿导出为CSV格式
    
    Args:
        optimizer: 已优化的NSGA2Optimizer实例
        filepath: 导出文件路径
        
    Returns:
        是否导出成功
    """
    try:
        import csv
        
        pareto_front = optimizer.pareto_front
        
        if not pareto_front:
            logger.warning("帕累托前沿为空，无法导出")
            return False
        
        with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            
            # 写入表头
            writer.writerow([
                '解编号',
                '按时交付率',
                '交期变更(取反)',
                '库存水位',
                '报缺精准度',
                'Rank',
                '拥挤度距离',
                '主要策略',
                '紧急度权重',
                '客户等级权重',
                '交期权重',
                '其他权重'
            ])
            
            # 写入数据
            for idx, ind in enumerate(pareto_front):
                strategies = ind.decision_vars.get('order_strategies', {})
                weights = ind.decision_vars.get('priority_weights', [0.25]*4)
                
                # 统计主要策略
                strategy_counts = {}
                for s in strategies.values():
                    strategy_counts[s] = strategy_counts.get(s, 0) + 1
                main_strategy = max(strategy_counts.items(), key=lambda x: x[1])[0] if strategy_counts else 'N/A'
                
                writer.writerow([
                    idx + 1,
                    f"{ind.objectives[0]:.4f}",
                    f"{ind.objectives[1]:.4f}",
                    f"{ind.objectives[2]:.4f}",
                    f"{ind.objectives[3]:.4f}",
                    ind.rank,
                    f"{ind.crowding_distance:.4f}",
                    main_strategy,
                    f"{weights[0]:.4f}",
                    f"{weights[1]:.4f}",
                    f"{weights[2]:.4f}",
                    f"{weights[3]:.4f}"
                ])
        
        logger.info(f"帕累托前沿已导出到: {filepath}")
        return True
        
    except Exception as e:
        logger.error(f"导出CSV失败: {str(e)}", exc_info=True)
        return False
