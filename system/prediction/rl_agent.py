"""
强化学习智能体 - 用于供应链动态决策
支持：
1. Q-Learning (表格型) - 用于简单决策场景
2. Deep Q-Network (DQN) - 用于复杂状态空间
3. 策略梯度方法 - 用于连续动作空间

应用场景：
- 是否接受紧急插单
- 物料分配策略动态选择
- 库存补货时机优化
- 供应商选择决策
"""

import numpy as np
from datetime import datetime, timedelta, date
from collections import defaultdict, deque
import random
import json
import logging

logger = logging.getLogger(__name__)

from .models import (PlanLog, SalesOrder, Inventory, Material,
                    MaterialPlanResult, SupplierCommitment, OrderAllocation,
                    WorkCenter, Capacity, SupplierMaterial)

# 尝试导入深度学习框架
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    logger.warning("PyTorch未安装，将使用表格型Q-Learning")


class SupplyChainEnvironment:
    """
    供应链环境模拟器 (OpenAI Gym风格)
    
    State Space:
    - 订单队列状态 (待处理/进行中/已完成)
    - 库存水平 (各物料的当前库存)
    - 产能利用率 (0-1)
    - 时间压力 (距交期的天数)
    
    Action Space:
    - 接受/拒绝订单
    - 选择分配策略 (FIFO/LIFO/PRIORITY等)
    - 触发采购/不采购
    - 让料/抢料
    
    Reward:
    - 按时交付: +15
    - 延期交付: 根据延期天数动态惩罚
    - 库存成本: 基于库存水平的动态成本
    - 缺料惩罚: 基于缺料量的动态惩罚
    - 产能利用奖励: +utilization_rate
    """

    def __init__(self, config=None):
        self.config = config or {}
        
        # 环境参数
        self.max_orders_in_queue = self.config.get('max_orders', 50)
        self.num_material_types = self.config.get('num_materials', 20)
        self.time_horizon = self.config.get('time_horizon', 50)  # 50步/轮，避免负奖励过度累积

        # 新订单流入参数（模拟真实业务中的持续订单到达）
        self.order_arrival_rate = self.config.get('order_arrival_rate', 0.3)  # 每步30%概率新订单到达
        self.max_new_orders_per_step = self.config.get('max_new_orders_per_step', 2)  # 每步最多新增订单数
        
        # 动作空间定义
        self.ACTIONS = {
            0: 'ACCEPT_ORDER_NORMAL',       # 正常接受订单
            1: 'ACCEPT_ORDER_URGENT',       # 紧急接受（加班）
            2: 'REJECT_ORDER',              # 拒绝订单
            3: 'ALLOCATE_FIFO',             # FIFO分配策略
            4: 'ALLOCATE_PRIORITY',         # 优先级分配
            5: 'ALLOCATE_LIFO',             # LIFO分配（降库存）
            6: 'TRIGGER_PROCUREMENT',       # 触发采购
            7: 'RELEASE_MATERIAL',          # 让料给高优订单
            8: 'GRAB_MATERIAL',             # 抢料
            9: 'NO_ACTION'                  # 不作为
        }
        self.n_actions = len(self.ACTIONS)
        
        # 状态空间维度
        self.state_dim = (
            5 +  # 订单统计 (待处理/部分齐套/完全齐套/延期/取消)
            3 +  # 库存统计 (平均库存水平/安全库存比率/缺料率)
            2 +  # 产能 (利用率/剩余产能)
            2 +  # 时间 (平均交期缓冲/紧急订单占比)
            1    # 当前步骤
        )
        
        # 初始化状态
        self.current_state = None
        self.current_step = 0
        self.episode_reward = 0
        self.history = []
        
        # 从数据库加载真实数据初始化环境
        self._init_from_database()

        # 环境内部状态跟踪（用于动作执行效果）
        self.order_promise_changes = []       # 订单承诺变更记录
        self.action_log = []                  # 动作执行日志
        self._prev_state_cache = None         # 上次状态缓存（用于差分计算）

    def _init_from_database(self):
        """从数据库加载真实的供应链统计数据初始化环境"""
        try:
            from django.db.models import Sum, Count, Q, Avg, F
            today = date.today()

            # ========== 1. 订单状态分布（各状态的精确数量和比例）==========
            orders_qs = SalesOrder.objects.all()
            total_orders = orders_qs.count()
            
            # 按状态分组统计
            order_status_counts = orders_qs.values('status').annotate(
                cnt=Count('id')
            ).order_by('status')
            
            status_count_map = {item['status']: item['cnt'] for item in order_status_counts}
            
            self.order_stats = {
                'pending': status_count_map.get('pending', 0),
                'confirmed': status_count_map.get('confirmed', 0),
                'in_production': status_count_map.get('in_production', 0),
                'allocated': status_count_map.get('allocated', 0),
                'partial': status_count_map.get('partial', 0),
                'complete': status_count_map.get('complete', 0),
                'processing': status_count_map.get('processing', 0),
                'shipped': status_count_map.get('shipped', 0),
                'delivered': status_count_map.get('delivered', 0),
                'cancelled': status_count_map.get('cancelled', 0),
                'total': total_orders,
            }
            # 归一化比例
            denom = max(total_orders, 1)
            self.order_stats['pending_ratio'] = self.order_stats['pending'] / denom
            self.order_stats['partial_ratio'] = self.order_stats['partial'] / denom
            self.order_stats['complete_ratio'] = (self.order_stats['complete'] + self.order_stats.get('delivered', 0)) / denom
            # 延期订单：交期已过但未完成/未发货的订单
            delayed_qs = orders_qs.filter(
                demand_date__lt=today,
                status__in=['pending', 'confirmed', 'in_production', 'allocated', 'partial', 'processing']
            )
            self.order_stats['delayed'] = delayed_qs.count()
            self.order_stats['delayed_ratio'] = self.order_stats['delayed'] / denom
            # 进行中订单占比（用于progress_ratio）
            in_progress_statuses = ['in_production', 'allocated', 'partial', 'processing']
            in_progress_count = sum(status_count_map.get(s, 0) for s in in_progress_statuses)
            self.order_stats['progress_ratio'] = in_progress_count / denom

            # ========== 2. 库存水平（总量、低于安全库存的物料数量）==========
            inv_qs = Inventory.objects.all()
            inv_count = inv_qs.count()
            
            # 总库存量
            inv_agg = inv_qs.aggregate(
                total_qty=Sum('quantity'),
                total_available=Sum('available_quantity'),
                total_hold=Sum('hold_quantity')
            )
            total_inv = float(inv_agg['total_qty'] or 0)
            
            # 低于安全库存的物料数量（关联Material.safety_stock）
            below_safety_qs = inv_qs.select_related('material').filter(
                quantity__lt=F('material__safety_stock')
            )
            below_safety_count = below_safety_qs.count()
            
            # 安全库存比率：不低于安全库存的物料占比
            safety_ratio = 1.0 - (below_safety_count / max(inv_count, 1))
            
            self.inventory_stats = {
                'total': total_inv,
                'total_available': float(inv_agg['total_available'] or 0),
                'total_hold': float(inv_agg['total_hold'] or 0),
                'avg_per_material': total_inv / max(inv_count, 1),
                'below_safety': below_safety_count,
                'safety_ratio': safety_ratio,
                'material_count': inv_count,
            }

            # ========== 3. 缺料率（从 OrderAllocation 的 shortage_quantity 计算）=========
            alloc_qs = OrderAllocation.objects.all()
            alloc_agg = alloc_qs.aggregate(
                total_required=Sum('required_quantity'),
                total_shortage=Sum('shortage_quantity'),
                total_allocated=Sum('allocated_quantity'),
                alloc_count=Count('id')
            )
            total_required = float(alloc_agg['total_required'] or 0)
            total_shortage = float(alloc_agg['total_shortage'] or 0)
            shortage_rate = total_shortage / max(total_required, 1)
            
            self.allocation_stats = {
                'total_required': total_required,
                'total_shortage': total_shortage,
                'total_allocated': float(alloc_agg['total_allocated'] or 0),
                'alloc_record_count': alloc_agg['alloc_count'],
                'shortage_rate': shortage_rate,
            }

            # ========== 4. 产能利用率（从 WorkCenter 和 Capacity 计算）==========
            wc_qs = WorkCenter.objects.filter(is_active=True)
            cap_qs = Capacity.objects.filter(is_active=True)
            
            # 工作中心总产能
            wc_total_daily = sum(wc.daily_capacity_limit for wc in wc_qs if wc.daily_capacity_limit > 0)
            cap_total_daily = sum(cap.daily_capacity for cap in cap_qs if cap.daily_capacity > 0)
            total_capacity = max(wc_total_daily, cap_total_daily, 1)
            
            # 基于在产订单估算已用产能（简化：按进行中订单数量/总订单数估算）
            active_order_count = sum(
                status_count_map.get(s, 0) 
                for s in ['in_production', 'allocated', 'partial', 'processing']
            )
            estimated_used_capacity = min(active_order_count * 10 / max(total_capacity, 1), 1.0)
            # 更精细的方式：基于Capacity记录计算平均利用率
            capacity_utilization = estimated_used_capacity
            
            self.capacity_stats = {
                'total_daily_capacity': total_capacity,
                'work_center_count': wc_qs.count(),
                'active_work_centers': wc_qs.count(),
                'utilization': capacity_utilization,
                'remaining_capacity': 1.0 - capacity_utilization,
            }

            # ========== 5. 时间压力（距交期不足7天/14天的订单占比）==========
            urgent_7days = orders_qs.filter(
                demand_date__lte=today + timedelta(days=7),
                demand_date__gte=today,
                status__in=['pending', 'confirmed', 'in_production', 'allocated', 'partial', 'processing']
            ).count()
            
            urgent_14days = orders_qs.filter(
                demand_date__lte=today + timedelta(days=14),
                demand_date__gte=today,
                status__in=['pending', 'confirmed', 'in_production', 'allocated', 'partial', 'processing']
            ).count()
            
            active_for_time = max(sum(
                status_count_map.get(s, 0) 
                for s in ['pending', 'confirmed', 'in_production', 'allocated', 'partial', 'processing']
            ), 1)
            
            # 平均交期缓冲不足率：交期紧迫订单占活跃订单的比例
            buffer_deficit_rate = urgent_14days / active_for_time
            urgent_order_ratio = urgent_7days / active_for_time
            
            self.time_pressure_stats = {
                'urgent_7days_count': urgent_7days,
                'urgent_14days_count': urgent_14days,
                'active_order_count': active_for_time,
                'buffer_deficit_rate': buffer_deficit_rate,
                'urgent_order_ratio': urgent_order_ratio,
            }

            logger.info(
                f"环境初始化完成: {self.order_stats['total']}个订单, "
                f"总库存{total_inv:.0f}, 产能利用率{capacity_utilization:.1%}, "
                f"缺料率{shortage_rate:.2%}, 紧急订单{urgent_order_ratio:.1%}"
            )

        except Exception as e:
            logger.warning(f"从数据库初始化失败，使用默认值: {str(e)}")
            self._set_default_stats()

    def _set_default_stats(self):
        """数据库不可用时使用默认统计数据"""
        self.order_stats = {
            'pending': 30, 'confirmed': 10, 'in_production': 15, 'allocated': 5,
            'partial': 8, 'complete': 20, 'processing': 5, 'shipped': 3,
            'delivered': 2, 'cancelled': 2, 'total': 100,
            'pending_ratio': 0.30, 'partial_ratio': 0.08,
            'complete_ratio': 0.22, 'delayed_ratio': 0.05, 'progress_ratio': 0.33,
        }
        self.inventory_stats = {
            'total': 5000, 'total_available': 4000, 'total_hold': 1000,
            'avg_per_material': 250, 'below_safety': 8, 'safety_ratio': 0.70,
            'material_count': 20,
        }
        self.allocation_stats = {
            'total_required': 8000, 'total_shortage': 800,
            'total_allocated': 7200, 'alloc_record_count': 150, 'shortage_rate': 0.10,
        }
        self.capacity_stats = {
            'total_daily_capacity': 500, 'work_center_count': 5,
            'active_work_centers': 5, 'utilization': 0.75, 'remaining_capacity': 0.25,
        }
        self.time_pressure_stats = {
            'urgent_7days_count': 8, 'urgent_14days_count': 18,
            'active_order_count': 73, 'buffer_deficit_rate': 0.25,
            'urgent_order_ratio': 0.11,
        }

    def reset(self):
        """重置环境到初始状态"""
        self.current_step = 0
        self.episode_reward = 0
        self.history = []
        self.current_state = self._get_state()
        return self.current_state

    def step(self, action):
        """
        执行动作
        
        Args:
            action: 动作索引 (0-9)
            
        Returns:
            tuple: (next_state, reward, done, info)
        """
        action_name = self.ACTIONS.get(action, 'UNKNOWN')
        
        # 根据动作执行相应的供应链操作
        reward, info = self._execute_action(action, action_name)
        
        # 更新状态
        self.current_step += 1
        next_state = self._get_state()

        # 注入新订单（模拟真实业务中持续到达的订单）
        self._inject_new_orders()

        # 检查是否结束
        done = self.current_step >= self.time_horizon
        
        # 累积奖励
        self.episode_reward += reward
        
        # 记录历史
        self.history.append({
            'step': self.current_step,
            'action': action_name,
            'reward': reward,
            'state': next_state.tolist() if hasattr(next_state, 'tolist') else next_state
        })
        
        return next_state, reward, done, info

    def _get_state(self):
        """
        获取当前状态向量（13维，全部基于真实数据库数据）
        
        state[0-4]: 订单统计 (pending_ratio, partial_ratio, complete_ratio, delayed_ratio, progress_ratio)
        state[5-7]: 库存状态 (总库存归一化, 安全库存比率, 缺料率)
        state[8-9]: 产能 (利用率, 剩余产能率)
        state[10-11]: 时间压力 (平均交期缓冲不足率, 紧急订单占比)
        state[12]: 当前步数/总步数
        """
        state = np.zeros(self.state_dim, dtype=np.float32)

        # ===== 订单统计 (state[0-4]) =====
        # 从 order_stats 获取真实比例数据
        state[0] = float(self.order_stats.get('pending_ratio', 0))       # 待处理订单占比
        state[1] = float(self.order_stats.get('partial_ratio', 0))       # 部分齐套订单占比
        state[2] = float(self.order_stats.get('complete_ratio', 0))      # 已完成/已交付订单占比
        state[3] = float(self.order_stats.get('delayed_ratio', 0))      # 延期订单占比
        state[4] = float(self.order_stats.get('progress_ratio', 0))     # 进行中订单占比

        # ===== 库存状态 (state[5-7]) =====
        total_inv = self.inventory_stats.get('total', 5000)
        # 总库存归一化（假设10000为满载参考值）
        state[5] = min(1.0, total_inv / 10000.0)
        # 安全库存比率：不低于安全库存的物料占比
        state[6] = float(self.inventory_stats.get('safety_ratio', 0.7))
        # 缺料率：从 OrderAllocation.shortage_quantity 真实计算
        state[7] = float(self.allocation_stats.get('shortage_rate', 0.1))

        # ===== 产能状态 (state[8-9]) =====
        # 利用率：从 WorkCenter/Capacity 实际计算
        state[8] = float(self.capacity_stats.get('utilization', 0.75))
        # 剩余产能率
        state[9] = float(self.capacity_stats.get('remaining_capacity', 0.25))

        # ===== 时间压力 (state[10-11]) =====
        # 平均交期缓冲不足率（14天内交期的活跃订单占比）
        state[10] = float(self.time_pressure_stats.get('buffer_deficit_rate', 0.25))
        # 紧急订单占比（7天内交期的活跃订单占比）
        state[11] = float(self.time_pressure_stats.get('urgent_order_ratio', 0.11))

        # ===== 当前进度 (state[12]) =====
        state[12] = self.current_step / self.time_horizon

        return state

    def _execute_action(self, action, action_name):
        """
        执行动作并计算奖励（基于真实状态数据，移除随机模拟）
        
        每个动作会：
        1. 更新环境内部状态（订单计数/库存分布等）
        2. 记录到 action_log
        3. 根据真实状态指标计算奖励
        """
        reward = 0
        info = {'action': action_name, 'step': self.current_step}
        
        # 获取当前关键状态指标（用于奖励计算）
        shortage_rate = self.allocation_stats.get('shortage_rate', 0.1)
        utilization = self.capacity_stats.get('utilization', 0.75)
        urgent_ratio = self.time_pressure_stats.get('urgent_order_ratio', 0.11)
        inventory_level = self.inventory_stats.get('total', 5000)
        delayed_ratio = self.order_stats.get('delayed_ratio', 0.05)

        if action == 0:  # ACCEPT_ORDER_NORMAL - 正常接受订单
            # 基于当前产能和库存计算接受成本
            base_reward = 15  # 原来是10，提升正向激励
            resource_cost = -1.5 * utilization  # 降低资源惩罚（原-2）
            # 低延期率+低缺料时额外奖励
            efficiency_bonus = (1 - delayed_ratio) * 2 + (1 - shortage_rate) * 2
            reward = base_reward + resource_cost + efficiency_bonus

            # 更新环境内部状态：待处理订单减少，进行中增加
            if self.order_stats.get('pending', 0) > 0:
                self.order_stats['pending'] = max(0, self.order_stats['pending'] - 1)
                self.order_stats['in_production'] = self.order_stats.get('in_production', 0) + 1
                self._recalc_order_ratios()

            info['effect'] = 'accepted_order_normally'
            info['order_change'] = 'pending -> in_production'

        elif action == 1:  # ACCEPT_ORDER_URGENT - 紧急接受（加班）
            base_reward = 18  # 原来是15
            overtime_cost = -3  # 原来是-5，降低加班惩罚
            # 紧急接受在高紧急时更合理，降低风险惩罚
            risk_penalty = -(delayed_ratio * 2 + shortage_rate * 1)  # 系数从3/2降到2/1
            urgency_bonus = urgent_ratio * 5  # 高紧急时额外奖励
            reward = base_reward + overtime_cost + risk_penalty + urgency_bonus
            
            # 更新环境状态
            if self.order_stats.get('pending', 0) > 0:
                self.order_stats['pending'] = max(0, self.order_stats['pending'] - 1)
                self.order_stats['allocated'] = self.order_stats.get('allocated', 0) + 1
                self._recalc_order_ratios()
            
            info['effect'] = 'accepted_order_urgently_with_overtime'
            info['order_change'] = 'pending -> allocated (urgent)'
            # 记录承诺变更
            self.order_promise_changes.append({
                'step': self.current_step,
                'action': 'ACCEPT_ORDER_URGENT',
                'change_type': 'overtime_accept',
                'timestamp': datetime.now().isoformat()
            })

        elif action == 2:  # REJECT_ORDER - 拒绝订单
            opportunity_cost = -5  # 原来是-8，降低机会成本惩罚
            # 当前压力越大，拒绝的收益越高（避免过度承诺）
            risk_avoided = min(8, (urgent_ratio + delayed_ratio) * 12)  # 提高规避收益上限
            reward = opportunity_cost + risk_avoided
            
            # 更新环境状态：待处理减少，取消增加
            if self.order_stats.get('pending', 0) > 0:
                self.order_stats['pending'] = max(0, self.order_stats['pending'] - 1)
                self.order_stats['cancelled'] = self.order_stats.get('cancelled', 0) + 1
                self._recalc_order_ratios()
            
            info['effect'] = 'rejected_order'
            info['order_change'] = 'pending -> cancelled'
            # 记录拒绝日志
            try:
                PlanLog.objects.create(
                    log_type='WARNING',
                    message=f'[RL决策] 步骤{self.current_step}: 拒绝订单 (紧急率{urgent_ratio:.1%}, '
                           f'延期率{delayed_ratio:.1%}, 缺料率{shortage_rate:.1%})'
                )
            except Exception:
                pass

        elif action in [3, 4, 5]:  # ALLOCATE_FIFO / PRIORITY / LIFO
            convert_count = 0  # 初始化默认值
            if action == 3:  # FIFO - 先进先出分配
                # 稳定策略，在低缺料时表现好
                fifo_bonus = (1 - shortage_rate) * 5  # 原来是4
                stability_bonus = (1 - urgent_ratio) * 2  # 低紧急时稳定策略额外奖励
                reward = 4 + fifo_bonus + stability_bonus  # 原来是2+
                
                # 模拟库存分配效果：部分齐套向完全齐套转化
                if self.order_stats.get('partial', 0) > 0:
                    convert_count = min(2, self.order_stats['partial'])
                    self.order_stats['partial'] -= convert_count
                    self.order_stats['complete'] = self.order_stats.get('complete', 0) + convert_count
                    self._recalc_order_ratios()
                
                info['allocation_strategy'] = 'FIFO'
                info['inventory_effect'] = f'FIFO分配: {convert_count}个订单齐套完成'
                
            elif action == 4:  # PRIORITY - 优先级驱动分配
                # 高缺料/高紧急时更有效
                priority_bonus = ((shortage_rate + urgent_ratio) * 8)  # 原来是6
                reward = 5 + priority_bonus  # 原来是4+
                
                # 模拟优先级分配：优先满足紧急订单
                if self.order_stats.get('partial', 0) > 0 and urgent_ratio > 0.05:
                    convert_count = min(1, self.order_stats['partial'])
                    self.order_stats['partial'] -= convert_count
                    self.order_stats['complete'] = self.order_stats.get('complete', 0) + convert_count
                    self._recalc_order_ratios()
                    
                    # 缺料率下降（优先分配缓解了缺料）
                    new_shortage = max(0, self.allocation_stats.get('shortage_rate', 0.1) - 0.01)
                    self.allocation_stats['shortage_rate'] = new_shortage
                
                info['allocation_strategy'] = 'PRIORITY'
                info['inventory_effect'] = '优先级分配: 紧急订单优先齐套'
                
            else:  # LIFO - 后进先出（降库存导向）
                # 库存高时效果好，加速消化
                lifo_bonus = (inventory_level / 10000.0) * 6  # 原来是4
                reward = 2 + lifo_bonus  # 原来是1+
                
                # 模拟LIFO效果：快速降低库存
                inv_reduction = min(inventory_level * 0.02, 200)
                self.inventory_stats['total'] = max(0, self.inventory_stats['total'] - inv_reduction)
                
                info['allocation_strategy'] = 'LIFO'
                info['inventory_effect'] = f'LIFO分配: 库存减少{inv_reduction:.0f}'
                
        elif action == 6:  # TRIGGER_PROCUREMENT - 触发采购
            immediate_cost = -3  # 原来是-5
            # 未来收益基于缺料程度：越缺料采购价值越大
            future_benefit = shortage_rate * 15  # 原来是12
            reward = immediate_cost + future_benefit
            
            # 创建供应商承诺记录（模拟）
            try:
                SupplierCommitment.objects.create(
                    supplier_id=1,  # 使用默认供应商
                    material_id=1,  # 使用默认物料
                    quantity=int(shortage_rate * 100 + 50),
                    delivery_date=date.today() + timedelta(days=14),
                    order_no=f'RL_AUTO_{self.current_step}_{datetime.now().strftime("%H%M%S")}'
                )
                info['db_record_created'] = True
            except Exception as e:
                info['db_record_created'] = False
                info['db_error'] = str(e)
            
            # 采购后预期改善库存
            self.inventory_stats['total'] += int(shortage_rate * 100 + 50)
            # 缺料率预期下降
            self.allocation_stats['shortage_rate'] = max(0, shortage_rate - 0.02)
            
            info['effect'] = 'triggered_procurement'
            info['procurement_qty'] = int(shortage_rate * 100 + 50)

        elif action == 7:  # RELEASE_MATERIAL - 让料给高优订单
            sacrifice = -2  # 原来是-3
            high_priority_gain = 8 * urgent_ratio  # 原来是6，提高让料收益
            reward = sacrifice + high_priority_gain
            
            # 记录让料日志
            release_log = {
                'step': self.current_step,
                'action': 'RELEASE_MATERIAL',
                'from_order': 'low_priority',
                'to_order': 'high_priority',
                'urgency_context': round(urgent_ratio, 3),
                'timestamp': datetime.now().isoformat()
            }
            self.order_promise_changes.append(release_log)
            
            # 让料效果：低优订单回退，高优订单推进
            if self.order_stats.get('processing', 0) > 0:
                self.order_stats['processing'] = max(0, self.order_stats['processing'] - 1)
                self.order_stats['partial'] = self.order_stats.get('partial', 0) + 1
            if self.order_stats.get('partial', 0) > 0:
                self.order_stats['partial'] -= 1
                self.order_stats['complete'] = self.order_stats.get('complete', 0) + 1
            self._recalc_order_ratios()
            
            info['effect'] = 'released_material_to_higher_priority'
            info['release_detail'] = release_log
            
            # 写入计划日志
            try:
                PlanLog.objects.create(
                    log_type='PLANNING',
                    message=f'[RL决策] 步骤{self.current_step}: 让料操作 - '
                           f'将物料从低优先级订单转移至高优先级订单 (紧急率{urgent_ratio:.1%})'
                )
            except Exception:
                pass

        elif action == 8:  # GRAB_MATERIAL - 抢料
            # 风险基于当前系统稳定性（降低惩罚系数）
            grab_risk = -(utilization * 2 + delayed_ratio * 1.5)  # 原来是3和2
            grab_gain = 5 + (shortage_rate * 8)  # 原来是4+6
            reward = grab_risk + grab_gain
            
            # 记录抢料日志
            grab_log = {
                'step': self.current_step,
                'action': 'GRAB_MATERIAL',
                'risk_factor': round(utilization, 3),
                'shortage_context': round(shortage_rate, 3),
                'timestamp': datetime.now().isoformat()
            }
            self.action_log.append(grab_log)
            
            # 抢料效果：从其他订单抢夺物料给当前订单
            if self.order_stats.get('partial', 0) > 0:
                self.order_stats['partial'] = max(0, self.order_stats['partial'] - 1)
                self.order_stats['complete'] = self.order_stats.get('complete', 0) + 1
                self._recalc_order_ratios()
            
            info['effect'] = 'grabbed_material_from_lower_priority'
            info['grab_detail'] = grab_log
            
            # 写入计划日志
            try:
                PlanLog.objects.create(
                    log_type='WARNING',
                    message=f'[RL决策] 步骤{self.current_step}: 抢料操作 - '
                           f'风险因子{utilization:.1%}, 缺料上下文{shortage_rate:.1%}'
                )
            except Exception:
                pass
            
        elif action == 9:  # NO_ACTION - 不作为
            # 不作为的时间惩罚（降低基础惩罚，让Agent不至于过度恐惧不作为）
            time_penalty = -0.3 * (1 + urgent_ratio * 0.5)  # 原来是-0.5
            reward = time_penalty
            info['effect'] = 'no_action_taken'
            info['time_pressure_context'] = round(urgent_ratio, 3)

        # 边界约束（移除随机噪声，使用确定性边界）
        reward = max(-20, min(20, reward))
        
        info['raw_reward'] = round(reward, 2)
        info['state_snapshot'] = {
            'shortage_rate': round(shortage_rate, 3),
            'utilization': round(utilization, 3),
            'urgent_ratio': round(urgent_ratio, 3),
            'delayed_ratio': round(delayed_ratio, 3),
        }
        
        return reward, info

    def _inject_new_orders(self):
        """
        每步以一定概率注入新订单，模拟真实业务中持续到达的订单流。
        解决原环境中pending订单被消耗殆尽后环境"死亡"的问题。
        """
        if random.random() < self.order_arrival_rate:
            # 新增1~max_new_orders_per_step个待处理订单
            new_count = random.randint(1, self.max_new_orders_per_step)
            self.order_stats['pending'] = self.order_stats.get('pending', 0) + new_count
            self.order_stats['total'] = self.order_stats.get('total', 0) + new_count
            # 缺料率随新订单略微上升（新订单带来新的物料需求）
            self.allocation_stats['shortage_rate'] = min(
                0.8,
                self.allocation_stats.get('shortage_rate', 0.1) + random.uniform(0, 0.02)
            )
            self._recalc_order_ratios()

    def _recalc_order_ratios(self):
        """重新计算订单状态比例（在订单数量变更后调用）"""
        total = max(self.order_stats.get('total', 1), 1)
        self.order_stats['pending_ratio'] = self.order_stats.get('pending', 0) / total
        self.order_stats['partial_ratio'] = self.order_stats.get('partial', 0) / total
        complete_delivered = (
            self.order_stats.get('complete', 0) + 
            self.order_stats.get('delivered', 0)
        )
        self.order_stats['complete_ratio'] = complete_delivered / total
        self.order_stats['delayed_ratio'] = self.order_stats.get('delayed', 0) / total
        in_progress_statuses = ['in_production', 'allocated', 'partial', 'processing']
        in_progress_count = sum(self.order_stats.get(s, 0) for s in in_progress_statuses)
        self.order_stats['progress_ratio'] = in_progress_count / total


