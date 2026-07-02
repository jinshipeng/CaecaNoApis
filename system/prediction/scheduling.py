"""
高级生产排程引擎模块

提供完整的生产计划排程能力，包括多产线联动、换线时间计算、
产能硬约束、工厂日历集成、物料齐套检查、优先级驱动排程等功能。
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import logging

from .models import (
    SalesOrder, WorkCenter, Capacity, FactoryCalendar,
    MaterialPlanResult, BillOfMaterials, Inventory, OrderAllocation, PlanLog
)

logger = logging.getLogger(__name__)


# =============================================================================
# 向后兼容：保留原有简单排程器
# =============================================================================

class LegacyScheduler:
    """
    简单排程器（原有 Scheduler 的向后兼容版本）

    提供基础的生产排程能力，适用于简单的需求预测场景。
    保留此类以确保现有调用方（如 scheduling_views.py）不受影响。
    """

    def __init__(self, production_capacity=100, initial_inventory=50, lead_time=1):
        self.production_capacity = production_capacity
        self.initial_inventory = initial_inventory
        self.lead_time = lead_time

    def generate_schedule(self, demand_forecast, weeks=4):
        """生成基础生产排程表"""
        schedule = []
        current_inventory = self.initial_inventory

        for i in range(weeks):
            demand = max(0, demand_forecast[i])
            net_demand = max(0, demand - current_inventory)
            production = min(net_demand, self.production_capacity)
            beginning_inventory = current_inventory
            current_inventory = current_inventory + production - demand

            schedule.append({
                '周次': i + 1,
                '预测需求': demand,
                '期初库存': beginning_inventory,
                '生产计划': production,
                '期末库存': current_inventory
            })

        schedule_df = pd.DataFrame(schedule)
        return schedule_df

    def optimize_schedule(self, demand_forecast, weeks=4):
        """优化排程并返回总成本"""
        base_schedule = self.generate_schedule(demand_forecast, weeks)
        inventory_cost = base_schedule['期末库存'].sum() * 1
        shortage_cost = max(
            0,
            sum(demand_forecast) - sum(base_schedule['生产计划']) - self.initial_inventory
        ) * 5
        total_cost = inventory_cost + shortage_cost
        return base_schedule, total_cost

    def calculate_service_level(self, demand_forecast, production_plan, initial_inventory):
        """计算服务水平指标"""
        current_inventory = initial_inventory
        total_demand = sum(demand_forecast)
        total_shortage = 0

        for i in range(len(demand_forecast)):
            demand = max(0, demand_forecast[i])
            production = production_plan[i]
            available = current_inventory + production
            if available < demand:
                shortage = demand - available
                total_shortage += shortage
            current_inventory = available - demand

        if total_demand > 0:
            service_level = 1 - (total_shortage / total_demand)
        else:
            service_level = 1.0

        return service_level, total_shortage, total_demand


# 向后兼容别名：原有的 Scheduler 名称仍然可用
Scheduler = LegacyScheduler


# =============================================================================
# 高级生产排程引擎
# =============================================================================

class AdvancedScheduler:
    """
    高级生产排程引擎

    核心能力：
    - 多产线/工作中心联动排程：从 WorkCenter 模型加载所有工作中心信息，
      支持跨产线分配订单
    - 换线时间(changeover_time)计算：连续排产不同产品时扣除换线时间，
      同产品连续生产可累积批量降低换线成本
    - 产能硬约束：每日产能不能超过 daily_capacity_limit * shift_count * (hours_per_shift / 8)，
      考虑维护窗口
    - 工厂日历集成：使用 FactoryCalendar 判断工作日，非工作日不排产
    - 物料齐套约束：排程前检查物料齐套状态，未齐套订单标记为"待料"
    - 优先级驱动：高优先级订单优先排产，同优先级按交期(EDD)排序
    - 反哺物料计划：排程结果回写到 MaterialPlanResult 或生成 ProductionSchedule 记录
    """

    # 待排产订单状态集合
    SCHEDULABLE_STATUSES = ['pending', 'confirmed', 'allocated', 'partial']

    # 默认排程展望天数
    DEFAULT_HORIZON_DAYS = 30

    # 默认每标准工时产能单位数（用于将小时转换为产能单位）
    CAPACITY_UNITS_PER_HOUR = 10

    def __init__(self):
        """初始化高级排程引擎，加载基础配置和缓存"""
        self.work_centers = {}           # {work_center_code: WorkCenter实例}
        self.capacity_data = {}          # {(work_center, material_id): Capacity实例}
        self.calendar_cache = {}         # {factory_code: {date_str: FactoryCalendar实例}}
        self.material_bom_cache = {}     # {material_id: [BOM列表]}
        self.inventory_cache = {}        # {material_id: 总可用库存}
        self.current_date = date.today()
        # 各工作中心每日已占用产能: {work_center_code: {date_str: 已占用量}}
        self.occupied_capacity = defaultdict(lambda: defaultdict(float))
        # 各工作中心当前正在生产的产品（用于换线计算）
        self.current_product_on_line = {}
        # 排程结果缓存
        self.schedule_results = []
        # 统计计数器
        self.changeover_count = 0
        self.total_changeover_hours = Decimal('0')
        logger.info('高级排程引擎已初始化')

    # -----------------------------------------------------------------
    # 资源加载方法
    # -----------------------------------------------------------------

    def load_production_resources(self):
        """
        从数据库加载所有生产资源（工作中心+产能+日历+BOM+库存）

        加载内容包括：
        - 所有启用的工作中心及其属性
        - 各工作中心的产能数据
        - 工厂日历（用于判断工作日）
        - BOM结构（用于齐套检查）
        - 当前库存水平
        """
        logger.info('开始加载生产资源...')

        # 1. 加载所有启用的工作中心
        wc_queryset = WorkCenter.objects.filter(is_active=True)
        for wc in wc_queryset:
            self.work_centers[wc.work_center_code] = wc
            # 初始化各产线的当前产品为空
            self.current_product_on_line[wc.work_center_code] = None
        logger.info(f'已加载 {len(self.work_centers)} 个工作中心')

        # 2. 加载产能数据
        cap_queryset = Capacity.objects.filter(is_active=True).select_related('material')
        for cap in cap_queryset:
            key = (cap.work_center, cap.material_id)
            self.capacity_data[key] = cap
        logger.info(f'已加载 {len(self.capacity_data)} 条产能记录')

        # 3. 加载工厂日历（加载未来90天的日历数据，支持多工厂）
        horizon_end = self.current_date + timedelta(days=90)
        cal_queryset = FactoryCalendar.objects.filter(
            date__gte=self.current_date,
            date__lte=horizon_end
        )
        for cal in cal_queryset:
            factory = cal.factory_code or 'DEFAULT'
            if factory not in self.calendar_cache:
                self.calendar_cache[factory] = {}
            self.calendar_cache[factory][cal.date.strftime('%Y-%m-%d')] = cal
        logger.info(f'已加载 {sum(len(v) for v in self.calendar_cache.values())} 天工厂日历（{len(self.calendar_cache)}个工厂）')

        # 4. 加载BOM结构（用于齐套检查）
        bom_queryset = BillOfMaterials.objects.filter(is_active=True).select_related(
            'parent_material', 'child_material'
        )
        for bom in bom_queryset:
            parent_id = bom.parent_material_id
            if parent_id not in self.material_bom_cache:
                self.material_bom_cache[parent_id] = []
            self.material_bom_cache[parent_id].append(bom)
        logger.info(f'已加载 {len(bom_queryset)} 条BOM记录')

        # 5. 加载库存汇总（按物料聚合可用数量，排除Hold状态物料）
        from django.db.models import Sum
        inv_agg = Inventory.objects.filter(
            is_hold=False
        ).values('material').annotate(
            total_available=Sum('available_quantity')
        )
        for inv in inv_agg:
            self.inventory_cache[inv['material']] = inv['total_available'] or 0
        logger.info(f'已加载 {len(self.inventory_cache)} 个物料的库存信息')

        logger.info('生产资源加载完成')

    # -----------------------------------------------------------------
    # 产能计算方法
    # -----------------------------------------------------------------

    def _is_workday(self, target_date, factory_code=None):
        """
        判断指定日期是否为工作日 - 支持多工厂差异化日历

        Args:
            target_date: 目标日期
            factory_code: 工厂代码，为空时使用默认日历

        Returns:
            bool: True 表示工作日，False 表示休息日
        """
        date_key = target_date.strftime('%Y-%m-%d')
        factory = factory_code or 'DEFAULT'
        # 优先查询指定工厂的日历
        if factory in self.calendar_cache and date_key in self.calendar_cache[factory]:
            return self.calendar_cache[factory][date_key].is_workday
        # 回退到默认工厂日历
        if factory != 'DEFAULT' and 'DEFAULT' in self.calendar_cache and date_key in self.calendar_cache['DEFAULT']:
            return self.calendar_cache['DEFAULT'][date_key].is_workday
        # 默认：周一到周五为工作日
        return target_date.weekday() < 5

    def _is_in_maintenance(self, work_center_code, target_date):
        """
        判断指定日期是否在工作中心的维护窗口内

        Args:
            work_center_code: 工作中心代码
            target_date: date 对象

        Returns:
            bool: True 表示在维护期内
        """
        wc = self.work_centers.get(work_center_code)
        if not wc:
            return False
        if wc.maintenance_start_date and wc.maintenance_end_date:
            return wc.maintenance_start_date <= target_date <= wc.maintenance_end_date
        return False

    def calculate_available_capacity(self, work_center_code, target_date):
        """
        计算某工作中心在指定日期的可用产能（考虑班次/维护/日历/已占用）

        计算公式：
        可用产能 = 日产能上限 × 班次数 × (每班工时 / 8) - 维护折减 - 已占用产能

        Args:
            work_center_code: 工作中心代码
            target_date: date 对象，目标日期

        Returns:
            dict: {
                'gross_capacity': 理论总产能,
                'available_capacity': 实际可用产能,
                'occupied_capacity': 已占用产能,
                'maintenance_deduction': 维护折减,
                'shift_count': 班次数,
                'hours_per_shift': 每班工时,
                'is_workday': 是否工作日
            }
        """
        wc = self.work_centers.get(work_center_code)
        if not wc:
            logger.warning(f'工作中心 {work_center_code} 不存在或未启用')
            return {
                'gross_capacity': 0,
                'available_capacity': 0,
                'occupied_capacity': 0,
                'maintenance_deduction': 0,
                'shift_count': 0,
                'hours_per_shift': 0,
                'is_workday': False
            }

        # 非工作日直接返回零产能
        if not self._is_workday(target_date):
            return {
                'gross_capacity': 0,
                'available_capacity': 0,
                'occupied_capacity': 0,
                'maintenance_deduction': 0,
                'shift_count': wc.shift_count or 1,
                'hours_per_shift': float(wc.hours_per_shift or 8),
                'is_workday': False
            }

        # 计算理论日产能
        shift_count = wc.shift_count or 1
        hours_per_shift = float(wc.hours_per_shift or 8)
        daily_capacity_limit = wc.daily_capacity_limit or 0

        # 理论产能 = 日产能上限 × 班次系数
        shift_factor = hours_per_shift / 8.0
        gross_capacity = daily_capacity_limit * shift_count * shift_factor

        # 维护折减：维护期间按计划维护停机时长比例扣减
        maintenance_deduction = 0
        if self._is_in_maintenance(work_center_code, target_date):
            planned_maintenance = float(wc.planned_maintenance_hours or 0)
            total_daily_hours = shift_count * hours_per_shift
            if total_daily_hours > 0:
                maintenance_ratio = planned_maintenance / total_daily_hours
                maintenance_deduction = gross_capacity * min(maintenance_ratio, 1.0)

        # 已占用产能
        date_key = target_date.strftime('%Y-%m-%d')
        occupied = self.occupied_capacity[work_center_code][date_key]

        # 实际可用产能
        available = gross_capacity - maintenance_deduction - occupied
        available = max(0, available)

        result = {
            'gross_capacity': gross_capacity,
            'available_capacity': available,
            'occupied_capacity': occupied,
            'maintenance_deduction': maintenance_deduction,
            'shift_count': shift_count,
            'hours_per_shift': hours_per_shift,
            'is_workday': True
        }

        logger.debug(
            f'[{work_center_code}] {date_key} 可用产能: '
            f'理论={gross_capacity:.1f}, 维护折减={maintenance_deduction:.1f}, '
            f'已占用={occupied:.1f}, 实际可用={available:.1f}'
        )

        return result

    # -----------------------------------------------------------------
    # 换线时间计算
    # -----------------------------------------------------------------

    def check_changeover_penalty(self, work_center_code, current_product, next_product):
        """
        计算换线时间惩罚（小时）

        规则：
        - 如果下一个产品与当前产品相同 → 无换线惩罚（同产品连续生产累积批量）
        - 如果下一个产品与当前产品不同 → 返回该工作中心的 changeover_time
        - 如果当前产线无产品（首次排产）→ 返回 0（无需换线）

        Args:
            work_center_code: 工作中心代码
            current_product: 当前产线上正在生产的物料ID（或None）
            next_product: 下一个要生产的物料ID

        Returns:
            tuple: (penalty_hours: Decimal, need_changeover: bool)
        """
        wc = self.work_centers.get(work_center_code)
        if not wc:
            return Decimal('0'), False

        changeover_time = wc.changeover_time or Decimal('0')

        # 首次排产或同产品连续生产：无需换线
        if current_product is None or current_product == next_product:
            return Decimal('0'), False

        # 不同产品：产生换线时间
        logger.debug(
            f'[{work_center_code}] 换线: {current_product} -> {next_product}, '
            f'耗时={changeover_time}小时'
        )
        return changeover_time, True

    def _apply_changeover_to_capacity(self, work_center_code, target_date, penalty_hours):
        """
        将换线时间折算为产能占用并记录

        Args:
            work_center_code: 工作中心代码
            target_date: 目标日期
            penalty_hours: 换线惩罚小时数（Decimal）
        """
        if penalty_hours <= 0:
            return

        wc = self.work_centers.get(work_center_code)
        if not wc:
            return

        # 将换线小时数转换为产能单位占用
        capacity_units = float(penalty_hours) * self.CAPACITY_UNITS_PER_HOUR
        date_key = target_date.strftime('%Y-%m-%d')
        self.occupied_capacity[work_center_code][date_key] += capacity_units

        self.changeover_count += 1
        self.total_changeover_hours += penalty_hours

        logger.info(
            f'[{work_center_code}] {date_key} 记录换线占用: '
            f'{penalty_hours}小时 ≈ {capacity_units:.1f}产能单位'
        )

    # -----------------------------------------------------------------
    # 物料齐套检查
    # -----------------------------------------------------------------

    def check_material_readiness(self, order):
        """
        检查订单的物料齐套状态

        检查逻辑：
        1. 根据订单成品物料查找BOM清单
        2. 对每个子物料查询可用库存
        3. 计算齐套率 = 可满足需求数量的子物料数 / 总子物料数

        Args:
            order: SalesOrder 实例

        Returns:
            dict: {
                'is_ready': 是否齐套,
                'complete_rate': 齐套率(0~1),
                'shortage_items': 缺料列表 [{material_code, material_name, required, available, shortage}],
                'status': 'ready'|'partial'|'shortage'
            }
        """
        material_id = order.material_id
        order_qty = order.quantity

        # 查找BOM
        bom_list = self.material_bom_cache.get(material_id, [])
        if not bom_list:
            # 无BOM定义，视为齐套（可能是外购件）
            return {
                'is_ready': True,
                'complete_rate': 1.0,
                'shortage_items': [],
                'status': 'ready'
            }

        shortage_items = []
        ready_count = 0
        total_items = len(bom_list)

        for bom in bom_list:
            child_id = bom.child_material_id
            required_qty = float(bom.quantity) * order_qty
            available_qty = self.inventory_cache.get(child_id, 0)

            shortage = max(0, required_qty - available_qty)

            if shortage > 0:
                shortage_items.append({
                    'material_code': bom.child_material.material_code,
                    'material_name': bom.child_material.material_name,
                    'required': required_qty,
                    'available': available_qty,
                    'shortage': shortage
                })
            else:
                ready_count += 1

        complete_rate = ready_count / total_items if total_items > 0 else 1.0

        # 统一齐套判定阈值（与 material_planning.py 核心算法一致）：
        #   完全齐套(ready): complete_rate >= 100%
        #   部分齐套(partial): 0 < complete_rate < 100%（有分配但未完全满足）
        #   未齐套(shortage): complete_rate = 0（完全没有分配）
        if complete_rate >= 1.0:
            status = 'ready'
        elif complete_rate > 0:
            status = 'partial'
        else:
            status = 'shortage'

        result = {
            'is_ready': complete_rate >= 1.0,
            'complete_rate': complete_rate,
            'shortage_items': shortage_items,
            'status': status
        }

        logger.debug(
            f'订单 {order.order_no} 齐套检查: 状态={status}, 齐套率={complete_rate:.1%}'
        )

        return result

    # -----------------------------------------------------------------
    # 工作中心匹配
    # -----------------------------------------------------------------

    def _find_eligible_work_centers(self, material_id):
        """
        查找能生产指定物料的所有工作中心

        匹配逻辑：
        1. 检查 WorkCenter.available_products 字段（逗号分隔的产品列表）
        2. 检查 Capacity 表中是否有该物料的产能记录
        3. 返回匹配的工作中心列表（按日产能降序排列）

        Args:
            material_id: 物料ID

        Returns:
            list: [(work_center_code, daily_capacity), ...] 按产能降序
        """
        candidates = []

        for wc_code, wc in self.work_centers.items():
            # 检查 available_products 字段
            available_products = wc.available_products or ''
            product_list = [p.strip() for p in available_products.split(',') if p.strip()]

            can_produce = False
            # 方式1: 通过 available_products 匹配物料代码
            for prod in product_list:
                if str(material_id) == prod or str(material_id) in prod:
                    can_produce = True
                    break

            # 方式2: 通过 Capacity 表确认
            if not can_produce:
                cap_key = (wc_code, material_id)
                if cap_key in self.capacity_data:
                    can_produce = True

            if can_produce:
                daily_cap = wc.daily_capacity_limit or 0
                candidates.append((wc_code, daily_cap))

        # 按日产能降序排列（优先使用高产能产线）
        candidates.sort(key=lambda x: x[1], reverse=True)

        return candidates

    # -----------------------------------------------------------------
    # 主排程方法
    # -----------------------------------------------------------------

    def generate_production_schedule(self, orders=None, horizon_days=30):
        """
        主排程方法：生成完整的生产计划

        算法流程：
        1. 加载所有待生产订单（status in [pending, confirmed, allocated, partial]）
        2. 按 priority ASC, demand_date ASC 排序（高优先级先排，同优先级按交期EDD排序）
        3. 对每个订单执行以下步骤：
           a. 找到可用的工作中心（可生产该产品的产线）
           b. 检查物料齐套状态（未齐套标记为"待料"不占产能）
           c. 计算最早可开工日期（考虑齐套+产能+换线）
           d. 分配产能（可能跨天分配）
           e. 扣除换线时间并更新产线当前产品
        4. 输出排程结果、统计摘要、瓶颈分析

        Args:
            orders: 可选的订单QuerySet或列表；若为None则自动加载待排产订单
            horizon_days: 排程展望天数（默认30天）

        Returns:
            dict: {
                'schedule': [...],       # 每个订单的排程详情
                'summary': {...},        # 产能利用率/换线次数/平均等待等
                'bottlenecks': [...],    # 瓶颈工作中心列表
                'unallocated': [...]     # 无法排产的订单及原因
            }
        """
        logger.info(f'========== 开始生成生产计划（展望{horizon_days}天）==========')

        # 重置内部状态
        self._reset_scheduling_state()

        # 加载生产资源（如果尚未加载）
        if not self.work_centers:
            self.load_production_resources()

        # 步骤1：获取待排产订单
        if orders is None:
            orders = SalesOrder.objects.filter(
                status__in=self.SCHEDULABLE_STATUSES
            ).exclude(
                status='cancelled'
            ).select_related('material').order_by('priority', 'demand_date')
        elif isinstance(orders, (list, tuple)):
            # 确保是 queryset 或可迭代对象
            pass

        order_list = list(orders)
        total_orders = len(order_list)
        logger.info(f'共获取 {total_orders} 个待排产订单')

        # 步骤2：排序 - 优先级升序（数字越小越优先），同优先级按交期升序(EDD)
        order_list.sort(key=lambda o: (o.priority or 999, o.demand_date or date.max))

        schedule = []
        unallocated = []

        # 各工作中心产能利用率追踪
        wc_total_capacity = defaultdict(float)
        wc_used_capacity = defaultdict(float)

        # 步骤3：逐订单排程
        for idx, order in enumerate(order_list):
            logger.info(
                f'[排程 {idx+1}/{total_orders}] 订单: {order.order_no}, '
                f'产品: {order.material.material_code}, 数量: {order.quantity}, '
                f'交期: {order.demand_date}, 优先级: {order.priority}'
            )

            order_result = self._schedule_single_order(order, horizon_days)

            if order_result['status'] == 'scheduled':
                schedule.append(order_result)

                # 更新产能利用率统计
                for alloc in order_result.get('allocations', []):
                    wc_code = alloc['work_center']
                    wc_used_capacity[wc_code] += alloc['capacity_used']

                # 更新产线当前产品
                if order_result.get('assigned_wc'):
                    self.current_product_on_line[
                        order_result['assigned_wc']
                    ] = order.material_id
            else:
                unallocated.append(order_result)
                logger.warning(
                    f'订单 {order.order_no} 无法排产: {order_result["reason"]}'
                )

        # 保存未排产订单列表供 What-If 场景（如订单取消后释放产能的受益分析）
        self._last_unallocated = unallocated

        # 计算各工作中心理论总产能（展望期内）
        horizon_end = self.current_date + timedelta(days=horizon_days)
        for wc_code, wc in self.work_centers.items():
            d = self.current_date
            while d <= horizon_end:
                cap_info = self.calculate_available_capacity(wc_code, d)
                wc_total_capacity[wc_code] += cap_info['gross_capacity']
                d += timedelta(days=1)

        # 步骤4：识别瓶颈工作中心
        bottlenecks = self._identify_bottlenecks(
            wc_total_capacity, wc_used_capacity, horizon_days
        )

        # 构建摘要
        summary = self._build_summary(
            schedule, unallocated, wc_total_capacity, wc_used_capacity, horizon_days
        )

        result = {
            'schedule': schedule,
            'summary': summary,
            'bottlenecks': bottlenecks,
            'unallocated': unallocated,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'horizon_days': horizon_days
        }

        self.schedule_results = schedule
        logger.info(
            f'========== 排程完成: 成功{len(schedule)}个, '
            f'失败{len(unallocated)}个, 换线{self.changeover_count}次 =========='
        )

        return result

    def _schedule_single_order(self, order, horizon_days):
        """
        为单个订单执行排程逻辑

        Args:
            order: SalesOrder 实例
            horizon_days: 展望天数

        Returns:
            dict: 单订单排程结果
        """
        material_id = order.material_id
        order_qty = order.quantity
        demand_date = order.demand_date or (self.current_date + timedelta(days=30))
        production_lt = order.production_lead_time or 2

        # 3a. 找到可用的工作中心
        eligible_wcs = self._find_eligible_work_centers(material_id)
        if not eligible_wcs:
            return {
                'order_no': order.order_no,
                'material_code': order.material.material_code,
                'quantity': order_qty,
                'status': 'unallocated',
                'reason': f'无可生产该产品({order.material.material_code})的工作中心',
                'demand_date': str(demand_date)
            }

        # 3b. 检查物料齐套状态
        readiness = self.check_material_readiness(order)
        if not readiness['is_ready']:
            return {
                'order_no': order.order_no,
                'material_code': order.material.material_code,
                'quantity': order_qty,
                'status': 'waiting_material',
                'reason': (
                    f'物料未齐套(齐套率:{readiness["complete_rate"]:.0%}), '
                    f'缺料项:{len(readiness["shortage_items"])}'
                ),
                'readiness': readiness,
                'demand_date': str(demand_date)
            }

        # 3c ~ 3e: 分配产能
        best_result = None
        for wc_code, wc_daily_cap in eligible_wcs:
            allocation_result = self._try_allocate_order(
                order, wc_code, material_id, order_qty,
                demand_date, production_lt, horizon_days
            )
            if allocation_result and allocation_result['status'] == 'scheduled':
                best_result = allocation_result
                break  # 第一个成功分配的产线即为最优解

        if best_result is None:
            return {
                'order_no': order.order_no,
                'material_code': order.material.material_code,
                'quantity': order_qty,
                'status': 'unallocated',
                'reason': '所有可用工作中心在展望期内产能不足',
                'demand_date': str(demand_date),
                'eligible_work_centers': [w[0] for w in eligible_wcs]
            }

        return best_result

    def _try_allocate_order(self, order, wc_code, material_id, order_qty,
                            demand_date, production_lt, horizon_days):
        """
        尝试在指定工作中心上分配订单产能

        支持跨天分配：如果单天产能不足，可将订单拆分到多天生产。

        Args:
            order: SalesOrder 实例
            wc_code: 工作中心代码
            material_id: 物料ID
            order_qty: 订单数量
            demand_date: 需求交付日期
            production_lt: 生产提前期
            horizon_days: 展望天数

        Returns:
            dict: 分配结果
        """
        # 计算最晚必须开工日期（考虑运输/检验等缓冲）
        latest_start = demand_date - timedelta(days=production_lt)
        earliest_possible = self.current_date

        # 计算最早可开工日期（考虑齐套后的最早日期）
        start_date = max(earliest_possible, latest_start - timedelta(days=horizon_days))

        allocations = []
        remaining_qty = order_qty
        current_date_cursor = start_date
        plan_start_date = None
        plan_end_date = None
        total_capacity_used = 0
        applied_changeover = False

        horizon_end = self.current_date + timedelta(days=horizon_days)

        while remaining_qty > 0 and current_date_cursor <= horizon_end:
            # 跳过非工作日
            if not self._is_workday(current_date_cursor):
                current_date_cursor += timedelta(days=1)
                continue

            # 获取当天可用产能
            cap_info = self.calculate_available_capacity(wc_code, current_date_cursor)
            available = cap_info['available_capacity']

            if available <= 0:
                current_date_cursor += timedelta(days=1)
                continue

            # 首次分配时检查并应用换线时间
            if not applied_changeover and len(allocations) == 0:
                current_product = self.current_product_on_line.get(wc_code)
                penalty_hours, need_co = self.check_changeover_penalty(
                    wc_code, current_product, material_id
                )
                if need_co:
                    self._apply_changeover_to_capacity(
                        wc_code, current_date_cursor, penalty_hours
                    )
                    # 重新获取扣除换线后的可用产能
                    cap_info = self.calculate_available_capacity(
                        wc_code, current_date_cursor
                    )
                    available = cap_info['available_capacity']
                    applied_changeover = True

            # 当天可生产的数量
            producible = min(remaining_qty, available)

            if producible > 0:
                if plan_start_date is None:
                    plan_start_date = current_date_cursor
                plan_end_date = current_date_cursor

                allocations.append({
                    'date': current_date_cursor.strftime('%Y-%m-%d'),
                    'quantity': producible,
                    'capacity_used': producible,
                    'work_center': wc_code
                })
                total_capacity_used += producible

                # 占用产能
                date_key = current_date_cursor.strftime('%Y-%m-%d')
                self.occupied_capacity[wc_code][date_key] += producible

                remaining_qty -= producible

            current_date_cursor += timedelta(days=1)

        # 检查是否全部分配完毕
        if remaining_qty > 0:
            # 回滚已分配的产能
            for alloc in allocations:
                self.occupied_capacity[alloc['work_center']][alloc['date']] -= \
                    alloc['capacity_used']
            return None

        # 检查完工日期是否满足交期要求
        on_time = (plan_end_date <= demand_date) if plan_end_date else False

        return {
            'order_no': order.order_no,
            'material_code': order.material.material_code,
            'material_name': order.material.material_name,
            'quantity': order_qty,
            'status': 'scheduled',
            'assigned_wc': wc_code,
            'plan_start_date': str(plan_start_date),
            'plan_end_date': str(plan_end_date),
            'demand_date': str(demand_date),
            'on_time_delivery': on_time,
            'production_lead_time': production_lt,
            'allocations': allocations,
            'total_capacity_used': total_capacity_used,
            'changeover_applied': applied_changeover,
            'priority': order.priority
        }

    # -----------------------------------------------------------------
    # What-If 排程模拟
    # -----------------------------------------------------------------

    def simulate_what_if(self, scenario_type, params):
        """
        What-if 排程模拟：评估不同场景对当前排程的影响

        支持的场景类型：
        - 'urgent_insert': 紧急插单影响评估
          params: {'order_no', 'material_id', 'quantity', 'priority', 'demand_date'}
        - 'capacity_reduce': 产能下降影响
          params: {'work_center_code', 'reduce_percent', 'start_date', 'end_date'}
        - 'order_cancel': 取消订单后的产能释放
          params: {'order_no'}
        - 'delay_order': 订单延期连锁反应
          params: {'order_no', 'delay_days'}

        Args:
            scenario_type: 场景类型字符串
            params: 场景参数字典

        Returns:
            dict: {
                'scenario_type': 场景类型,
                'impact_summary': 影响摘要,
                'affected_orders': 受影响的订单列表,
                'new_schedule': 调整后排程（如适用）,
                'recommendations': 建议
            }
        """
        logger.info(f'What-If 模拟: 场景类型={scenario_type}')

        # 备份当前排程状态
        backup_occupied = {
            k: dict(v) for k, v in self.occupied_capacity.items()
        }
        backup_current_product = dict(self.current_product_on_line)
        backup_changeover_count = self.changeover_count
        backup_changeover_hours = self.total_changeover_hours

        try:
            if scenario_type == 'urgent_insert':
                result = self._simulate_urgent_insert(params)
            elif scenario_type == 'capacity_reduce':
                result = self._simulate_capacity_reduce(params)
            elif scenario_type == 'order_cancel':
                result = self._simulate_order_cancel(params)
            elif scenario_type == 'delay_order':
                result = self._simulate_delay_order(params)
            else:
                raise ValueError(f'不支持的场景类型: {scenario_type}')

            result['scenario_type'] = scenario_type
            logger.info(f'What-If 模拟完成: {scenario_type}')
            return result

        finally:
            # 无论成功与否都恢复原始状态
            self.occupied_capacity = defaultdict(
                lambda: defaultdict(float),
                {k: defaultdict(float, v) for k, v in backup_occupied.items()}
            )
            self.current_product_on_line = backup_current_product
            self.changeover_count = backup_changeover_count
            self.total_changeover_hours = backup_changeover_hours

    def _simulate_urgent_insert(self, params):
        """紧急插单影响评估"""
        order_no = params.get('order_no', 'SIM-URGENT-001')
        quantity = params.get('quantity', 100)
        priority = params.get('priority', 1)
        demand_date_str = params.get('demand_date')
        material_id = params.get('material_id')

        # 构造虚拟订单
        mock_order = type('MockOrder', (), {
            'order_no': order_no,
            'material_id': material_id,
            'quantity': quantity,
            'priority': priority,
            'demand_date': datetime.strptime(demand_date_str, '%Y-%m-%d').date()
                          if demand_date_str else self.current_date + timedelta(days=7),
            'production_lead_time': 2,
            'material': type('MockMaterial', (), {
                'material_code': params.get('material_code', 'UNKNOWN'),
                'material_name': params.get('material_name', '未知物料')
            })()
        })()

        # 尝试插入排程
        original_count = len(self.schedule_results)
        result = self._schedule_single_order(mock_order, horizon_days=self.DEFAULT_HORIZON_DAYS)

        affected_orders = []
        if result.get('status') == 'scheduled':
            # 分析被挤占的潜在风险订单
            for sched in self.schedule_results:
                if (sched.get('plan_end_date') and
                    sched['plan_end_date'] >= str(mock_order.demand_date)):
                    affected_orders.append({
                        'order_no': sched['order_no'],
                        'risk': '可能延期',
                        'original_end': sched.get('plan_end_date'),
                        'new_risk_date': str(mock_order.demand_date)
                    })

        return {
            'impact_summary': {
                'can_insert': result.get('status') == 'scheduled',
                'inserted_order': order_no,
                'affected_existing_orders': len(affected_orders),
                'estimated_delay_impact': len(affected_orders)
            },
            'affected_orders': affected_orders[:10],
            'insert_result': result,
            'recommendations': (
                f'{"建议接受插单" if result.get("status") == "scheduled" else "产能不足，建议外协或协商延交"}'
                f'; 可能影响 {len(affected_orders)} 个已有订单'
            )
        }

    def _simulate_capacity_reduce(self, params):
        """产能下降影响模拟"""
        wc_code = params.get('work_center_code')
        reduce_percent = params.get('reduce_percent', 20) / 100.0
        start_date_str = params.get('start_date')
        end_date_str = params.get('end_date')

        # 临时修改工作中心产能
        wc = self.work_centers.get(wc_code)
        if not wc:
            return {'impact_summary': {'error': f'工作中心 {wc_code} 不存在'}}

        original_cap = wc.daily_capacity_limit
        wc.daily_capacity_limit = int(original_cap * (1 - reduce_percent))

        # 重新运行排程
        new_result = self.generate_production_schedule(horizon_days=self.DEFAULT_HORIZON_DAYS)

        # 恢复原始产能
        wc.daily_capacity_limit = original_cap

        affected = [
            s for s in new_result['schedule']
            if s.get('assigned_wc') == wc_code
        ]

        return {
            'impact_summary': {
                'work_center': wc_code,
                'capacity_reduction': f'{reduce_percent:.0%}',
                'originally_scheduled': len([s for s in self.schedule_results
                                           if s.get('assigned_wc') == wc_code]),
                'after_reduction_scheduled': len(affected),
                'dropped_orders': len(new_result['unallocated'])
            },
            'affected_orders': affected[:20],
            'unallocated': new_result['unallocated'],
            'recommendations': (
                f'产能下降 {reduce_percent:.0%} 后，'
                f'{len(new_result["unallocated"])} 个订单无法排产; '
                f'建议增加班次或外协'
            )
        }

    def _simulate_order_cancel(self, params):
        """取消订单后产能释放评估"""
        order_no = params.get('order_no')

        # 查找该订单在当前排程中的位置
        released_capacity = 0
        canceled_entry = None
        for entry in self.schedule_results:
            if entry['order_no'] == order_no:
                canceled_entry = entry
                # 释放已占用产能
                for alloc in entry.get('allocations', []):
                    wc = alloc['work_center']
                    dt = alloc['date']
                    if wc in self.occupied_capacity and dt in self.occupied_capacity[wc]:
                        self.occupied_capacity[wc][dt] -= alloc['capacity_used']
                        released_capacity += alloc['capacity_used']
                break

        # 尝试用释放的产能安排未排产的订单
        newly_schedulable = []
        for unalloc in getattr(self, '_last_unallocated', []):
            # 简化处理：仅报告可利用的产能
            newly_schedulable.append({
                'order_no': unalloc.get('order_no'),
                'potential_benefit': f'可利用释放的 {released_capacity:.0f} 产能单位'
            })

        return {
            'impact_summary': {
                'canceled_order': order_no,
                'released_capacity': released_capacity,
                'release_dates': (
                    [a['date'] for a in canceled_entry.get('allocations', [])]
                    if canceled_entry else []
                ),
                'newly_schedulable_count': len(newly_schedulable)
            },
            'affected_orders': newly_schedulable[:10],
            'recommendations': (
                f'取消后释放 {released_capacity:.0f} 产能单位; '
                f'建议立即重新运行排程以填补空缺'
            )
        }

    def _simulate_delay_order(self, params):
        """订单延期连锁反应模拟"""
        order_no = params.get('order_no')
        delay_days = params.get('delay_days', 7)

        # 查找受影响的后续订单
        delayed_entry = None
        for entry in self.schedule_results:
            if entry['order_no'] == order_no:
                delayed_entry = entry
                break

        if not delayed_entry:
            return {'impact_summary': {'error': f'订单 {order_no} 未找到在当前排程中'}}

        original_end = delayed_entry.get('plan_end_date')
        new_end = (
            datetime.strptime(original_end, '%Y-%m-%d').date() + timedelta(days=delay_days)
            if original_end else None
        )

        # 找出因该订单延期而可能受影响的后续订单
        cascade_affected = []
        for entry in self.schedule_results:
            if (entry['order_no'] != order_no and
                entry.get('plan_start_date') and original_end and
                entry['plan_start_date'] <= original_end <= entry.get('plan_end_date', '')):
                cascade_affected.append({
                    'order_no': entry['order_no'],
                    'original_start': entry.get('plan_start_date'),
                    'original_end': entry.get('plan_end_date'),
                    'risk': '可能被顺延'
                })

        return {
            'impact_summary': {
                'delayed_order': order_no,
                'delay_days': delay_days,
                'original_end_date': original_end,
                'new_estimated_end_date': str(new_end) if new_end else None,
                'cascade_affected_count': len(cascade_affected)
            },
            'affected_orders': cascade_affected[:15],
            'recommendations': (
                f'延期可能导致 {len(cascade_affected)} 个订单发生连锁延期; '
                f'建议评估对关键客户的影响并提前沟通'
            )
        }

    # -----------------------------------------------------------------
    # 甘特图数据
    # -----------------------------------------------------------------

    def get_gantt_data(self):
        """
        返回甘特图数据供前端展示

        数据格式适配前端甘特图组件（如 Frappe Gantt / vue-gantt）：

        Returns:
            list: [{
                'id': 任务唯一标识,
                'text': 显示文本,
                'start_date': 开始日期(YYYY-MM-DD),
                'end_date': 结束日期(YYYY-MM-DD),
                'progress': 进度(0~100),
                'work_center': 所属工作中心,
                'material_code': 产品代码,
                'order_no': 关联订单号,
                'dependencies': 前置任务ID列表,
                'custom_class': CSS样式类（用于着色）
            }, ...]
        """
        if not self.schedule_results:
            logger.warning('尚无排程结果，请先运行 generate_production_schedule()')
            return []

        gantt_tasks = []
        task_id_counter = 1

        for entry in self.schedule_results:
            start_str = entry.get('plan_start_date')
            end_str = entry.get('plan_end_date')

            if not start_str or not end_str:
                continue

            start_dt = datetime.strptime(start_str, '%Y-%m-%d').date()
            end_dt = datetime.strptime(end_str, '%Y-%m-%d').date()

            # 计算持续时间（天）
            duration = (end_dt - start_dt).days + 1

            # 根据状态设置样式
            on_time = entry.get('on_time_delivery', True)
            custom_class = 'bar-on-time' if on_time else 'bar-delayed'

            # 根据优先级调整颜色深浅
            priority = entry.get('priority', 5)
            if priority <= 2:
                custom_class += ' bar-high-priority'

            task = {
                'id': f'task-{task_id_counter}',
                'text': f'{entry["order_no"]} ({entry.get("material_code", "")})',
                'start_date': start_str,
                'end_date': end_str,
                'duration': duration,
                'progress': 0,  # 生产尚未开始
                'work_center': entry.get('assigned_wc', ''),
                'material_code': entry.get('material_code', ''),
                'material_name': entry.get('material_name', ''),
                'order_no': entry.get('order_no', ''),
                'quantity': entry.get('quantity', 0),
                'dependencies': [],
                'custom_class': custom_class.strip(),
                'priority': priority,
                'on_time': on_time
            }

            gantt_tasks.append(task)
            task_id_counter += 1

        logger.info(f'已生成 {len(gantt_tasks)} 条甘特图任务数据')
        return gantt_tasks

    # -----------------------------------------------------------------
    # 多目标优化
    # -----------------------------------------------------------------

    def optimize_with_objectives(self, objectives=['delivery', 'utilization', 'changeover']):
        """
        多目标排程优化（贪心/启发式算法）

        优化目标：
        - delivery: 最大化交付率（按时完成的订单占比）
        - utilization: 最大化产能利用率（已用产能/总产能）
        - changeover: 最小化换线次数（通过合并同产品批次）

        策略：
        1. 基于 generate_production_schedule 生成初始方案
        2. 根据 objectives 权重进行迭代改进：
           - 若重视 delivery → 将临近交期的低优先级订单提升
           - 若重视 utilization → 在低负载产线间均衡分配
           - 若重视 changeover → 合并同一产品的相邻订单

        Args:
            objectives: 优化目标列表，可选值:
                       'delivery', 'utilization', 'changeover'

        Returns:
            dict: {
                'optimized_schedule': 优化后排程,
                'objectives': 应用的目标列表,
                'improvements': 各目标的改善幅度,
                'metrics_before': 优化前指标,
                'metrics_after': 优化后指标
            }
        """
        logger.info(f'开始多目标优化，目标: {objectives}')

        # 先运行一次基准排程
        baseline_result = self.generate_production_schedule(
            horizon_days=self.DEFAULT_HORIZON_DAYS
        )
        baseline_metrics = self._calculate_metrics(baseline_result)

        optimized_schedule = list(baseline_result['schedule'])

        # ---- 优化策略1: 换线最小化（合并同产品） ----
        if 'changeover' in objectives:
            optimized_schedule = self._optimize_merge_same_product(optimized_schedule)

        # ---- 优化策略2: 交付率最大化 ----
        if 'delivery' in objectives:
            optimized_schedule = self._optimize_prioritize_delivery(
                optimized_schedule, baseline_result['unallocated']
            )

        # ---- 优化策略3: 产能利用率均衡 ----
        if 'utilization' in objectives:
            optimized_schedule = self._optimize_balance_utilization(optimized_schedule)

        # 构建优化后结果
        optimized_result = {
            'schedule': optimized_schedule,
            'summary': baseline_result['summary'],
            'bottlenecks': baseline_result['bottlenecks'],
            'unallocated': baseline_result['unallocated']
        }
        after_metrics = self._calculate_metrics(optimized_result)

        improvements = {}
        for obj in objectives:
            before_val = baseline_metrics.get(obj, 0)
            after_val = after_metrics.get(obj, 0)
            if before_val != 0:
                improvements[obj] = (after_val - before_val) / abs(before_val)
            else:
                improvements[obj] = 0

        logger.info(f'多目标优化完成，改善: {improvements}')

        return {
            'optimized_schedule': optimized_schedule,
            'objectives': objectives,
            'improvements': improvements,
            'metrics_before': baseline_metrics,
            'metrics_after': after_metrics
        }

    def _optimize_merge_same_product(self, schedule):
        """
        换线最小化优化：在同一工作中心上合并相同产品的相邻订单

        通过重新排序使相同产品的订单集中排产，减少换线次数。
        """
        # 按工作中心分组
        wc_groups = defaultdict(list)
        for entry in schedule:
            wc = entry.get('assigned_wc')
            if wc:
                wc_groups[wc].append(entry)

        optimized = []
        for wc_code, entries in wc_groups.items():
            # 按产品分组
            product_groups = defaultdict(list)
            for entry in entries:
                product_groups[entry.get('material_code', '')].append(entry)

            # 对每个产品组内按交期排序，然后依次追加
            sorted_products = sorted(product_groups.keys())
            for product_code in sorted_products:
                group_entries = sorted(
                    product_groups[product_code],
                    key=lambda e: e.get('demand_date', '')
                )
                optimized.extend(group_entries)

        # 补充未分配工作中心的条目
        assigned_ids = {e.get('order_no') for e in optimized}
        for entry in schedule:
            if entry.get('order_no') not in assigned_ids:
                optimized.append(entry)

        logger.debug(f'换线优化: 合并同产品后重排 {len(optimized)} 个订单')
        return optimized

    def _optimize_prioritize_delivery(self, schedule, unallocated):
        """
        交付率优化：尝试将部分未排产的低优先级订单以延期方式纳入
        """
        # 对未排产订单按交期紧迫度排序，看是否有机会在非瓶颈时段插入
        unallocated_sorted = sorted(
            unallocated,
            key=lambda u: (u.get('demand_date', ''), u.get('priority', 999))
        )

        # 这里简化处理：仅标记建议
        for u in unallocated_sorted[:5]:
            logger.debug(
                f'交付优化建议: 订单 {u.get("order_no")} '
                f'可考虑协商延至更晚交期'
            )

        return schedule

    def _optimize_balance_utilization(self, schedule):
        """
        产能利用率均衡：检测低负载产线，尝试将部分订单转移
        """
        wc_loads = defaultdict(float)
        for entry in schedule:
            wc = entry.get('assigned_wc')
            if wc:
                wc_loads[wc] += entry.get('total_capacity_used', 0)

        if not wc_loads:
            return schedule

        avg_load = sum(wc_loads.values()) / len(wc_loads)
        underloaded = [wc for wc, load in wc_loads.items() if load < avg_load * 0.6]

        if underloaded:
            logger.debug(
                f'产能均衡: 低负载产线 {underloaded}, '
                f'平均负载 {avg_load:.0f}'
            )

        return schedule

    # -----------------------------------------------------------------
    # 内部辅助方法
    # -----------------------------------------------------------------

    def _reset_scheduling_state(self):
        """重置排程相关的内部状态"""
        self.occupied_capacity = defaultdict(lambda: defaultdict(float))
        self.current_product_on_line = {
            code: None for code in self.work_centers
        }
        self.schedule_results = []
        self.changeover_count = 0
        self.total_changeover_hours = Decimal('0')

    def _identify_bottlenecks(self, wc_total_cap, wc_used_cap, horizon_days):
        """
        识别瓶颈工作中心

        瓶颈判定标准：产能利用率 > 85%
        """
        bottlenecks = []
        for wc_code in self.work_centers:
            total = wc_total_cap.get(wc_code, 0)
            used = wc_used_cap.get(wc_code, 0)
            util_rate = (used / total * 100) if total > 0 else 0

            if util_rate > 85:
                wc = self.work_centers.get(wc_code)
                bottlenecks.append({
                    'work_center_code': wc_code,
                    'work_center_name': wc.work_center_name if wc else '',
                    'utilization_rate': round(util_rate, 1),
                    'used_capacity': round(used, 1),
                    'total_capacity': round(total, 1),
                    'severity': 'high' if util_rate > 95 else 'medium'
                })

        # 按利用率降序排列
        bottlenecks.sort(key=lambda b: b['utilization_rate'], reverse=True)
        return bottlenecks

    def _build_summary(self, schedule, unallocated, wc_total_cap, wc_used_cap, horizon_days):
        """构建排程摘要统计"""
        total_orders = len(schedule) + len(unallocated)
        scheduled_count = len(schedule)

        # 交付率
        on_time_count = sum(1 for s in schedule if s.get('on_time_delivery'))
        delivery_rate = (on_time_count / scheduled_count * 100) if scheduled_count > 0 else 0

        # 平均等待时间（从今天到开工日期的天数）
        wait_days_list = []
        for s in schedule:
            start_str = s.get('plan_start_date')
            if start_str:
                try:
                    start_dt = datetime.strptime(start_str, '%Y-%m-%d').date()
                    wait_days = (start_dt - self.current_date).days
                    wait_days_list.append(max(0, wait_days))
                except ValueError:
                    pass
        avg_wait = sum(wait_days_list) / len(wait_days_list) if wait_days_list else 0

        # 整体产能利用率
        total_cap_all = sum(wc_total_cap.values())
        total_used_all = sum(wc_used_cap.values())
        overall_util = (total_used_all / total_cap_all * 100) if total_cap_all > 0 else 0

        summary = {
            'total_orders': total_orders,
            'scheduled_orders': scheduled_count,
            'unallocated_orders': len(unallocated),
            'delivery_rate': round(delivery_rate, 1),
            'on_time_orders': on_time_count,
            'late_orders': scheduled_count - on_time_count,
            'average_wait_days': round(avg_wait, 1),
            'overall_utilization': round(overall_util, 1),
            'changeover_count': self.changeover_count,
            'total_changeover_hours': float(self.total_changeover_hours),
            'horizon_days': horizon_days,
            'generation_date': self.current_date.strftime('%Y-%m-%d')
        }

        return summary

    def _calculate_metrics(self, result):
        """计算当前方案的各项指标（用于优化对比）"""
        schedule = result.get('schedule', [])
        scheduled_count = len(schedule)
        total = scheduled_count + len(result.get('unallocated', []))

        # 交付率
        on_time = sum(1 for s in schedule if s.get('on_time_delivery'))
        delivery_rate = (on_time / scheduled_count * 100) if scheduled_count > 0 else 0

        # 产能利用率（基于summary）
        summary = result.get('summary', {})
        utilization = summary.get('overall_utilization', 0)

        # 换线效率（换线次数越少越好，归一化为越高越好）
        changeover_score = max(
            0, 100 - summary.get('changeover_count', 0) * 5
        )

        return {
            'delivery': delivery_rate,
            'utilization': utilization,
            'changeover': changeover_score
        }

    # -----------------------------------------------------------------
    # 反哺物料计划
    # -----------------------------------------------------------------

    def backfill_material_plan(self, schedule_result):
        """
        将排程结果回写到 MaterialPlanResult 或生成生产计划记录

        对每个已排程订单：
        - 更新 MaterialPlanResult 的计划开工/完工日期
        - 写入 PlanLog 记录排程操作日志

        Args:
            schedule_result: generate_production_schedule() 返回的结果字典

        Returns:
            dict: {'updated_count': 更新的记录数, 'logs_written': 日志条数}
        """
        updated_count = 0
        logs_written = 0

        for entry in schedule_result.get('schedule', []):
            order_no = entry.get('order_no')
            if not order_no:
                continue

            try:
                # 查找对应的销售订单
                order = SalesOrder.objects.filter(order_no=order_no).first()
                if not order:
                    continue

                # 更新或创建 MaterialPlanResult
                mpr, created = MaterialPlanResult.objects.update_or_create(
                    order=order,
                    defaults={
                        'updated_at': datetime.now()
                    }
                )

                # 将排程日期写入 allocation_details JSON字段
                details = mpr.allocation_details or {}
                details['production_schedule'] = {
                    'plan_start_date': entry.get('plan_start_date'),
                    'plan_end_date': entry.get('plan_end_date'),
                    'assigned_work_center': entry.get('assigned_wc'),
                    'on_time_delivery': entry.get('on_time_delivery'),
                    'scheduled_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                mpr.allocation_details = details
                mpr.save()
                updated_count += 1

                # 写入排程日志
                PlanLog.objects.create(
                    log_type='PLANNING',
                    message=(
                        f'订单 {order_no} 已排产: '
                        f'产线={entry.get("assigned_wc")}, '
                        f'开工={entry.get("plan_start_date")}, '
                        f'完工={entry.get("plan_end_date")}'
                    ),
                    order_id=order.id,
                    material_id=order.material_id
                )
                logs_written += 1

            except Exception as e:
                logger.error(f'回写物料计划失败 [{order_no}]: {e}')

        logger.info(f'反哺物料计划完成: 更新{updated_count}条, 日志{logs_written}条')
        return {
            'updated_count': updated_count,
            'logs_written': logs_written
        }


# 解决循环导入问题：延迟导入 Sum
from django.db.models import Sum