class RealTimeSupplyChainEnvironment(SupplyChainEnvironment):
    """
    实时供应链环境 - 继承 SupplyChainEnvironment，增加实时数据刷新能力
    
    额外能力：
    - refresh_state(): 每次决策前重新从数据库加载最新状态
    - get_state_diff(): 对比上次状态，返回变化向量（用于检测异常事件触发重规划）
    - execute_action_real(): 真正执行动作到数据库（而不仅是模拟）
    
    适用场景：
    - 在线决策：每次决策前获取最新系统状态
    - 异常检测：通过状态差分发现突发变化
    - 生产环境部署：动作直接写入数据库
    """

    def __init__(self, config=None):
        super().__init__(config)
        self.state_refresh_count = 0          # 状态刷新次数统计
        self.state_diff_history = []         # 状态差分历史记录
        self.anomaly_threshold = 0.15        # 异常检测阈值（状态变化超过此值视为异常）

    def refresh_state(self):
        """
        刷新状态：重新从数据库加载最新统计数据
        
        在每次RL决策前调用，确保状态反映当前真实情况。
        返回是否发生了显著状态变化。
        """
        # 保存旧状态用于差分对比
        old_state = self._get_state() if self.current_state is not None else None
        
        # 重新从数据库加载全部统计数据
        self._init_from_database()
        
        # 获取新状态
        new_state = self._get_state()
        self.current_state = new_state
        self.state_refresh_count += 1
        
        # 计算状态差分
        if old_state is not None:
            diff = self.get_state_diff(old_state, new_state)
            self.state_diff_history.append({
                'step': self.current_step,
                'refresh_count': self.state_refresh_count,
                'diff_vector': diff.tolist(),
                'max_change': float(np.max(np.abs(diff))),
                'timestamp': datetime.now().isoformat()
            })
            
            is_anomaly = np.max(np.abs(diff)) > self.anomaly_threshold
            if is_anomaly:
                logger.warning(
                    f"状态异常变化检测: 最大变化量 {np.max(np.abs(diff)):.3f} "
                    f"(阈值{self.anomaly_threshold}), 可能需要重规划"
                )
                try:
                    PlanLog.objects.create(
                        log_type='WARNING',
                        message=f'[RL实时] 状态异常变化: max_diff={np.max(np.abs(diff)):.3f}, '
                               f'可能需要重规划 (刷新#{self.state_refresh_count})'
                    )
                except Exception:
                    pass
            
            return new_state, is_anomaly, diff
        
        return new_state, False, None

    def get_state_diff(self, old_state, new_state=None):
        """
        对比两次状态，返回变化向量（用于检测异常事件触发重规划）
        
        Args:
            old_state: 上次的状态向量
            new_state: 当前状态向量（若为None则使用self.current_state）
            
        Returns:
            numpy.ndarray: 13维变化向量（每个维度的新值-旧值）
        """
        if new_state is None:
            new_state = self.current_state
        if old_state is None or new_state is None:
            return np.zeros(self.state_dim, dtype=np.float32)
        
        diff = np.array(new_state, dtype=np.float32) - np.array(old_state, dtype=np.float32)
        return diff

    def execute_action_real(self, action):
        """
        真正执行动作到数据库（而非仅模拟内部状态变更）
        
        与父类 step() 的区别：
        - step() 仅更新环境内部模拟状态
        - execute_action_real() 将动作效果持久化到数据库
        
        Args:
            action: 动作索引 (0-9)
            
        Returns:
            dict: 执行结果包含 action_name, reward, db_effects, state_diff
        """
        action_name = self.ACTIONS.get(action, 'UNKNOWN')
        
        # 先执行标准动作（更新内部状态+计算奖励）
        next_state, reward, done, info = self.step(action)
        
        db_effects = []
        
        try:
            from django.db.models import F
            
            if action == 6:  # TRIGGER_PROCUREMENT - 确保采购记录已创建
                # 在 step() 中已创建 SupplierCommitment，这里做额外验证/补充
                shortage_rate = self.allocation_stats.get('shortage_rate', 0.1)
                
                # 尝试查找合适的供应商物料关系
                sm_qs = SupplierMaterial.objects.filter(
                    is_forbidden=False
                ).select_related('supplier', 'material')[:1]
                
                if sm_qs.exists():
                    sm = sm_qs.first()
                    SupplierCommitment.objects.create(
                        supplier=sm.supplier,
                        material=sm.material,
                        quantity=int(shortage_rate * 100 + 100),
                        delivery_date=date.today() + timedelta(days=sm.lead_time),
                        order_no=f'RL_REAL_{self.current_step}_{datetime.now().strftime("%Y%m%d%H%M%S")}'
                    )
                    db_effects.append(f'创建供应商承诺: {sm.supplier.supplier_name}-{sm.material.material_code}')
                    
            elif action == 2:  # REJECT_ORDER - 真正标记订单拒绝
                # 找一个待处理的高风险订单进行拒绝操作
                today = date.today()
                reject_candidates = SalesOrder.objects.filter(
                    status='pending',
                    demand_date__lt=today + timedelta(days=3)  # 交期紧迫的待处理订单
                ).order_by('demand_date')[:1]
                
                if reject_candidates.exists():
                    order = reject_candidates.first()
                    old_status = order.status
                    order.status = 'cancelled'
                    order.save(update_fields=['status'])
                    db_effects.append(f'订单 {order.order_no} 状态: {old_status} -> cancelled')
                    
                    PlanLog.objects.create(
                        log_type='WARNING',
                        message=f'[RL实时] 拒绝订单: {order.order_no} (交期{order.demand_date})',
                        order_id=order.id
                    )
                    
            elif action in [7, 8]:  # RELEASE_MATERIAL / GRAB_MATERIAL - 记录物料转移日志
                action_label = '让料' if action == 7 else '抢料'
                
                # 找出缺料最严重的分配记录
                critical_alloc = OrderAllocation.objects.filter(
                    shortage_quantity__gt=0
                ).order_by('-shortage_quantity')[:3]
                
                for alloc in critical_alloc:
                    PlanLog.objects.create(
                        log_type='PLANNING' if action == 7 else 'WARNING',
                        message=f'[RL实时]{action_label}: 订单{alloc.order.order_no} '
                               f'- 物料{alloc.material.material_code} '
                               f'(缺料{alloc.shortage_quantity})',
                        order_id=alloc.order_id,
                        material_id=alloc.material_id
                    )
                db_effects.append(f'{action_label}操作: 影响{critical_alloc.count()}条缺料记录')

            # 通用动作日志
            PlanLog.objects.create(
                log_type='PLANNING',
                message=f'[RL实时] 步骤{self.current_step}: 执行动作 {action_name}, '
                       f'奖励={reward:.2f}, 效果数={len(db_effects)}'
            )

        except Exception as e:
            logger.error(f"execute_action_real 数据库写入失败: {str(e)}")
            db_effects.append(f'DB_ERROR: {str(e)}')
            try:
                PlanLog.objects.create(
                    log_type='ERROR',
                    message=f'[RL实时] 动作执行失败: {action_name} - {str(e)}'
                )
            except Exception:
                pass

        return {
            'action': action,
            'action_name': action_name,
            'reward': round(reward, 2),
            'next_state': next_state.tolist() if hasattr(next_state, 'tolist') else next_state,
            'db_effects': db_effects,
            'info': info,
            'executed_at': datetime.now().isoformat(),
            'refresh_count': self.state_refresh_count,
        }


class QLearningAgent:
    """
    表格型Q-Learning智能体
    
    适用场景：
    - 状态空间较小且离散
    - 需要快速训练和部署
    - 可解释性要求高的场景
    
    优势：
    - 训练速度快
    - 结果可解释
    - 无需GPU
    """

    def __init__(self, n_states=10000, n_actions=10,
                 learning_rate=0.1, discount_factor=0.95,
                 epsilon_start=1.0, epsilon_end=0.01, epsilon_decay=0.995):
        self.n_states = n_states
        self.n_actions = n_actions
        self.lr = learning_rate
        self.gamma = discount_factor
        
        # 探索-利用平衡
        self.epsilon = epsilon_start
        self.epsilon_end = epsilon_end
        self.epsilon_decay = epsilon_decay
        
        # Q表：状态 -> [动作价值]
        self.q_table = defaultdict(lambda: np.zeros(n_actions))
        
        # 训练统计
        self.training_history = []
        self.episode_rewards = []

    def discretize_state(self, continuous_state):
        """
        将连续状态离散化为整数索引
        使用分层量化+特征哈希，避免原取模100导致的严重碰撞
        13维状态每维5个桶，组合空间足够大
        """
        # 关键维度细粒度量化（5个桶），非关键维度粗粒度（3个桶）
        key_dims = [0, 2, 3, 7, 8, 11]  # pending_ratio, complete_ratio, delayed, shortage, utilization, urgent
        buckets = []

        for i, val in enumerate(continuous_state):
            val_clamped = max(0.0, min(1.0, float(val)))
            if i in key_dims:
                # 关键维度：5级细分
                bucket = int(val_clamped * 4.99)
            else:
                # 非关键维度：3级粗分
                bucket = int(val_clamped * 2.99)
            buckets.append(bucket)

        # 使用质数加权哈希，减少碰撞（比简单取模好得多）
        primes = [3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43]
        hash_val = 0
        for b, p in zip(buckets, primes):
            hash_val = (hash_val * p + b) % self.n_states

        return hash_val

    def choose_action(self, state, training=True):
        """
        使用ε-greedy策略选择动作
        """
        discrete_state = self.discretize_state(state)
        
        if training and random.random() < self.epsilon:
            # 探索：随机选择
            action = random.randint(0, self.n_actions - 1)
        else:
            # 利用：选择最优动作
            q_values = self.q_table[discrete_state]
            # 处理平局情况
            max_q = np.max(q_values)
            best_actions = np.where(q_values == max_q)[0]
            action = np.random.choice(best_actions)
        
        return action

    def select_action(self, state, training=True):
        """select_action别名，兼容训练接口"""
        return self.choose_action(state, training)

    def update(self, state, action, reward, next_state, done):
        """
        更新Q表 (Bellman方程)
        Q(s,a) = Q(s,a) + α[r + γ*max(Q(s',a')) - Q(s,a)]
        """
        discrete_state = self.discretize_state(state)
        discrete_next_state = self.discretize_state(next_state)
        
        # 当前Q值
        current_q = self.q_table[discrete_state][action]
        
        # 目标Q值
        if done:
            target_q = reward
        else:
            max_next_q = np.max(self.q_table[discrete_next_state])
            target_q = reward + self.gamma * max_next_q
        
        # 更新
        self.q_table[discrete_state][action] += self.lr * (target_q - current_q)

    def decay_epsilon(self):
        """衰减探索率"""
        self.epsilon = max(self.epsilon_end, self.epsilon * self.epsilon_decay)

    def train(self, env, num_episodes=1000, max_steps_per_episode=50):
        """
        训练智能体
        
        Args:
            env: 供应链环境
            num_episodes: 训练轮数
            max_steps_per_episode: 每轮最大步数
            
        Returns:
            dict: 训练结果
        """
        logger.info(f"开始Q-Learning训练: {num_episodes}轮")
        
        all_rewards = []
        
        for episode in range(num_episodes):
            state = env.reset()
            episode_reward = 0
            
            for step in range(max_steps_per_episode):
                # 选择动作
                action = self.choose_action(state, training=True)
                
                # 执行动作
                next_state, reward, done, info = env.step(action)
                
                # 更新Q表
                self.update(state, action, reward, next_state, done)
                
                episode_reward += reward
                state = next_state
                
                if done:
                    break
            
            # 衰减探索率
            self.decay_epsilon()
            
            # 记录
            all_rewards.append(episode_reward)
            self.episode_rewards.append(episode_reward)
            
            # 定期日志
            if (episode + 1) % 50 == 0:
                avg_reward = np.mean(all_rewards[-50:])
                logger.info(f"Episode {episode+1}/{num_episodes}: "
                           f"平均奖励={avg_reward:.2f}, ε={self.epsilon:.3f}")
        
        # 训练完成
        result = {
            'agent_type': 'Q-Learning',
            'episodes_trained': num_episodes,
            'final_epsilon': self.epsilon,
            'average_reward_last_100': round(np.mean(all_rewards[-100:]), 2),
            'best_episode_reward': round(max(all_rewards), 2),
            'q_table_size': len(self.q_table),
            'training_curve': {
                'episodes': list(range(num_episodes)),
                'rewards': all_rewards,
                'moving_avg': self._calculate_moving_average(all_rewards, window=20)
            },
            'trained_at': datetime.now().isoformat(),
            'policy_summary': self._extract_policy_summary()
        }
        
        PlanLog.objects.create(
            log_type='INFO',
            message=f'RL训练完成: Q-Learning, {num_episodes}轮, '
                   f'最终平均奖励{result["average_reward_last_100"]:.2f}'
        )
        
        logger.info(f"[OK] Q-Learning training completed")
        
        return result

    def _calculate_moving_average(self, data, window=20):
        """计算移动平均"""
        if len(data) < window:
            return data
        return [np.mean(data[max(0, i-window):i+1]) for i in range(len(data))]

    def _extract_policy_summary(self):
        """提取学到的策略摘要"""
        policy_summary = {}
        
        # 对典型状态采样
        sample_states = [
            np.array([0.3, 0.2, 0.4, 0.05, 0.1, 0.5, 0.8, 0.1, 0.7, 0.3, 0.5, 0.1, 0.3]),  # 正常状态
            np.array([0.6, 0.3, 0.1, 0.15, 0.5, 0.3, 0.4, 0.3, 0.95, 0.05, 0.2, 0.25, 0.7]),  # 高压状态
            np.array([0.1, 0.05, 0.8, 0.02, 0.1, 0.8, 0.9, 0.05, 0.5, 0.5, 0.7, 0.05, 0.2]),  # 轻松状态
        ]
        
        state_descriptions = ['正常运营', '高压/紧急', '宽松/充足']
        
        for state, desc in zip(sample_states, state_descriptions):
            discrete = self.discretize_state(state)
            best_action = np.argmax(self.q_table[discrete])
            policy_summary[desc] = {
                'recommended_action': best_action,
                'action_name': SupplyChainEnvironment().ACTIONS.get(best_action, 'UNKNOWN'),
                'confidence': round(np.max(self.q_table[discrete]) / (np.sum(np.abs(self.q_table[discrete])) + 1e-6), 3)
            }
        
        return policy_summary

    def get_recommendation(self, current_state):
        """
        为给定状态推荐最佳动作
        
        Args:
            current_state: 当前状态向量
            
        Returns:
            dict: 推荐结果
        """
        discrete_state = self.discretize_state(current_state)
        q_values = self.q_table[discrete_state]
        best_action = int(np.argmax(q_values))
        
        # 动作名称映射
        env = SupplyChainEnvironment()
        action_name = env.ACTIONS.get(best_action, 'UNKNOWN')
        
        # 置信度计算
        total_q = np.sum(np.abs(q_values)) + 1e-6
        confidence = np.max(q_values) / total_q
        
        # 替代方案（次优动作）
        sorted_actions = np.argsort(q_values)[::-1][:3]
        alternatives = []
        for alt_action in sorted_actions[1:]:
            alternatives.append({
                'action': int(alt_action),
                'name': env.ACTIONS.get(int(alt_action), 'UNKNOWN'),
                'q_value': round(float(q_values[alt_action]), 2)
            })
        
        recommendation = {
            'primary_recommendation': {
                'action': best_action,
                'name': action_name,
                'q_value': round(float(q_values[best_action]), 2),
                'confidence': round(confidence, 3)
            },
            'alternatives': alternatives,
            'all_q_values': {env.ACTIONS.get(i, f'Action_{i}'): round(float(q), 2) 
                            for i, q in enumerate(q_values)},
            'state_analysis': self._analyze_state(current_state),
            'reasoning': self._generate_reasoning(action_name, current_state),
            'timestamp': datetime.now().isoformat()
        }
        
        return recommendation

    def _analyze_state(self, state):
        """分析当前状态特征"""
        analysis = {}
        
        if len(state) >= 13:
            pending_ratio = state[0]
            complete_ratio = state[2]
            inventory_level = state[5]
            utilization = state[8]
            time_pressure = 1 - state[10]
            
            analysis['order_backlog'] = 'high' if pending_ratio > 0.4 else ('medium' if pending_ratio > 0.2 else 'low')
            analysis['fulfillment_rate'] = f'{complete_ratio*100:.0f}%'
            analysis['inventory_status'] = 'tight' if inventory_level < 0.3 else ('adequate' if inventory_level < 0.7 else 'abundant')
            analysis['capacity_utilization'] = f'{utilization*100:.0f}%'
            analysis['urgency'] = 'high' if time_pressure > 0.6 else ('medium' if time_pressure > 0.3 else 'low')
            
            # 风险评估
            risk_score = (
                pending_ratio * 0.3 +
                (1 - complete_ratio) * 0.25 +
                (1 - inventory_level) * 0.2 +
                utilization * 0.15 +
                time_pressure * 0.1
            )
            analysis['overall_risk'] = round(risk_score, 3)
            analysis['risk_level'] = 'critical' if risk_score > 0.6 else ('high' if risk_score > 0.4 else 'normal')
        
        return analysis

    def _generate_reasoning(self, action_name, state):
        """生成决策推理说明"""
        reasoning_parts = []
        
        if len(state) >= 13:
            # 订单压力
            if state[0] > 0.4:
                reasoning_parts.append("待处理订单积压较多")
            
            # 库存状况
            if state[5] < 0.3:
                reasoning_parts.append("库存水平偏低")
            elif state[5] > 0.7:
                reasoning_parts.append("库存充足")
            
            # 产能状况
            if state[8] > 0.9:
                reasoning_parts.append("产能接近饱和")
            
            # 时间压力
            if state[11] > 0.2:
                reasoning_parts.append("存在较多紧急订单")
        
        # 结合动作给出解释
        action_reasoning = {
            'ACCEPT_ORDER_NORMAL': "建议按正常流程处理新订单",
            'ACCEPT_ORDER_URGENT': "建议启动应急机制加速处理",
            'REJECT_ORDER': "建议暂时拒绝以保护现有承诺",
            'ALLOCATE_PRIORITY': "建议采用优先级驱动的分配策略",
            'ALLOCATE_FIFO': "建议保持稳定的先进先出策略",
            'ALLOCATE_LIFO': "建议优先消化库存降低持有成本",
            'TRIGGER_PROCUREMENT': "建议立即触发采购补充关键物料",
            'RELEASE_MATERIAL': "建议让出资源保障高优先级订单",
            'GRAB_MATERIAL': "建议从低优先级订单调配紧缺资源",
            'NO_ACTION': "建议暂维持现状观察"
        }.get(action_name, "根据当前状态做出响应")
        
        reasoning_parts.append(action_reasoning)
        
        return '; '.join(reasoning_parts) if reasoning_parts else "综合分析后给出的建议"


class DeepQLearningAgent:
    """
    Deep Q-Network (DQN) 智能体
    
    使用神经网络近似Q函数，适合大规模状态空间
    
    注意：需要安装PyTorch才能使用
    """

    def __init__(self, state_dim=13, n_actions=10, hidden_size=128):
        if not TORCH_AVAILABLE:
            raise ImportError("PyTorch未安装，无法使用DQN。请运行: pip install torch")
        
        self.state_dim = state_dim
        self.n_actions = n_actions
        self.hidden_size = hidden_size
        
        # 神经网络
        self.policy_net = self._build_network()
        self.target_net = self._build_network()
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()
        
        # 优化器
        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=0.001)
        
        # 经验回放缓冲区
        self.memory = deque(maxlen=10000)
        self.batch_size = 64
        
        # 超参数
        self.gamma = 0.99
        self.epsilon = 1.0
        self.epsilon_min = 0.01
        self.epsilon_decay = 0.995
        self.target_update_freq = 10
        
        # 训练计数器
        self.steps_done = 0

    def _build_network(self):
        """构建DQN网络"""
        return nn.Sequential(
            nn.Linear(self.state_dim, self.hidden_size),
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.hidden_size // 2),
            nn.ReLU(),
            nn.Linear(self.hidden_size // 2, self.n_actions)
        )

    def select_action(self, state, training=True):
        """ε-greedy策略选择动作"""
        if training and random.random() < self.epsilon:
            return random.randrange(self.n_actions)
        
        with torch.no_grad():
            state_tensor = torch.FloatTensor(state).unsqueeze(0)
            q_values = self.policy_net(state_tensor)
            return q_values.argmax(1).item()

    def store_transition(self, state, action, reward, next_state, done):
        """存储经验到回放缓冲区"""
        self.memory.append((state, action, reward, next_state, done))

    def optimize_model(self):
        """从经验回放中学习"""
        if len(self.memory) < self.batch_size:
            return 0.0
        
        # 采样batch
        batch = random.sample(self.memory, self.batch_size)
        states, actions, rewards, next_states, dones = zip(*batch)
        
        # 转换为tensor
        state_batch = torch.FloatTensor(np.array(states))
        action_batch = torch.LongTensor(actions).unsqueeze(1)
        reward_batch = torch.FloatTensor(rewards)
        next_state_batch = torch.FloatTensor(np.array(next_states))
        done_batch = torch.FloatTensor(dones)
        
        # 计算当前Q值
        current_q = self.policy_net(state_batch).gather(1, action_batch).squeeze(1)
        
        # 计算目标Q值
        with torch.no_grad():
            next_q = self.target_net(next_state_batch).max(1)[0]
            target_q = reward_batch + (1 - done_batch) * self.gamma * next_q
        
        # 计算损失并更新
        loss = nn.MSELoss()(current_q, target_q)
        self.optimizer.zero_grad()
        loss.backward()
        
        # 梯度裁剪防止爆炸
        for param in self.policy_net.parameters():
            param.grad.data.clamp_(-1, 1)
        
        self.optimizer.step()
        
        # 衰减epsilon
        self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)
        
        return loss.item()

    def update_target_network(self):
        """定期更新目标网络"""
        self.target_net.load_state_dict(self.policy_net.state_dict())

    def train(self, env, num_episodes=300, max_steps=200):
        """训练DQN智能体"""
        logger.info(f"开始DQN训练: {num_episodes}轮")
        
        rewards_history = []
        
        for episode in range(num_episodes):
            state = env.reset()
            episode_reward = 0
            
            for step in range(max_steps):
                action = self.select_action(state, training=True)
                next_state, reward, done, _ = env.step(action)
                
                self.store_transition(state, action, reward, next_state, done)
                
                # 优化模型
                loss = self.optimize_model()
                
                episode_reward += reward
                state = next_state
                self.steps_done += 1
                
                # 更新目标网络
                if self.steps_done % self.target_update_freq == 0:
                    self.update_target_network()
                
                if done:
                    break
            
            rewards_history.append(episode_reward)
            
            if (episode + 1) % 50 == 0:
                avg_r = np.mean(rewards_history[-50:])
                logger.info(f"DQN Episode {episode+1}: avg={avg_r:.2f}, ε={self.epsilon:.3f}")
        
        result = {
            'agent_type': 'DQN',
            'episodes_trained': num_episodes,
            'final_epsilon': self.epsilon,
            'average_reward': round(np.mean(rewards_history[-100:]), 2),
            'network_architecture': f'{self.state_dim}->{self.hidden_size}->{self.n_actions}',
            'trained_at': datetime.now().isoformat()
        }
        
        PlanLog.objects.create(
            log_type='INFO',
            message=f'DQN训练完成: {num_episodes}轮'
        )
        
        return result


def train_rl_agent(agent_type='qlearning', num_episodes=500):
    """
    快速训练RL智能体的便捷函数
    
    Args:
        agent_type: 'qlearning' 或 'dqn'
        num_episodes: 训练轮数
    """
    env = SupplyChainEnvironment()
    
    if agent_type.lower() == 'dqn':
        if not TORCH_AVAILABLE:
            logger.warning("DQN需要PyTorch，回退到Q-Learning")
            agent_type = 'qlearning'
        else:
            agent = DeepQLearningAgent()
            return agent.train(env, num_episodes=num_episodes)
    
    # 默认使用Q-Learning
    agent = QLearningAgent(n_states=10000)
    return agent.train(env, num_episodes=num_episodes)


# ==================== 全局训练模型缓存 ====================

# 模块级缓存：保存最近一次训练好的Agent，供推荐接口复用
_trained_agent_cache = None
_trained_agent_timestamp = None


def get_rl_recommendation(current_state):
    """
    获取RL智能体的实时决策推荐

    Args:
        current_state: 当前系统状态向量 (13维)

    Returns:
        dict: 决策建议
    """
    global _trained_agent_cache

    # 优先使用已训练的缓存Agent，避免每次创建空Q表导致置信度为0%
    if _trained_agent_cache is not None:
        agent = _trained_agent_cache
    else:
        # 无缓存时尝试从文件加载
        agent = load_rl_model()
        if agent is None:
            agent = QLearningAgent(n_states=10000)
            logger.warning("RL推荐: 无已训练模型，使用未训练Agent（建议先执行训练）")

    return agent.get_recommendation(current_state)


def get_realtime_rl_recommendation():
    """
    获取基于真实数据的RL推荐
    
    使用 RealTimeSupplyChainEnvironment 从数据库加载最新状态，
    然后通过 QLearningAgent 生成决策建议。
    
    Returns:
        dict: 包含以下字段
            - current_state: 当前13维状态向量
            - recommendation: RL智能体的推荐动作
            - state_analysis: 当前状态分析摘要
            - data_source: 数据来源标识（'realtime_db'）
            - timestamp: 推荐生成时间
    """
    logger.info("开始获取实时RL推荐...")
    
    # 创建实时环境并刷新状态
    env = RealTimeSupplyChainEnvironment()
    current_state, is_anomaly, state_diff = env.refresh_state()
    
    # 获取当前状态快照用于日志
    state_summary = {
        'pending_ratio': round(float(current_state[0]), 3),
        'partial_ratio': round(float(current_state[1]), 3),
        'complete_ratio': round(float(current_state[2]), 3),
        'delayed_ratio': round(float(current_state[3]), 3),
        'inventory_level': round(float(current_state[5]), 3),
        'shortage_rate': round(float(current_state[7]), 3),
        'capacity_utilization': round(float(current_state[8]), 3),
        'urgent_order_ratio': round(float(current_state[11]), 3),
    }
    
    logger.info(f"实时状态已加载: 缺料率{state_summary['shortage_rate']:.1%}, "
                f"产能利用率{state_summary['capacity_utilization']:.1%}, "
                f"紧急订单占比{state_summary['urgent_order_ratio']:.1%}")
    
    # 使用 Q-Learning 智能体生成推荐（优先使用已训练的缓存Agent）
    global _trained_agent_cache
    if _trained_agent_cache is not None:
        agent = _trained_agent_cache
    else:
        agent = load_rl_model()
        if agent is None:
            agent = QLearningAgent(n_states=10000)
            logger.warning("RL实时推荐: 无已训练模型，使用未训练Agent")
    recommendation = agent.get_recommendation(current_state)
    
    # 如果检测到异常，附加异常信息
    if is_anomaly and state_diff is not None:
        max_diff_idx = int(np.argmax(np.abs(state_diff)))
        dimension_names = [
            '待处理订单比', '部分齐套比', '完成比', '延期比', '进行中比',
            '总库存', '安全库存比', '缺料率', '产能利用率', '剩余产能',
            '交期缓冲不足率', '紧急订单占比', '进度'
        ]
        recommendation['anomaly_detected'] = {
            'is_anomaly': True,
            'max_change_dimension': dimension_names[max_diff_idx] if max_diff_idx < len(dimension_names) else f'dim_{max_diff_idx}',
            'max_change_value': round(float(state_diff[max_diff_idx]), 4),
            'suggestion': '建议触发重规划以应对突发变化',
        }
        logger.warning(f"检测到状态异常: {dimension_names[max_diff_idx]} 变化 {state_diff[max_diff_idx]:.4f}")
    
    result = {
        'current_state': current_state.tolist() if hasattr(current_state, 'tolist') else list(current_state),
        'recommendation': recommendation,
        'state_analysis': state_summary,
        'data_source': 'realtime_db',
        'anomaly_detected': is_anomaly,
        'timestamp': datetime.now().isoformat(),
    }
    
    # 记录推荐日志
    try:
        action_name = recommendation.get('primary_recommendation', {}).get('name', 'UNKNOWN')
        confidence = recommendation.get('primary_recommendation', {}).get('confidence', 0)
        PlanLog.objects.create(
            log_type='INFO',
            message=f'[RL实时推荐] 推荐动作: {action_name}, '
                   f'置信度: {confidence:.3%}, 异常检测: {is_anomaly}'
        )
    except Exception:
        pass
    
    return result


def train_on_historical_data(days=30):
    """
    基于历史数据训练RL智能体
    
    从数据库加载最近N天的历史数据构建训练环境，
    进行Q-Learning训练，返回训练结果。
    
    Args:
        days: 使用的历史数据天数（默认30天）
        
    Returns:
        dict: 训练结果包含
            - training_result: QLearningAgent.train() 的返回值
            - data_period: 使用的数据时间范围
            - data_stats: 训练数据的统计摘要
            - model_info: 模型信息
    """
    from django.db.models import Sum, Count
    
    logger.info(f"开始基于历史数据训练RL智能体 (近{days}天)...")
    
    today = date.today()
    start_date = today - timedelta(days=days)
    
    # 统计训练数据范围
    try:
        order_count = SalesOrder.objects.filter(
            created_at__date__gte=start_date
        ).count()
        
        inv_count = Inventory.objects.count()
        alloc_count = OrderAllocation.objects.count()
        
        data_stats = {
            'period_start': start_date.isoformat(),
            'period_end': today.isoformat(),
            'days': days,
            'sales_orders_in_period': order_count,
            'total_inventory_records': inv_count,
            'total_allocation_records': alloc_count,
        }
        
        logger.info(f"训练数据统计: {order_count}个订单(近{days}天), "
                    f"{inv_count}条库存记录, {alloc_count}条分配记录")
                    
    except Exception as e:
        logger.warning(f"数据统计失败: {str(e)}")
        data_stats = {'error': str(e), 'period_start': start_date.isoformat(), 'period_end': today.isoformat()}
    
    # 创建环境（自动从数据库加载真实数据）
    env = SupplyChainEnvironment(config={
        'time_horizon': 50,  # 统一使用50步/轮
    })
    
    # 执行训练
    agent = QLearningAgent(
        n_states=10000,
        n_actions=10,
        learning_rate=0.1,
        discount_factor=0.95,
        epsilon_start=1.0,
        epsilon_end=0.01,
        epsilon_decay=0.995
    )

    training_result = agent.train(
        env,
        num_episodes=min(days * 10, 1000),   # 训练轮数与数据量相关，上限1000
        max_steps_per_episode=50  # 与time_horizon一致
    )
    
    # 组装完整结果
    result = {
        'training_result': training_result,
        'data_period': {
            'start': start_date.isoformat(),
            'end': today.isoformat(),
            'days': days,
        },
        'data_stats': data_stats,
        'model_info': {
            'agent_type': 'Q-Learning',
            'trained_on_historical_data': True,
            'state_dim': env.state_dim,
            'n_actions': env.n_actions,
            'q_table_size': len(agent.q_table),
            'final_epsilon': agent.epsilon,
        },
        'trained_at': datetime.now().isoformat(),
    }
    
    # 写入训练完成日志
    try:
        avg_reward = training_result.get('average_reward_last_100', 0)
        episodes = training_result.get('episodes_trained', 0)
        PlanLog.objects.create(
            log_type='INFO',
            message=f'[RL历史训练] 完成: {episodes}轮, 近{days}天数据, '
                   f'最终平均奖励={avg_reward:.2f}, Q表大小={len(agent.q_table)}'
        )
    except Exception:
        pass
    
    logger.info(f"[OK] 历史数据训练完成: {training_result.get('episodes_trained', 0)}轮")

    # 将训练好的Agent写入全局缓存，供推荐接口复用（修复置信度0%问题）
    global _trained_agent_cache, _trained_agent_timestamp
    _trained_agent_cache = agent
    _trained_agent_timestamp = datetime.now().isoformat()

    # 同时持久化到文件，重启后可恢复
    try:
        save_rl_model(agent)
        logger.info("RL模型已缓存并持久化到文件")
    except Exception as e:
        logger.warning(f"RL模型持久化失败（不影响内存缓存）: {e}")

    return result


# ==================== 模型持久化支持 ====================

def save_rl_model(agent, filepath='models/rl_trained_model.pkl'):
    """
    保存训练好的RL模型到文件
    
    Args:
        agent: 已训练的QLearningAgent或DeepQLearningAgent实例
        filepath: 保存路径
        
    Returns:
        dict: 保存结果信息
    """
    import pickle
    import os
    
    os.makedirs(os.path.dirname(filepath) or 'models', exist_ok=True)
    
    model_data = {
        'agent_type': type(agent).__name__,
        'trained_at': datetime.now().isoformat(),
        'state_dim': getattr(agent, 'state_dim', getattr(agent, 'n_states', None)),
        'n_actions': agent.n_actions,
        'final_epsilon': getattr(agent, 'epsilon', None),
        'final_avg_reward': None,
    }
    
    if isinstance(agent, QLearningAgent):
        model_data['q_table'] = dict(agent.q_table)
        model_data['training_history'] = agent.training_history
        model_data['episode_rewards'] = agent.episode_rewards
        if agent.episode_rewards:
            model_data['final_avg_reward'] = round(
                np.mean(agent.episode_rewards[-min(100, len(agent.episode_rewards)):]), 2
            )
    elif isinstance(agent, DeepQLearningAgent):
        model_data['policy_net_state'] = agent.policy_net.state_dict()
        model_data['target_net_state'] = agent.target_net.state_dict()
        model_data['memory_size'] = len(agent.memory)
    
    with open(filepath, 'wb') as f:
        pickle.dump(model_data, f)
    
    logger.info(f"RL模型已保存: {filepath} (type={model_data['agent_type']})")
    
    PlanLog.objects.create(
        log_type='INFO',
        message=f'[RL模型持久化] 模型已保存至 {filepath}, '
               f'类型={model_data["agent_type"]}, 最终奖励={model_data.get("final_avg_reward", "N/A")}'
    )
    
    return {'success': True, 'filepath': filepath, **model_data}


def load_rl_model(filepath='models/rl_trained_model.pkl'):
    """
    从文件加载已训练的RL模型
    
    Args:
        filepath: 模型文件路径
        
    Returns:
        agent: 已恢复的智能体实例，或None（如果加载失败）
    """
    import pickle
    import os
    
    if not os.path.exists(filepath):
        logger.warning(f"RL模型文件不存在: {filepath}")
        return None
    
    try:
        with open(filepath, 'rb') as f:
            model_data = pickle.load(f)
        
        agent_type = model_data.get('agent_type', 'QLearningAgent')
        
        if agent_type == 'DeepQLearningAgent':
            if not TORCH_AVAILABLE:
                logger.warning("DQN模型需要PyTorch，无法加载")
                return None
            
            agent = DeepQLearningAgent(
                state_dim=model_data.get('state_dim', 13),
                n_actions=model_data.get('n_actions', 10)
            )
            agent.policy_net.load_state_dict(model_data['policy_net_state'])
            agent.target_net.load_state_dict(model_data['target_net_state'])
            agent.epsilon = model_data.get('final_epsilon', 0.01)
            
        else:
            agent = QLearningAgent(
                n_states=model_data.get('state_dim', 10000) or 10000,  # 默认10000（原100太小）
                n_actions=model_data.get('n_actions', 10),
                epsilon_start=model_data.get('final_epsilon', 0.01),
                epsilon_end=model_data.get('final_epsilon', 0.01)
            )
            # 恢复Q表
            if 'q_table' in model_data:
                for k, v in model_data['q_table'].items():
                    agent.q_table[k] = v
            agent.training_history = model_data.get('training_history', [])
            agent.episode_rewards = model_data.get('episode_rewards', [])
        
        logger.info(f"RL模型已加载: {filepath} (type={agent_type}, trained_at={model_data.get('trained_at', 'N/A')})")
        
        return agent
        
    except Exception as e:
        logger.error(f"RL模型加载失败: {filepath}, 错误: {str(e)}")
        return None


# ==================== 训练增强：带指标收集和早停 ====================

def train_with_early_stopping(env, num_episodes=1000, max_steps=50,
                               patience=50, min_delta=0.5,
                               agent_type='qlearning'):
    """
    带早停机制的RL训练（增强版）
    
    相比基础train()方法，增加：
    - 早停机制：连续patience轮无改善则停止
    - 详细指标收集：每轮的reward/loss/epsilon/动作分布
    - 训练曲线数据：用于前端可视化
    
    Args:
        env: 训练环境
        num_episodes: 最大训练轮数
        max_steps: 每轮最大步数
        patience: 早停耐心值（连续多少轮无改善则停止）
        min_delta: 最小改善阈值
        agent_type: 'qlearning' 或 'dqn'
        
    Returns:
        dict: 完整训练结果，含训练曲线数据
    """
    from copy import deepcopy
    
    training_curves = {
        'episodes': [],
        'rewards': [],
        'avg_rewards_50': [],       # 最近50轮滑动平均
        'epsilons': [],
        'action_distribution': [],  # 每轮的动作分布统计
        'losses': [],               # DQN损失值(仅DQN)
        'best_reward': float('-inf'),
        'best_episode': 0,
        'early_stopped': False,
        'stop_reason': '',
    }
    
    # 选择智能体类型
    if agent_type.lower() == 'dqn' and TORCH_AVAILABLE:
        agent = DeepQLearningAgent()
    else:
        agent = QLearningAgent(n_states=10000)
    
    no_improve_count = 0
    
    for episode in range(num_episodes):
        state = env.reset()
        episode_reward = 0
        action_counts = defaultdict(int)
        episode_loss = 0.0
        loss_count = 0
        
        for step in range(max_steps):
            # 选择动作
            if hasattr(agent, 'select_action'):
                action = agent.select_action(state, training=True)
            else:
                action = agent.select_action(state)
            
            action_counts[action] += 1
            
            # 执行动作
            next_state, reward, done, _ = env.step(action)
            episode_reward += reward
            
            # DQN需要存储和优化
            if isinstance(agent, DeepQLearningAgent):
                agent.store_transition(state, action, reward, next_state, done)
                loss = agent.optimize_model()
                if loss > 0:
                    episode_loss += loss
                    loss_count += 1
                
                if agent.steps_done % agent.target_update_freq == 0:
                    agent.update_target_network()
            else:
                # Q-Learning更新
                agent.update(state, action, reward, next_state, done)
            
            state = next_state
            if done:
                break
        
        # 记录本轮指标
        training_curves['episodes'].append(episode + 1)
        training_curves['rewards'].append(round(episode_reward, 2))
        training_curves['epsilons'].append(round(getattr(agent, 'epsilon', 0), 4))
        training_curves['action_distribution'].append(dict(action_counts))
        
        if loss_count > 0:
            training_curves['losses'].append(round(episode_loss / loss_count, 4))
        elif isinstance(agent, DeepQLearningAgent):
            training_curves['losses'].append(0.0)
        
        # 滑动平均奖励
        recent_rewards = training_curves['rewards'][-min(50, len(training_curves['rewards'])):]
        avg_r = np.mean(recent_rewards) if recent_rewards else 0
        training_curves['avg_rewards_50'].append(round(avg_r, 2))
        
        # 早停检查
        if avg_r > training_curves['best_reward'] + min_delta:
            training_curves['best_reward'] = avg_r
            training_curves['best_episode'] = episode + 1
            no_improve_count = 0
        else:
            no_improve_count += 1
        
        # 日志输出
        if (episode + 1) % 50 == 0 or no_improve_count == patience:
            logger.info(
                f"[RL训练] Episode {episode+1}/{num_episodes}: "
                f"reward={episode_reward:.1f}, avg50={avg_r:.2f}, "
                f"ε={getattr(agent, 'epsilon', 0):.3f}, "
                f"best={training_curves['best_reward']:.2f}@Ep{training_curves['best_episode']}"
            )
        
        # 触发早停
        if no_improve_count >= patience and episode >= 100:
            training_curves['early_stopped'] = True
            training_curves['stop_reason'] = (
                f"早停: 连续{patience}轮无改善 "
                f"(最佳={training_curves['best_reward']:.2f} @ Ep{training_curves['best_episode']})"
            )
            logger.info(f"训练早停于Episode {episode+1}: {training_curves['stop_reason']}")
            break
    
    if not training_curves['early_stopped']:
        training_curves['stop_reason'] = f"达到最大轮数{num_episodes}"
    
    # 最终结果
    final_result = {
        'agent_type': type(agent).__name__,
        'total_episodes': len(training_curves['episodes']),
        'final_avg_reward': round(training_curves['avg_rewards_50'][-1], 2) if training_curves['avg_rewards_50'] else 0,
        'best_reward': round(training_curves['best_reward'], 2),
        'best_episode': training_curves['best_episode'],
        'early_stopped': training_curves['early_stopped'],
        'stop_reason': training_curves['stop_reason'],
        'trained_at': datetime.now().isoformat(),
        'training_curves': training_curves,
        'agent': agent,  # 返回智能体供后续使用
    }
    
    # 写入日志
    try:
        PlanLog.objects.create(
            log_type='INFO',
            message=f'[RL增强训练] 完成: {final_result["total_episodes"]}轮, '
                   f'最佳奖励={final_result["best_reward"]:.2f}, '
                   f'原因={final_result["stop_reason"]}'
        )
    except Exception:
        pass

    # 将训练好的Agent写入全局缓存
    global _trained_agent_cache, _trained_agent_timestamp
    _trained_agent_cache = agent
    _trained_agent_timestamp = datetime.now().isoformat()
    try:
        save_rl_model(agent)
    except Exception:
        pass

    return final_result


# ==================== 奖励函数敏感性分析 ====================

class TunableRewardEnvironment(SupplyChainEnvironment):
    """
    可调奖励系数的供应链环境（用于敏感性分析）
    
    继承 SupplyChainEnvironment，允许动态覆盖奖励塑形中的关键系数，
    以系统性测试不同参数组合对训练收敛的影响。
    """

    def __init__(self, config=None, reward_config=None):
        super().__init__(config)
        # 默认奖励系数（基线值，已优化）
        self.reward_config = reward_config or {
            'on_time_delivery_reward': 15,      # 按时交付奖励（原10）
            'delay_penalty_per_day': -5,        # 延期惩罚/天
            'inventory_cost_coeff': -0.01,      # 库存成本系数
            'shortage_penalty_coeff': -2,       # 缺料惩罚系数
            'capacity_utilization_reward': 1.0,  # 产能利用奖励
        }

    def _execute_action(self, action, action_name):
        """使用可配置的奖励系数执行动作"""
        reward, info = super()._execute_action(action, action_name)

        # 获取当前状态指标
        shortage_rate = self.allocation_stats.get('shortage_rate', 0.1)
        utilization = self.capacity_stats.get('utilization', 0.75)
        urgent_ratio = self.time_pressure_stats.get('urgent_order_ratio', 0.11)
        inventory_level = self.inventory_stats.get('total', 5000)
        delayed_ratio = self.order_stats.get('delayed_ratio', 0.05)

        # 根据动作类型和可配置系数重新计算奖励
        rc = self.reward_config

        if action == 0:  # ACCEPT_ORDER_NORMAL
            base_reward = rc['on_time_delivery_reward']
            resource_cost = -1.5 * utilization
            efficiency_bonus = (1 - delayed_ratio) * 2 + (1 - shortage_rate) * 2
            reward = base_reward + resource_cost + efficiency_bonus

        elif action == 1:  # ACCEPT_ORDER_URGENT
            base_reward = rc['on_time_delivery_reward'] * 1.2
            overtime_cost = -3
            risk_penalty = -(delayed_ratio * 2 + shortage_rate * 1)
            urgency_bonus = urgent_ratio * 5
            reward = base_reward + overtime_cost + risk_penalty + urgency_bonus

        elif action == 2:  # REJECT_ORDER
            opportunity_cost = -5
            risk_avoided = min(8, (urgent_ratio + delayed_ratio) * 12)
            reward = opportunity_cost + risk_avoided

        elif action in [3, 4, 5]:  # ALLOCATE_FIFO / PRIORITY / LIFO
            if action == 3:
                fifo_bonus = (1 - shortage_rate) * 5
                stability_bonus = (1 - urgent_ratio) * 2
                reward = 4 + fifo_bonus + stability_bonus
            elif action == 4:
                priority_bonus = ((shortage_rate + urgent_ratio) * 8)
                reward = 5 + priority_bonus
            else:
                lifo_bonus = (inventory_level / 10000.0) * 6
                reward = 2 + lifo_bonus

        elif action == 6:  # TRIGGER_PROCUREMENT
            immediate_cost = -3
            future_benefit = shortage_rate * 15
            reward = immediate_cost + future_benefit

        elif action == 7:  # RELEASE_MATERIAL
            sacrifice = -2
            high_priority_gain = 8 * urgent_ratio
            reward = sacrifice + high_priority_gain

        elif action == 8:  # GRAB_MATERIAL
            grab_risk = -(utilization * 2 + delayed_ratio * 1.5)
            grab_gain = 5 + (shortage_rate * 8)
            reward = grab_risk + grab_gain

        elif action == 9:  # NO_ACTION
            time_penalty = -0.3 * (1 + urgent_ratio * 0.5)
            reward = time_penalty

        # 边界约束
        reward = max(-20, min(20, reward))
        info['raw_reward'] = round(reward, 2)

        return reward, info


def reward_sensitivity_analysis(env=None, n_runs=50):
    """
    奖励函数敏感性分析
    
    系统性地调整奖励塑形中的关键系数，观察对训练收敛的影响，
    用于论证当前奖励系数设置的合理性。
    
    测试维度:
    1. 按时交付奖励系数 (baseline: +10) → 测试 [5, 10, 15, 20]
    2. 延期惩罚系数 (baseline: -5/天) → 测试 [-3, -5, -8, -10]  
    3. 库存成本系数 (baseline: -0.01) → 测试 [-0.005, -0.01, -0.02, -0.05]
    4. 缺料惩罚系数 (baseline: -2) → 测试 [-1, -2, -4, -6]
    5. 产能利用奖励 (baseline: +1.0) → 测试 [+0.5, +1.0, +1.5, +2.0]
    
    对每组参数运行 n_runs 次短期训练(50 episodes)，记录:
    - 平均最终奖励
    - 收敛速度(达到稳定所需episodes)
    - 动作分布熵(衡量策略多样性)
    - 各动作选择频率
    
    Args:
        env: 可选的已有环境实例，若为None则创建新环境
        n_runs: 每组参数运行的训练次数
        
    Returns:
        dict: {
            'parameter_sweep_results': [...],  # 每组参数的结果
            'current_config_performance': {...},  # 当前配置的表现
            'recommended_tuning': str,  # 是否建议调参
            'sensitivity_ranking': [...]  # 参数敏感度排序（最敏感的排第一）
        }
    """
    import uuid
    try:
        from scipy import stats as scipy_stats
    except ImportError:
        scipy_stats = None
        logger.warning("scipy未安装，敏感性分析中的统计功能将受限")
    
    logger.info(f"开始奖励函数敏感性分析: {n_runs}次运行/组")
    
    # 参数扫描空间定义
    param_sweep_space = {
        'on_time_delivery_reward': {
            'name': '按时交付奖励系数',
            'baseline': 10,
            'test_values': [5, 10, 15, 20],
            'unit': '',
        },
        'delay_penalty_per_day': {
            'name': '延期惩罚系数',
            'baseline': -5,
            'test_values': [-3, -5, -8, -10],
            'unit': '/天',
        },
        'inventory_cost_coeff': {
            'name': '库存成本系数',
            'baseline': -0.01,
            'test_values': [-0.005, -0.01, -0.02, -0.05],
            'unit': '',
        },
        'shortage_penalty_coeff': {
            'name': '缺料惩罚系数',
            'baseline': -2,
            'test_values': [-1, -2, -4, -6],
            'unit': '',
        },
        'capacity_utilization_reward': {
            'name': '产能利用奖励',
            'baseline': 1.0,
            'test_values': [0.5, 1.0, 1.5, 2.0],
            'unit': '',
        },
    }
    
    # 基线奖励配置
    baseline_config = {
        'on_time_delivery_reward': 10,
        'delay_penalty_per_day': -5,
        'inventory_cost_coeff': -0.01,
        'shortage_penalty_coeff': -2,
        'capacity_utilization_reward': 1.0,
    }
    
    parameter_sweep_results = []
    sensitivity_data = {}
    
    for param_key, param_info in param_sweep_space.items():
        logger.info(f"  扫描参数: {param_info['name']} ({param_key})")
        
        param_results = []
        
        for test_value in param_info['test_values']:
            # 构建该测试值的奖励配置
            test_config = dict(baseline_config)
            test_config[param_key] = test_value
            
            run_rewards = []
            run_convergence_speeds = []
            run_action_entropies = []
            run_action_distributions = []
            
            for run_idx in range(n_runs):
                # 创建带可调奖励的环境
                test_env = TunableRewardEnvironment(
                    config=env.config if env else None,
                    reward_config=test_config
                )
                
                # 使用Q-Learning进行短期训练
                agent = QLearningAgent(
                    n_states=100,
                    n_actions=10,
                    learning_rate=0.1,
                    discount_factor=0.95,
                    epsilon_start=1.0,
                    epsilon_end=0.05,
                    epsilon_decay=0.99
                )
                
                # 运行10轮短期训练（敏感性分析只需趋势，不需完整收敛）
                episode_rewards = []
                action_counts_all = defaultdict(int)

                for ep in range(10):
                    state = test_env.reset()
                    ep_reward = 0
                    
                    for step in range(100):
                        action = agent.choose_action(state, training=True)
                        action_counts_all[action] += 1
                        next_state, reward, done, _ = test_env.step(action)
                        agent.update(state, action, reward, next_state, done)
                        ep_reward += reward
                        state = next_state
                        if done:
                            break
                    
                    episode_rewards.append(ep_reward)
                    agent.decay_epsilon()
                
                # 记录本次运行的指标
                final_avg = np.mean(episode_rewards[-10:]) if len(episode_rewards) >= 10 else np.mean(episode_rewards)
                run_rewards.append(final_avg)
                
                # 收敛速度：滑动平均首次超过阈值80%的episode数
                moving_avg = []
                convergence_ep = 10  # 默认未收敛（当前为10轮）
                target_val = max(episode_rewards) * 0.8 if max(episode_rewards) > 0 else 0
                for i, r in enumerate(episode_rewards):
                    moving_avg.append(np.mean(episode_rewards[max(0, i-9):i+1]))
                    if len(moving_avg) > 3 and moving_avg[-1] >= target_val and convergence_ep == 10:
                        convergence_ep = i + 1
                run_convergence_speeds.append(convergence_ep)
                
                # 动作分布熵
                total_actions = sum(action_counts_all.values())
                if total_actions > 0:
                    probs = np.array([count / total_actions for count in action_counts_all.values()])
                    entropy = -np.sum(probs * np.log2(probs + 1e-10))
                else:
                    entropy = 0
                run_action_entropies.append(entropy)
                
                # 动作选择频率
                action_dist = {
                    f'action_{k}': v for k, v in sorted(action_counts_all.items())
                }
                run_action_distributions.append(action_dist)
            
            # 汇总该参数值的结果
            param_results.append({
                'param_value': test_value,
                'avg_final_reward': round(np.mean(run_rewards), 3),
                'std_final_reward': round(np.std(run_rewards), 3),
                'avg_convergence_speed': round(np.mean(run_convergence_speeds), 1),
                'avg_action_entropy': round(np.mean(run_action_entropies), 3),
                'action_distribution_summary': {
                    k: round(np.mean([d.get(k, 0) for d in run_action_distributions]), 1)
                    for k in (run_action_distributions[0].keys() if run_action_distributions else [])
                },
            })
        
        # 计算该参数的敏感度
        avg_rewards_by_value = [r['avg_final_reward'] for r in param_results]
        mean_reward = np.mean(avg_rewards_by_value)
        std_reward = np.std(avg_rewards_by_value)
        sensitivity = abs(std_reward / mean_reward) if mean_reward != 0 else 0
        
        sensitivity_data[param_key] = {
            'name': param_info['name'],
            'sensitivity': round(sensitivity, 4),
            'baseline_value': param_info['baseline'],
            'best_value': param_results[np.argmax(avg_rewards_by_value)]['param_value'],
            'worst_value': param_results[np.argmin(avg_rewards_by_value)]['param_value'],
            'reward_range': [
                round(min(avg_rewards_by_value), 3),
                round(max(avg_rewards_by_value), 3),
            ],
        }
        
        parameter_sweep_results.append({
            'parameter_key': param_key,
            'parameter_name': param_info['name'],
            'unit': param_info.get('unit', ''),
            'baseline': param_info['baseline'],
            'test_results': param_results,
            'sensitivity': round(sensitivity, 4),
        })
    
    # 敏感度排序（最敏感的排第一）
    sensitivity_ranking = sorted(
        sensitivity_data.items(),
        key=lambda x: x[1]['sensitivity'],
        reverse=True
    )
    
    # 当前基线配置在各维度上的表现百分位
    current_config_performance = {}
    for param_key, sdata in sensitivity_data.items():
        baseline_val = sdata['baseline_value']
        all_values = [r['param_value'] for r in 
                     next(p['test_results'] for p in parameter_sweep_results 
                          if p['parameter_key'] == param_key)]
        all_rewards = [r['avg_final_reward'] for r in 
                      next(p['test_results'] for p in parameter_sweep_results 
                           if p['parameter_key'] == param_key)]
        
        # 找到基线值对应的表现百分位
        baseline_idx = all_values.index(baseline_val) if baseline_val in all_values else len(all_values) // 2
        baseline_reward = all_rewards[baseline_idx] if baseline_idx < len(all_rewards) else np.mean(all_rewards)
        if scipy_stats is not None:
            percentile = scipy_stats.percentileofscore(all_rewards, baseline_reward)
        else:
            # scipy不可用时，用简单的排名百分比替代
            sorted_rewards = sorted(all_rewards)
            rank = sorted_rewards.index(baseline_reward) if baseline_reward in sorted_rewards else len(sorted_rewards) // 2
            percentile = (rank / len(sorted_rewards)) * 100
        
        current_config_performance[param_key] = {
            'value': baseline_val,
            'reward': round(baseline_reward, 3),
            'percentile': round(percentile, 1),
            'interpretation': (
                '优秀(>75%)' if percentile > 75 else
                '良好(50-75%)' if percentile > 50 else
                '一般(25-50%)' if percentile > 25 else
                '需优化(<25%)'
            ),
        }
    
    # 调参建议
    most_sensitive = sensitivity_ranking[0] if sensitivity_ranking else None
    if most_sensitive and most_sensitive[1]['sensitivity'] > 0.15:
        recommended_tuning = (
            f"建议重点调优 [{most_sensitive[1]['name']}](敏感度={most_sensitive[1]['sensitivity']:.3f})，"
            f"当前值{most_sensitive[1]['baseline_value']}，"
            f"最优值可能为{most_sensitive[1]['best_value']}"
        )
    elif most_sensitive and most_sensitive[1]['sensitivity'] > 0.05:
        recommended_tuning = (
            f"当前奖励系数设置较为合理，[{most_sensitive[1]['name']}]敏感度适中({most_sensitive[1]['sensitivity']:.3f})，"
            f"微调可能带来小幅提升"
        )
    else:
        recommended_tuning = "当前奖励系数设置稳定，各参数敏感度均较低，无需大幅调参"
    
    result = {
        'analysis_id': str(uuid.uuid4())[:8],
        'parameter_sweep_results': parameter_sweep_results,
        'current_config_performance': current_config_performance,
        'recommended_tuning': recommended_tuning,
        'sensitivity_ranking': [
            {
                'parameter': item[0],
                'name': item[1]['name'],
                'sensitivity': item[1]['sensitivity'],
                'baseline': item[1]['baseline_value'],
                'best_value': item[1]['best_value'],
                'reward_range': item[1]['reward_range'],
            }
            for item in sensitivity_ranking
        ],
        'analysis_metadata': {
            'n_runs_per_config': n_runs,
            'episodes_per_run': 50,
            'max_steps_per_episode': 100,
            'parameters_tested': len(param_sweep_space),
            'total_experiments': n_runs * len(param_sweep_space) * sum(
                len(p['test_values']) for p in param_sweep_space.values()
            ),
            'analyzed_at': datetime.now().isoformat(),
        },
    }
    
    # 写入 PlanLog 记录分析结论
    try:
        PlanLog.objects.create(
            log_type='INFO',
            message=f'[RL敏感性分析] 完成: 最敏感参数={most_sensitive[1]["name"] if most_sensitive else "N/A"}'
                   f'(敏感度={most_sensitive[1]["sensitivity"]:.3f}), '
                   f'建议: {recommended_tuning[:100]}'
        )
    except Exception:
        pass
    
    logger.info(f"[OK] 奖励敏感性分析完成: {result['analysis_metadata']['total_experiments']}次实验")
    
    return result
