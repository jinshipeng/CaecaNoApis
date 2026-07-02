import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from collections import defaultdict
import threading
import hashlib
import time
import logging
from django.core.cache import cache
from .utils.safe_cache import safe_get, safe_set
from django.db import transaction
from django.db.models import Q, F, Sum, Max
from .models import (
    SalesOrder, Material, BillOfMaterials, Inventory, SupplierCommitment,
    SupplierMaterial, OrderAllocation, Capacity, MaterialPlanResult, PlanLog,
    FactoryCalendar, Supplier, WorkCenter, FactoryTransfer, PriorityRule,
    PurchaseOrder, Notification
)

logger = logging.getLogger(__name__)

class MaterialPlanner:
    """物料计划优化器 - 集成新增数据字段"""

    CONSUMPTION_PRIORITY_CHOICES = ['FIFO', 'LIFO', 'PRIORITY', 'SUPPLIER_FIRST', 'EXPIRY_FIRST', 'INVENTORY_FIRST', 'VALUE_AWARE']
    
    PRODUCTION_LEAD_TIME = 2

    # 工厂间调拨默认耗时(天)
    DEFAULT_TRANSFER_DAYS = 1
    # 调拨成本单价(元/件)
    DEFAULT_TRANSFER_COST_PER_UNIT = 0.5

    # 物流时间计算（硬性约束：海运45天/空运3天）
    SHIPPING_DAYS_MAP = {
        'sea': 45,     # 海运默认45天
        'air': 3,      # 空运默认3天
    }

    def __init__(self, consumption_priority='FIFO', factory_id=None, strategy=None):
        self.inventory_cache = {}
        self.bom_cache = {}
        self.alternative_cache = {}
        self.consumption_priority = consumption_priority
        self.lock = threading.Lock()
        self.allocation_history = []
        self.material_shortage_records = []
        self.order_promise_changes = defaultdict(int)
        self.material_info_cache = {}
        self.supplier_info_cache = {}
        self.workcenter_info_cache = {}
        self.factory_id = factory_id
        self.factory_calendar_cache = {}
        self.batch_allocation_cache = {}
        self.capacity_utilization = defaultdict(float)
        self.production_schedule = defaultdict(list)
        self.cache_enabled = True
        self.cache_ttl = 3600  # 缓存有效期1小时
        self.previous_plan_results = {}
        self.strategy = strategy  # 用于分配加成计算
        self.dynamic_priority_rules = {}
        # 多工厂调拨相关
        self.factory_inventory_cache = defaultdict(lambda: defaultdict(list))  # {factory_code: {material_id: [inventory_items]}}
        self.transfer_records = []  # 调拨记录
        self.priority_rule = None  # 优先级规则配置
        self.order_cache = {}  # 订单缓存，避免重复查询数据库
        # 计算模式：serial(串行) / parallel(并行)
        self._computation_mode = 'serial'

        # ========== L2优化: BOM DAG缓存 ==========
        # 将递归BOM展开升级为DAG(有向无环图)缓存机制
        # 避免同一物料被多个父件引用时的重复递归计算
        # 预期性能提升: 10x以上（尤其对深层/共享子件的BOM结构）
        self._bom_dag_cache = {}       # {material_id: {child_id: {quantity, scrap_rate, ...}}}
        self._bom_dag_built = False    # DAG是否已构建
        self._bom_dag_stats = {'hits': 0, 'misses': 0, 'build_count': 0}

    def get_computation_mode(self):
        """返回当前计算模式"""
        return self._computation_mode

    def set_computation_mode(self, mode):
        """设置计算模式（serial/parallel）"""
        if mode not in ('serial', 'parallel'):
            raise ValueError(f"不支持的计算模式: {mode}，仅支持 serial 或 parallel")
        self._computation_mode = mode
        return {'success': True, 'current_mode': mode}

    def _generate_cache_key(self, prefix, **kwargs):
        """生成唯一缓存键"""
        key_string = f"{prefix}_{hashlib.md5(str(sorted(kwargs.items())).encode()).hexdigest()}"
        return f"mrp_{key_string}"

    def _get_cached_result(self, key):
        """获取缓存结果"""
        if not self.cache_enabled:
            return None
        try:
            return safe_get(key)
        except Exception:
            return None

    def _set_cached_result(self, key, value, ttl=None):
        """设置缓存结果"""
        if not self.cache_enabled:
            return
        try:
            safe_set(key, value, ttl or self.cache_ttl)
        except Exception:
            pass

    def _invalidate_cache(self, pattern):
        """清除匹配模式的缓存"""
        try:
            cache.delete_pattern(f"mrp_{pattern}*")
        except Exception:
            pass

    def load_material_info_cache(self):
        """加载物料信息缓存（仅加载需要的字段，优化数据库查询）"""
        self.material_info_cache = {}
        # 只加载实际使用的字段，减少数据库传输量
        materials = Material.objects.all().only(
            'id', 'material_code', 'material_name', 'material_type',
            'unit', 'shelf_life', 'min_order_qty', 'lead_time',
            'standard_cost', 'sales_price', 'safety_stock', 'min_production_qty'
        )
        for material in materials:
            self.material_info_cache[material.id] = {
                'code': material.material_code,
                'name': material.material_name,
                'type': material.material_type,
                'unit': material.unit,
                'shelf_life': material.shelf_life,
                'min_order_qty': material.min_order_qty,
                'lead_time': material.lead_time,
                'standard_cost': round(float(material.standard_cost or 0), 2),
                'sales_price': round(float(material.sales_price or 0), 2),
                'safety_stock': int(material.safety_stock or 0),
                'min_production_qty': material.min_production_qty
            }

    def load_supplier_info_cache(self):
        """加载供应商信息缓存（仅加载需要的字段，优化数据库查询）"""
        self.supplier_info_cache = {}
        # 只加载实际使用的字段，减少数据库传输量
        suppliers = Supplier.objects.all().only(
            'id', 'supplier_code', 'supplier_name', 'rating',
            'delivery_reliability', 'normal_lead_time'
        )
        for supplier in suppliers:
            self.supplier_info_cache[supplier.id] = {
                'code': supplier.supplier_code,
                'name': supplier.supplier_name,
                'rating': supplier.rating,
                'delivery_reliability': supplier.delivery_reliability,
                'normal_lead_time': supplier.normal_lead_time
            }

        # 性能优化：预加载所有 SupplierMaterial 关联（避免 analyze_shortage 每次查DB）
        self.supplier_material_cache = defaultdict(list)
        try:
            for sm in SupplierMaterial.objects.filter(is_forbidden=False).select_related('supplier'):
                self.supplier_material_cache[sm.material_id].append(sm)
        except Exception:
            self.supplier_material_cache = defaultdict(list)

    def load_forbidden_materials(self):
        """加载供应商禁用料信息（支持实时变化）"""
        self.forbidden_materials = {}
        forbidden_sm = SupplierMaterial.objects.select_related('supplier', 'material').filter(is_forbidden=True)
        for sm in forbidden_sm:
            key = (sm.supplier_id, sm.material_id)
            self.forbidden_materials[key] = {
                'supplier_id': sm.supplier_id,
                'material_id': sm.material_id,
                'forbidden_reason': sm.forbidden_reason,
                'created_at': sm.created_at,
                'updated_at': sm.updated_at
            }
    
    def is_material_forbidden(self, supplier_id, material_id):
        """检查供应商物料是否被禁用"""
        key = (supplier_id, material_id)
        return key in self.forbidden_materials
    
    def check_forbidden_material_changes(self):
        """检查禁用料是否发生变化，返回变化记录"""
        current_forbidden = set()
        forbidden_sm = SupplierMaterial.objects.select_related('supplier', 'material').filter(is_forbidden=True)
        for sm in forbidden_sm:
            current_forbidden.add((sm.supplier_id, sm.material_id))
        
        previous_forbidden = set(self.forbidden_materials.keys())
        
        newly_forbidden = current_forbidden - previous_forbidden
        newly_allowed = previous_forbidden - current_forbidden
        
        changes = {
            'newly_forbidden': [],
            'newly_allowed': [],
            'has_changes': len(newly_forbidden) > 0 or len(newly_allowed) > 0
        }
        
        # 批量预加载相关物料和供应商（避免循环内N+1查询）
        all_keys = newly_forbidden | newly_allowed
        if all_keys:
            supplier_ids = list({k[0] for k in all_keys})
            material_ids = list({k[1] for k in all_keys})
            suppliers_map = {s.id: s for s in Supplier.objects.filter(id__in=supplier_ids)}
            materials_map = {m.id: m for m in Material.objects.filter(id__in=material_ids)}
            sm_map = {
                (sm.supplier_id, sm.material_id): sm
                for sm in SupplierMaterial.objects.select_related('supplier', 'material').filter(
                    Q(supplier_id__in=supplier_ids) & Q(material_id__in=material_ids)
                )
            }

        for key in newly_forbidden:
            sm = sm_map.get(key)
            if sm:
                material = materials_map.get(key[1])
                supplier = suppliers_map.get(key[0])
                changes['newly_forbidden'].append({
                    'supplier_code': supplier.supplier_code if supplier else '',
                    'supplier_name': supplier.supplier_name if supplier else '',
                    'material_code': material.material_code if material else '',
                    'material_name': material.material_name if material else '',
                    'reason': sm.forbidden_reason
                })

        for key in newly_allowed:
            sm = sm_map.get(key)
            if sm:
                material = materials_map.get(key[1])
                supplier = suppliers_map.get(key[0])
                changes['newly_allowed'].append({
                    'supplier_code': supplier.supplier_code if supplier else '',
                    'supplier_name': supplier.supplier_name if supplier else '',
                    'material_code': material.material_code if material else '',
                    'material_name': material.material_name if material else ''
                })
        
        if changes['has_changes']:
            self.load_forbidden_materials()
            PlanLog.objects.create(
                log_type='INFO',
                message=f'禁用料发生变化: {len(changes["newly_forbidden"])} 个禁用, {len(changes["newly_allowed"])} 个解禁'
            )
        
        # P7增强：检测受影响的活跃订单并通知
        if changes['newly_forbidden']:
            affected_order_info = self._analyze_forbidden_impact_on_orders(changes['newly_forbidden'])
            changes['affected_orders'] = affected_order_info
            if affected_order_info['affected_count'] > 0:
                PlanLog.objects.create(
                    log_type='WARNING',
                    message=f'禁用料变更影响 {affected_order_info["affected_count"]} 个活跃订单，建议重新执行物料计划'
                )
        
        return changes

    def _analyze_forbidden_impact_on_orders(self, newly_forbidden_list):
        """分析禁用料变化对活跃订单的影响"""
        from .models import SalesOrder, OrderAllocation, MaterialPlanResult
        
        affected_material_ids = list({item['material_id'] for item in newly_forbidden_list})
        affected_order_ids = set()
        
        # 方式1：查找使用了这些物料的活跃订单（通过分配记录）
        allocated_orders = OrderAllocation.objects.filter(
            material_id__in=affected_material_ids
        ).values_list('order_id', flat=True).distinct()
        affected_order_ids.update(allocated_orders)
        
        # 方式2：查找BOM中包含这些物料的活跃订单
        for mat_id in affected_material_ids:
            if mat_id in self.bom_cache:
                parent_ids = set()  # 使用这些子件的父件
                # 反查：哪些父件使用了这个子件
                for parent_id, bom_list in self.bom_cache.items():
                    for bom in bom_list:
                        if bom.get('child_id') == mat_id:
                            parent_ids.add(parent_id)
                
                if parent_ids:
                    parent_orders = SalesOrder.objects.filter(
                        material_id__in=list(parent_ids),
                        status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing'],
                    ).values_list('id', flat=True).distinct()
                    affected_order_ids.update(parent_orders)
        
        affected_orders = SalesOrder.objects.filter(id__in=list(affected_order_ids)).select_related('material')[:20]
        
        return {
            'affected_count': len(affected_order_ids),
            'affected_order_details': [
                {
                    'order_id': o.id,
                    'order_no': o.order_no,
                    'material_code': o.material.material_code if o.material else '',
                    'priority': o.priority,
                    'demand_date': str(o.demand_date) if o.demand_date else '',
                    'status': o.status,
                }
                for o in affected_orders
            ],
            'suggested_action': 'replan' if len(affected_order_ids) > 0 else None,
        }

    def load_workcenter_info_cache(self):
        """加载工作中心/产线信息缓存"""
        self.workcenter_info_cache = {}
        workcenters = WorkCenter.objects.all()
        for wc in workcenters:
            self.workcenter_info_cache[wc.work_center_code] = {
                'id': wc.id,
                'name': wc.work_center_name,
                'daily_capacity_limit': float(wc.daily_capacity_limit or 0),
                'shift_count': wc.shift_count,
                'hours_per_shift': float(wc.hours_per_shift or 0),
                'production_days_per_week': wc.production_days_per_week,
                'changeover_time': float(wc.changeover_time or 0),
                'planned_maintenance_hours': float(wc.planned_maintenance_hours or 0),
                'maintenance_start_date': wc.maintenance_start_date,
                'maintenance_end_date': wc.maintenance_end_date,
                'is_active': wc.is_active,
                'available_products': wc.available_products.split(',') if wc.available_products else []
            }

    def load_factory_calendar(self):
        """加载工厂日历缓存 - 支持多工厂差异化日历"""
        self.factory_calendar_cache = {}  # {factory_code: {date: {...}}}
        calendars = FactoryCalendar.objects.all()
        for cal in calendars:
            # 确保缓存key始终为date类型（数据库可能返回datetime）
            cal_date = cal.date
            if isinstance(cal_date, datetime):
                cal_date = cal_date.date()
            factory = cal.factory_code or 'DEFAULT'
            if factory not in self.factory_calendar_cache:
                self.factory_calendar_cache[factory] = {}
            self.factory_calendar_cache[factory][cal_date] = {
                'is_workday': cal.is_workday,
                'shift_count': cal.shift_count,
                'remarks': cal.remarks
            }

    def is_workday(self, check_date, factory_code=None):
        """检查指定日期是否为工作日 - 支持按工厂查询"""
        factory = factory_code or self.factory_id or 'DEFAULT'
        # 优先查询指定工厂的日历
        if factory in self.factory_calendar_cache:
            if check_date in self.factory_calendar_cache[factory]:
                return self.factory_calendar_cache[factory][check_date]['is_workday']
        # 回退到默认工厂日历
        if factory != 'DEFAULT' and 'DEFAULT' in self.factory_calendar_cache:
            if check_date in self.factory_calendar_cache['DEFAULT']:
                return self.factory_calendar_cache['DEFAULT'][check_date]['is_workday']
        # 最终回退：按星期判断
        default_workdays = [0, 1, 2, 3, 4]
        return check_date.weekday() in default_workdays

    def calculate_production_days(self, start_date, end_date):
        """计算两个日期之间的生产天数"""
        days = 0
        current_date = start_date
        while current_date <= end_date:
            if self.is_workday(current_date):
                days += 1
            current_date += timedelta(days=1)
        return max(1, days)

    def check_capacity_constraint(self, work_center_code, product_code, required_qty, demand_date):
        """检查产能约束"""
        wc = self.workcenter_info_cache.get(work_center_code)
        if not wc or not wc['is_active']:
            return {'available': False, 'reason': '工作中心不存在或未启用'}

        if product_code and wc['available_products'] and product_code not in wc['available_products']:
            return {'available': False, 'reason': '该工作中心不支持此产品'}

        today = date.today()
        days_available = self.calculate_production_days(today, demand_date)
        
        effective_capacity = wc['daily_capacity_limit'] * days_available
        
        if work_center_code in self.capacity_utilization:
            used_capacity = self.capacity_utilization[work_center_code]
        else:
            used_capacity = 0
        
        available_capacity = effective_capacity - used_capacity

        if available_capacity >= required_qty:
            return {
                'available': True,
                'available_capacity': available_capacity,
                'used_capacity': used_capacity,
                'total_capacity': effective_capacity,
                'utilization_rate': used_capacity / effective_capacity if effective_capacity > 0 else 0
            }
        else:
            return {
                'available': False,
                'reason': f'产能不足，可用产能: {available_capacity:.0f}, 需求: {required_qty:.0f}',
                'available_capacity': available_capacity,
                'used_capacity': used_capacity,
                'total_capacity': effective_capacity
            }

    def allocate_capacity(self, work_center_code, product_code, quantity, demand_date):
        """分配产能"""
        check_result = self.check_capacity_constraint(work_center_code, product_code, quantity, demand_date)
        
        if not check_result['available']:
            return check_result

        self.capacity_utilization[work_center_code] += float(quantity or 0)

        if work_center_code not in self.production_schedule:
            self.production_schedule[work_center_code] = []

        self.production_schedule[work_center_code].append({
            'product_code': product_code,
            'quantity': quantity,
            'demand_date': demand_date,
            'allocation_date': datetime.now()
        })

        return {
            'available': True,
            'allocated_quantity': int(quantity or 0),
            'work_center': work_center_code,
            'remaining_capacity': check_result['available_capacity'] - float(quantity or 0)
        }

    def find_alternative_workcenter(self, product_code, required_qty, demand_date):
        """查找替代工作中心"""
        alternatives = []
        
        for wc_code, wc_info in self.workcenter_info_cache.items():
            if not wc_info['is_active']:
                continue
            
            if wc_info['available_products'] and product_code not in wc_info['available_products']:
                continue
            
            check_result = self.check_capacity_constraint(wc_code, product_code, required_qty, demand_date)
            if check_result['available']:
                alternatives.append({
                    'work_center_code': wc_code,
                    'work_center_name': wc_info['name'],
                    'available_capacity': check_result['available_capacity'],
                    'utilization_rate': check_result.get('utilization_rate', 0)
                })
        
        alternatives.sort(key=lambda x: x['utilization_rate'])
        return alternatives

    def allocate_by_batch(self, material_id, required_qty, order_id, batch_preferences=None):
        """按批次分配库存"""
        allocated = 0.0
        allocations = []
        
        if material_id not in self.inventory_cache:
            return allocated, allocations

        inventory_list = self.inventory_cache[material_id]

        if batch_preferences:
            inventory_list = self.sort_inventory_by_batch_preferences(inventory_list, batch_preferences)
        else:
            # L3优化: 构建订单上下文用于Value-aware排序
            _ctx = self._build_allocation_context(order_id, material_id) if hasattr(self, '_build_allocation_context') else None
            inventory_list = self.sort_inventory_by_priority(inventory_list, order_context=_ctx)

        for inv in inventory_list:
            if allocated >= required_qty:
                break

            qty_to_allocate = min(inv['quantity'], required_qty - allocated)

            allocation = {
                'inventory_id': inv['id'],
                'material_id': material_id,
                'quantity': qty_to_allocate,
                'type': inv['type'],
                'order_id': order_id,
                'batch_no': inv.get('batch_no'),
                'expiry_date': inv.get('expiry_date')
            }

            allocations.append(allocation)
            allocated += qty_to_allocate
            inv['quantity'] -= qty_to_allocate

            if inv['quantity'] <= 0:
                self.inventory_cache[material_id].remove(inv)

        return allocated, allocations

    def sort_inventory_by_batch_preferences(self, inventory_list, preferences):
        """按批次偏好排序库存"""
        def get_sort_key(item):
            keys = []
            
            if 'batch_no' in preferences:
                preferred_batches = preferences['batch_no']
                if item.get('batch_no') in preferred_batches:
                    keys.append(0)
                else:
                    keys.append(1)
            
            if 'expiry_date' in preferences and preferences['expiry_date'] == 'earliest':
                keys.append(item.get('expiry_date') or date.max)
            elif 'expiry_date' in preferences and preferences['expiry_date'] == 'latest':
                keys.append(item.get('expiry_date') or date.min)
            
            if 'location' in preferences:
                preferred_locations = preferences['location']
                if item.get('warehouse') in preferred_locations:
                    keys.append(0)
                else:
                    keys.append(1)
            
            return tuple(keys)

        return sorted(inventory_list, key=get_sort_key)

    def load_inventory_cache(self):
        """加载库存缓存 - 考虑保质期、安全库存，按工厂分组，批量查询优化

        修复：不再因is_hold就完全排除库存记录。
        Hold库存应使用available_quantity（可用数量）而非quantity（在库数量），
        这样部分冻结的物料仍可参与齐套计算。
        """
        self.inventory_cache = defaultdict(list)
        self.factory_inventory_cache = defaultdict(lambda: defaultdict(list))
        # 使用 .only() 限制查询字段，减少数据库传输量
        inventories = Inventory.objects.select_related('material').all().only(
            'id', 'material_id', 'quantity', 'available_quantity', 'hold_quantity',
            'inventory_type', 'expiry_date',
            'warehouse', 'batch_no', 'factory_code', 'created_at', 'is_hold', 'hold_until'
        )
        for inv in inventories:
            # 已过期的hold视为已释放
            if inv.is_hold and inv.hold_until and inv.hold_until < date.today():
                pass  # hold已过期，按正常库存处理

            material_info = self.material_info_cache.get(inv.material_id, {})
            safety_stock = material_info.get('safety_stock', 0)
            factory_code = getattr(inv, 'factory_code', None) or 'default'

            # Hold未过期时，使用available_quantity作为可分配量
            is_held = inv.is_hold and inv.hold_until and inv.hold_until >= date.today()
            if is_held:
                # 使用可用数量（= 在库数量 - hold数量 - 锁定数量）
                allocatable_qty = int(inv.available_quantity or 0)
                if allocatable_qty <= 0:
                    continue  # 可用量为0才跳过，有可用量的hold记录仍参与分配
            else:
                allocatable_qty = int(inv.quantity or 0)
                if allocatable_qty <= 0:
                    continue

            entry = {
                'id': inv.id,
                'quantity': allocatable_qty,  # 使用实际可分配量
                'original_quantity': int(inv.quantity or 0),  # 保留原始在库数量供参考
                'type': inv.inventory_type,
                'expiry_date': inv.expiry_date,
                'warehouse': inv.warehouse,
                'batch_no': inv.batch_no,
                'factory_code': factory_code,
                'created_at': inv.created_at if hasattr(inv, 'created_at') else None,
                'is_safety_stock': False,
                'safety_stock_required': safety_stock,
                'is_held': is_held,  # 标记此条目来自hold记录
            }
            self.inventory_cache[inv.material_id].append(entry)
            # 按工厂分组存储
            self.factory_inventory_cache[factory_code][inv.material_id].append(entry)

        # 批量加载供应商承诺数据
        commitments = SupplierCommitment.objects.select_related('material', 'supplier').all().only(
            'id', 'material_id', 'supplier_id', 'quantity', 'delivery_date', 'order_no'
        )
        for comm in commitments:
            supplier_info = self.supplier_info_cache.get(comm.supplier_id, {})
            reliability_factor = supplier_info.get('delivery_reliability', 0.9)
            
            supplier_entry = {
                'id': comm.id,
                'quantity': round(float(comm.quantity or 0) * reliability_factor, 2),
                'original_quantity': int(comm.quantity or 0),
                'type': 'supplier',
                'expiry_date': comm.delivery_date,
                'delivery_date': comm.delivery_date,
                'supplier_id': comm.supplier_id,
                'supplier_rating': supplier_info.get('rating', 'B'),
                'reliability_factor': reliability_factor,
                'order_no': comm.order_no,
                'factory_code': 'supplier'
            }
            self.inventory_cache[comm.material_id].append(supplier_entry)
            self.factory_inventory_cache['supplier'][comm.material_id].append(supplier_entry)

    def load_bom_cache(self, factory_code=None):
        """加载BOM缓存 - 考虑报废率，支持按工厂过滤、ECN生效日期，批量加载优化"""
        self.bom_cache = defaultdict(list)
        self.alternative_cache = defaultdict(list)

        # 使用 .only() 限制查询字段，减少数据库传输量
        boms = BillOfMaterials.objects.select_related('parent_material', 'child_material').filter(is_active=True).only(
            'id', 'parent_material_id', 'child_material_id', 'quantity', 'unit', 'bom_level',
            'alternative_group', 'alternative_priority', 'alternative_ratio', 'factory_code',
            'scrap_rate', 'usage_ratio', 'is_configurable', 'config_group', 'config_options', 'ecn_date'
        )
        
        # 按工厂过滤BOM
        if factory_code:
            boms = boms.filter(
                Q(factory_code=factory_code) | Q(factory_code__isnull=True) | Q(factory_code='')
            )
        
        today = date.today()
        
        for bom in boms:
            # ECN生效日期检查：如果BOM有ECN日期，且当前日期早于ECN日期，则跳过（使用旧版本）
            ecn_date = getattr(bom, 'ecn_date', None)
            if ecn_date and ecn_date > today:
                continue  # ECN尚未生效，跳过此BOM
            # 考虑报废率计算实际需求
            scrap_rate = bom.scrap_rate if hasattr(bom, 'scrap_rate') else 0.0
            adjusted_quantity = float(bom.quantity or 0) * (1 + scrap_rate)
            
            self.bom_cache[bom.parent_material_id].append({
                'id': bom.id,
                'child_id': bom.child_material_id,
                'quantity': adjusted_quantity,
                'original_quantity': float(bom.quantity or 0),
                'scrap_rate': scrap_rate,
                'unit': bom.unit,
                'bom_level': bom.bom_level,
                'alternative_group': bom.alternative_group,
                'alternative_priority': bom.alternative_priority,
                'alternative_ratio': bom.alternative_ratio,
                'factory_code': bom.factory_code,
                'usage_ratio': bom.usage_ratio if hasattr(bom, 'usage_ratio') else 0.0,
                'is_configurable': bom.is_configurable if hasattr(bom, 'is_configurable') else False,
                'config_group': bom.config_group if hasattr(bom, 'config_group') else None,
                'config_options': bom.config_options if hasattr(bom, 'config_options') else None
            })

            # 替代料缓存：解析逗号分隔的物料代码，归一化组key
            if bom.alternative_group:
                alt_codes = [c.strip() for c in str(bom.alternative_group).split(',') if c.strip()]
                if alt_codes:
                    # 归一化组key：将当前物料自身也加入组中，排序后作为唯一标识
                    all_member_codes = sorted(set(alt_codes + [str(bom.child_material_id)]))
                    group_key = ','.join(all_member_codes)
                    self.alternative_cache[group_key].append({
                        'material_id': bom.child_material_id,
                        'parent_id': bom.parent_material_id,
                        'priority': bom.alternative_priority,
                        'ratio': bom.alternative_ratio,
                        'bom_quantity': adjusted_quantity,
                        'scrap_rate': scrap_rate,
                        'factory_code': bom.factory_code
                    })

        for group in self.alternative_cache:
            self.alternative_cache[group] = sorted(
                self.alternative_cache[group],
                key=lambda x: x['priority']
            )

        # L2优化: BOM加载完成后自动构建DAG缓存
        try:
            self._build_bom_dag()
        except Exception as e:
            logger.warning(f'BOM DAG构建失败(非致命，将回退到递归展开): {e}')
            self._bom_dag_built = False

    def load_priority_rule(self):
        """加载优先级规则配置"""
        self.priority_rule = PriorityRule.objects.filter(is_active=True).first()

    def find_cross_factory_inventory(self, material_id, required_qty, order_factory_code, required_date=None):
        """查找其他工厂的可用库存，用于跨工厂调拨决策
        
        返回可调拨的工厂列表，按调拨可行性排序（考虑距离/耗时）
        """
        transfer_options = []
        
        for factory_code, materials in self.factory_inventory_cache.items():
            # 跳过当前工厂和供应商库存
            if factory_code == order_factory_code or factory_code == 'supplier':
                continue
            
            factory_available = 0
            inventory_items = materials.get(material_id, [])
            for inv in inventory_items:
                if 'delivery_date' in inv and required_date:
                    if inv['delivery_date'] > required_date:
                        continue
                factory_available += inv['quantity']
            
            if factory_available <= 0:
                continue
            
            can_transfer_qty = min(factory_available, required_qty)
            transfer_options.append({
                'from_factory': factory_code,
                'available_qty': factory_available,
                'transferable_qty': can_transfer_qty,
                'transfer_days': self.DEFAULT_TRANSFER_DAYS,
                'transfer_cost': can_transfer_qty * self.DEFAULT_TRANSFER_COST_PER_UNIT,
            })
        
        # 按可调拨数量降序排列（优先选择库存充足的工厂）
        transfer_options.sort(key=lambda x: x['transferable_qty'], reverse=True)
        return transfer_options

    def execute_factory_transfer(self, material_id, required_qty, order_factory_code, order_id, required_date=None):
        """执行跨工厂调拨 - 从其他工厂调拨物料到订单所属工厂
        
        返回: (已调拨数量, 调拨记录列表)
        """
        transferred = 0
        transfer_recs = []
        
        transfer_options = self.find_cross_factory_inventory(
            material_id, required_qty, order_factory_code, required_date
        )
        
        for option in transfer_options:
            if transferred >= required_qty:
                break
            
            from_factory = option['from_factory']
            qty_to_transfer = min(option['transferable_qty'], required_qty - transferred)
            
            if qty_to_transfer <= 0:
                break
            
            # 检查调拨时间是否可行（调拨耗时不能超过交期限制）
            if required_date:
                transfer_arrival = date.today() + timedelta(days=option['transfer_days'])
                if transfer_arrival > required_date:
                    continue
            
            # 从源工厂扣减库存
            remaining = qty_to_transfer
            factory_inv_list = self.factory_inventory_cache[from_factory].get(material_id, [])
            for inv in factory_inv_list:
                if remaining <= 0:
                    break
                if inv['quantity'] <= 0:
                    continue
                take = min(inv['quantity'], remaining)
                inv['quantity'] -= take
                remaining -= take
            
            # 增加到目标工厂
            target_entry = {
                'id': f'transfer_{from_factory}_{material_id}_{len(self.transfer_records)}',
                'quantity': qty_to_transfer,
                'type': 'transfer',
                'factory_code': order_factory_code,
                'from_factory': from_factory,
                'transfer_days': option['transfer_days'],
                'is_safety_stock': False,
                'safety_stock_required': 0
            }
            self.factory_inventory_cache[order_factory_code][material_id].append(target_entry)
            # 同步到全局库存缓存
            self.inventory_cache[material_id].append(target_entry)
            
            transferred += qty_to_transfer
            
            transfer_rec = {
                'material_id': material_id,
                'from_factory': from_factory,
                'to_factory': order_factory_code,
                'quantity': qty_to_transfer,
                'transfer_days': option['transfer_days'],
                'transfer_cost': option['transfer_cost'],
                'order_id': order_id,
                'reason': f'订单{order_id}物料不足，从{from_factory}调拨'
            }
            transfer_recs.append(transfer_rec)
            self.transfer_records.append(transfer_rec)
            
            PlanLog.objects.create(
                log_type='INFO',
                message=f'工厂调拨: 物料{material_id}从{from_factory}调拨{qty_to_transfer}到{order_factory_code}'
            )
        
        return transferred, transfer_recs

    def calculate_effective_required_date(self, order):
        """计算有效需求日期 - 支持提前交货
        
        如果订单允许提前交货，则在最早可交货日期和需求日期之间选择最优日期
        """
        # 默认需求日期 = 交期 - 运输天数 - 生产提前期
        shipping_days = self._get_effective_shipping_days(order)
        standard_required_date = order.demand_date - timedelta(days=shipping_days + self.PRODUCTION_LEAD_TIME)
        
        # 如果不允许提前交货，直接返回标准日期
        if not getattr(order, 'allow_early_delivery', False):
            return standard_required_date, False
        
        # 如果有最早可交货日期限制
        earliest_date = getattr(order, 'earliest_delivery_date', None)
        if earliest_date:
            earliest_required = earliest_date - timedelta(days=shipping_days + self.PRODUCTION_LEAD_TIME)
            # 取两者中较早的日期（更宽松的约束）
            effective_date = min(standard_required_date, earliest_required)
        else:
            # 没有最早交货限制，可以更早安排生产
            effective_date = standard_required_date
        
        # 提前1天交货的优化：如果标准日期有产能冲突，尝试提前1天
        today = date.today()
        if effective_date > today:
            # 尝试提前1天看是否有更好的产能匹配
            early_date = effective_date - timedelta(days=1)
            if early_date >= today:
                return early_date, True  # 标记为提前交货
        
        return effective_date, False

    def sort_inventory_by_priority(self, inventory_list, order_context=None):
        """根据配置的优先级策略对库存进行排序

        L3优化: 新增 VALUE_AWARE 模式（价值驱动分配）
        核心思想: 不再简单按时间(FIFO)分配，而是综合评估每条库存的"价值贡献"
        价值评分 = 价值贡献 + 稀缺性加成 - 风险惩罚 - 成本惩罚

        价值驱动分配让系统做到:
        - 高价值订单优先获得优质库存
        - 稀缺物料优先分配给最需要的订单
        - 高风险库存(临期/低可靠性)被合理规避

        Args:
            inventory_list: 库存记录列表
            order_context: 当前订单上下文(可选，VALUE_AWARE模式使用)
                         包含: order_id, material_id, urgency, priority等
        """
        from django.utils.timezone import now as tz_now
        _far_future = tz_now().replace(year=2099, month=12, day=31)
        _far_past = tz_now().replace(year=2000, month=1, day=1)
        today = date.today()

        # L3优化: Value-aware 模式
        if self.consumption_priority == 'VALUE_AWARE':
            return self._sort_inventory_value_aware(inventory_list, order_context)

        if self.consumption_priority == 'FIFO':
            return sorted(inventory_list, key=lambda x: (
                x.get('created_at') or _far_future,
                {'local': 0, 'finished': 1, 'semi': 2, 'transit': 3, 'supplier': 4}.get(x['type'], 99)
            ))
        elif self.consumption_priority == 'LIFO':
            return sorted(inventory_list, key=lambda x: (
                x.get('created_at') or _far_past,
                {'local': 0, 'finished': 1, 'semi': 2, 'transit': 3, 'supplier': 4}.get(x['type'], 99)
            ), reverse=True)
        elif self.consumption_priority == 'INVENTORY_FIRST':
            # 优先消耗本地库存，再按时间排序（FIFO）
            # 修复: 处理offset-naive和offset-aware datetime混用导致的TypeError
            def _inv_sort_key(x):
                created = x.get('created_at')
                if created is None:
                    return 0, datetime.max
                # 统一转为naive datetime比较
                if hasattr(created, 'tzinfo') and created.tzinfo is not None:
                    import pytz
                    try:
                        created = created.astimezone(pytz.utc).replace(tzinfo=None)
                    except Exception:
                        pass
                return 0, created or datetime.max
            return sorted(inventory_list, key=lambda x: (
                {'local': 0, 'finished': 1, 'semi': 2, 'transit': 3, 'supplier': 4}.get(x.get('type'), 99),
                _inv_sort_key(x)[1]
            ))
        elif self.consumption_priority == 'SUPPLIER_FIRST':
            # 优先使用供应商库存，按可靠性降序，再按时间升序
            return sorted(inventory_list, key=lambda x: (
                {'supplier': 0, 'transit': 1, 'local': 2, 'finished': 3, 'semi': 4}.get(x['type'], 99),
                -x.get('reliability_factor', 1.0),  # 可靠性高的优先
                x.get('created_at') or _far_future
            ))
        elif self.consumption_priority == 'EXPIRY_FIRST':
            return sorted(inventory_list, key=lambda x: (
                x.get('expiry_date') or date(2099, 12, 31),
                {'local': 0, 'finished': 1, 'semi': 2, 'transit': 3, 'supplier': 4}.get(x['type'], 99)
            ))
        else:
            return sorted(inventory_list, key=lambda x: (
                {'local': 0, 'finished': 1, 'semi': 2, 'transit': 3, 'supplier': 4}.get(x['type'], 99),
                x.get('created_at') or _far_future
            ))

    def _sort_inventory_value_aware(self, inventory_list, order_context=None):
        """L3优化: 价值驱动库存排序

        对每条库存记录计算综合价值分数，按分数降序排列。
        分数越高 → 越优先分配给当前订单。

        价值评分公式:
            score = w1*value_contribution     # 价值贡献(供应商评级/物料重要性)
                  + w2*scarcity_bonus          # 稀缺性加成(该物料整体库存紧张度)
                  - w3*risk_penalty            # 风险惩罚(临期/低可靠性/Hold)
                  - w4*cost_penalty            # 成本惩罚(调拨成本/加急费用)
                  + w5*locality_bonus          # 本地优先(同工厂/同仓库)

        权重配置 (可通过策略参数调整):
            w1=0.30, w2=0.25, w3=0.20, w4=0.15, w5=0.10
        """
        scored_list = []
        today = date.today()

        for inv in inventory_list:
            score = 0.0
            score_details = {}

            # ---- 维度1: 价值贡献 (w1=0.30) ----
            supplier_rating = inv.get('supplier_rating', 'C')
            rating_map = {'A': 1.0, 'B': 0.75, 'C': 0.5, 'D': 0.25}
            value_score = rating_map.get(supplier_rating, 0.5)
            reliability = inv.get('reliability_factor', 1.0)
            value_score = value_score * 0.6 + reliability * 0.4
            score += value_score * 0.30
            score_details['value'] = value_score

            # ---- 维度2: 稀缺性加成 (w2=0.25) ----
            material_id = inv.get('material_id')
            total_for_material = 0
            if material_id and material_id in self.inventory_cache:
                total_for_material = sum(i.get('quantity', 0) for i in self.inventory_cache[material_id])
            inv_qty = inv.get('quantity', 0)
            if total_for_material > 0:
                scarcity_ratio = inv_qty / total_for_material
                scarcity_score = 1.0 - min(scarcity_ratio, 1.0)
            else:
                scarcity_score = 0.5
            score += scarcity_score * 0.25
            score_details['scarcity'] = scarcity_score

            # ---- 维度3: 风险惩罚 (w3=0.20) ----
            risk_penalty = 0.0
            expiry_date = inv.get('expiry_date')
            if expiry_date:
                try:
                    days_to_expiry = (expiry_date - today).days
                    if days_to_expiry < 0:
                        risk_penalty += 0.8
                    elif days_to_expiry < 7:
                        risk_penalty += 0.5
                    elif days_to_expiry < 30:
                        risk_penalty += 0.2
                except (TypeError, ValueError):
                    pass
            if reliability < 0.7:
                risk_penalty += (0.7 - reliability) * 0.5
            if inv.get('is_held', False):
                risk_penalty += 0.3
            score -= risk_penalty * 0.20
            score_details['risk_penalty'] = risk_penalty

            # ---- 维度4: 成本惩罚 (w4=0.15) ----
            cost_penalty = 0.0
            inv_type = inv.get('type', 'local')
            type_cost_map = {'local': 0.0, 'finished': 0.0, 'semi': 0.05,
                            'transit': 0.15, 'supplier': 0.20, 'transfer': 0.25}
            cost_penalty += type_cost_map.get(inv_type, 0.1)
            if inv_type == 'transfer':
                cost_penalty += 0.1
            score -= cost_penalty * 0.15
            score_details['cost_penalty'] = cost_penalty

            # ---- 维度5: 本地优先 (w5=0.10) ----
            locality_bonus = 0.0
            if order_context:
                order_factory = order_context.get('factory_code')
                inv_factory = inv.get('factory_code')
                if order_factory and inv_factory and order_factory == inv_factory:
                    locality_bonus = 1.0
                elif inv_type in ('local', 'finished'):
                    locality_bonus = 0.7
                elif inv_type == 'semi':
                    locality_bonus = 0.4
                else:
                    locality_bonus = 0.1
            else:
                type_locality = {'local': 1.0, 'finished': 0.9, 'semi': 0.6,
                                'transit': 0.3, 'supplier': 0.2, 'transfer': 0.15}
                locality_bonus = type_locality.get(inv_type, 0.1)

            score += locality_bonus * 0.10
            score_details['locality'] = locality_bonus

            inv['_value_score'] = round(score, 4)
            inv['_value_details'] = score_details
            scored_list.append((score, inv))

        scored_list.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored_list]

    def check_expiry_urgency(self, inventory_item, required_date):
        """检查库存过期紧迫性"""
        expiry_date = inventory_item.get('expiry_date')
        if not expiry_date:
            return 0
        
        days_until_expiry = (expiry_date - required_date).days
        
        if days_until_expiry < 7:
            return 3
        elif days_until_expiry < 30:
            return 2
        elif days_until_expiry < 90:
            return 1
        return 0

    def get_bom_requirements(self, material_id, quantity=1, visited=None, config_options=None):
        """获取物料的BOM需求（展开多层BOM）- 支持CTO配置

        L2优化: 优先使用DAG缓存，命中时直接返回，避免递归展开
        未命中时回退到传统递归展开（向后兼容）
        """
        # ===== L2优化: 尝试从DAG缓存获取 =====
        if self._bom_dag_built and material_id in self._bom_dag_cache:
            self._bom_dag_stats['hits'] += 1
            dag_result = self._bom_dag_cache[material_id]
            requirements = {}
            for child_id, child_info in dag_result.items():
                base_qty = child_info['quantity'] * quantity
                requirements[child_id] = {
                    'quantity': base_qty,
                    'scrap_rate': child_info.get('scrap_rate', 0.0)
                }
            return requirements

        # ===== 回退: 传统递归展开（DAG未构建或缓存未命中）=====
        self._bom_dag_stats['misses'] += 1
        if visited is None:
            visited = set()
        requirements = defaultdict(lambda: {'quantity': 0, 'parent_path': [], 'scrap_rate': 0.0})
        self._explode_bom(material_id, quantity, [], requirements, visited, config_options)
        return {k: {'quantity': v['quantity'], 'scrap_rate': v['scrap_rate']} for k, v in requirements.items()}

    def _build_bom_dag(self):
        """L2优化: 构建BOM有向无环图(DAG)缓存

        将多层BOM结构预展平为扁平的 {父件→{子件: 需求系数}} 映射。
        相比递归展开的优势：
        - 每个物料只计算一次完整子树（即使被多个父件引用）
        - 后续查询O(1)直接返回，无需重复遍历
        - 特别适合电子制造业中大量共享子件的场景

        构建算法（拓扑排序+广度优先）：
          1. 从所有根节点(无父件的成品)开始
          2. BFS逐层展开，累计每层的用量系数和报废率
          3. 缓存每个节点的完整子件清单
          4. 检测并处理循环引用（同原递归逻辑）
        """
        if not self.bom_cache:
            return

        self._bom_dag_cache = {}
        visited_global = set()

        # 找出所有根节点：作为parent_material_id出现但不出现在任何child_material_id中的物料
        all_children = set()
        all_parents = set()
        for parent_id, children in self.bom_cache.items():
            all_parents.add(parent_id)
            for child in children:
                all_children.add(child['child_id'])

        root_materials = all_parents - all_children
        # 如果没有明确的根节点（如循环引用或扁平结构），将所有parent都视为潜在根
        if not root_materials:
            root_materials = set(self.bom_cache.keys())

        # BFS构建DAG
        from collections import deque
        queue = deque(root_materials)

        while queue:
            current_id = queue.popleft()

            if current_id in visited_global:
                continue
            visited_global.add(current_id)

            if current_id not in self.bom_cache:
                # 叶子节点：自身就是最终物料
                self._bom_dag_cache[current_id] = {}
                continue

            # 展开当前节点的一层子件
            children_map = {}  # {child_id: {quantity_coefficient, scrap_rate}}
            for bom_entry in self.bom_cache[current_id]:
                child_id = bom_entry['child_id']
                qty_coeff = bom_entry['quantity']  # 已包含报废补偿
                scrap = bom_entry.get('scrap_rate', 0.0)

                if child_id not in children_map:
                    children_map[child_id] = {
                        'quantity': qty_coeff,
                        'scrap_rate': scrap,
                        'direct': True
                    }
                else:
                    # 同一父件下同一子件可能有多条BOM记录（不同配置），取最大用量
                    if qty_coeff > children_map[child_id]['quantity']:
                        children_map[child_id]['quantity'] = qty_coeff
                    children_map[child_id]['scrap_rate'] = max(children_map[child_id]['scrap_rate'], scrap)

                # 将子件加入队列继续展开
                if child_id not in visited_global and child_id in self.bom_cache:
                    queue.append(child_id)

            # 递归合并子件的子树（将孙子件提升到当前层级）
            flattened_children = {}
            for child_id, child_info in children_map.items():
                coeff = child_info['quantity']
                scrap = child_info['scrap_rate']

                # 如果该子件在DAG缓存中有自己的子树，则合并
                if child_id in self._bom_dag_cache:
                    for grandchild_id, grandchild_info in self._bom_dag_cache[child_id].items():
                        combined_qty = coeff * grandchild_info['quantity']
                        combined_scrap = max(scrap, grandchild_info.get('scrap_rate', 0.0))

                        if grandchild_id not in flattened_children:
                            flattened_children[grandchild_id] = {
                                'quantity': combined_qty,
                                'scrap_rate': combined_scrap
                            }
                        else:
                            # 多路径到达同一叶子，累加数量
                            flattened_children[grandchild_id]['quantity'] += combined_qty
                            flattened_children[grandchild_id]['scrap_rate'] = max(
                                flattened_children[grandchild_id]['scrap_rate'], combined_scrap
                            )
                else:
                    # 该子件没有进一步的子树（或尚未展开），直接作为叶子
                    if child_id not in flattened_children:
                        flattened_children[child_id] = {
                            'quantity': coeff,
                            'scrap_rate': scrap
                        }
                    else:
                        flattened_children[child_id]['quantity'] += coeff
                        flattened_children[child_id]['scrap_rate'] = max(
                            flattened_children[child_id]['scrap_rate'], scrap
                        )

            self._bom_dag_cache[current_id] = flattened_children

        self._bom_dag_built = True
        self._bom_dag_stats['build_count'] += 1
        logger.info(f'BOM DAG缓存构建完成: 覆盖{len(self._bom_dag_cache)}个物料节点')

    def _invalidate_bom_dag(self):
        """使BOM DAG缓存失效（在BOM数据变更时调用）"""
        self._bom_dag_cache = {}
        self._bom_dag_built = False

    def _explode_bom(self, material_id, quantity, parent_path, requirements, visited, config_options=None):
        """递归展开BOM - 考虑报废率累计，支持CTO配置"""
        if material_id in visited:
            return
        visited.add(material_id)

        current_path = parent_path + [material_id]

        if material_id not in self.bom_cache:
            if material_id not in requirements:
                requirements[material_id] = {'quantity': 0, 'parent_path': current_path, 'scrap_rate': 0.0}
            requirements[material_id]['quantity'] += quantity
            return

        for bom in self.bom_cache[material_id]:
            # CTO配置过滤：如果BOM是可配置的，检查是否匹配订单配置
            if bom.get('is_configurable') and config_options:
                config_group = bom.get('config_group')
                config_value = config_options.get(config_group)
                if config_value and bom.get('config_options'):
                    # 解析BOM配置选项
                    bom_config = bom['config_options']
                    valid_options = []
                    
                    if isinstance(bom_config, dict):
                        valid_options = bom_config.get('options', [])
                        # 支持嵌套格式: {"options": [...], "default": "..."}
                        if not valid_options and isinstance(bom_config.get('options'), list):
                            valid_options = bom_config['options']
                    elif isinstance(bom_config, list):
                        valid_options = bom_config
                    
                    # 如果配置了有效选项列表且当前值不在其中，跳过
                    if valid_options and config_value not in valid_options:
                        continue  # 配置不匹配，跳过此BOM子件
            elif bom.get('is_configurable') and not config_options:
                # 可配置BOM但没有提供配置选项 → 使用默认值或跳过
                bom_config = bom.get('config_options')
                has_default = False
                if isinstance(bom_config, dict) and bom_config.get('default'):
                    has_default = True
                if not has_default:
                    continue  # 无配置无默认值，跳过可配置项
            
            child_qty = quantity * bom['quantity']
            if bom['child_id'] not in requirements:
                requirements[bom['child_id']] = {'quantity': 0, 'parent_path': current_path + [bom['child_id']], 'scrap_rate': bom.get('scrap_rate', 0.0)}
            requirements[bom['child_id']]['quantity'] += child_qty
            requirements[bom['child_id']]['scrap_rate'] = max(requirements[bom['child_id']]['scrap_rate'], bom.get('scrap_rate', 0.0))

            self._explode_bom(bom['child_id'], child_qty, current_path, requirements, visited, config_options)

    def get_available_inventory(self, material_id, required_date=None, consider_safety_stock=True):
        """获取可用库存 - 可选择是否考虑安全库存"""
        total = 0.0
        if material_id not in self.inventory_cache:
            return total
        
        material_info = self.material_info_cache.get(material_id, {})
        safety_stock = material_info.get('safety_stock', 0) if consider_safety_stock else 0
        safety_allocated = 0.0
        
        for inv in self.inventory_cache[material_id]:
            qty = inv['quantity']
            
            if 'delivery_date' in inv and required_date:
                if inv['delivery_date'] > required_date:
                    continue
            
            if inv.get('is_safety_stock', False):
                safety_allocated += qty
                if safety_allocated <= safety_stock:
                    continue
            
            total += qty
        
        return total

    def is_inventory_available(self, inventory_entry, required_date=None):
        """检查库存条目是否可用（统一Hold过滤）"""
        # 如果有hold_until且未到期，则不可用
        hold_until = inventory_entry.get('hold_until')
        if hold_until:
            try:
                from datetime import date as _date
                if isinstance(hold_until, str):
                    hold_until = datetime.strptime(hold_until, '%Y-%m-%d').date()
                if hold_until >= _date.today():
                    return False
            except (ValueError, TypeError):
                pass
        # 数量必须大于0
        if inventory_entry.get('quantity', 0) <= 0:
            return False
        return True

    def allocate_inventory(self, material_id, required_qty, order_id, required_date=None):
        """分配库存 - 考虑保质期、安全库存和特殊消耗规则"""
        allocated = 0.0
        allocations = []

        if material_id not in self.inventory_cache:
            return allocated, allocations

        material_info = self.material_info_cache.get(material_id, {})
        safety_stock = material_info.get('safety_stock', 0)
        today = date.today()

        # ===== 加载并应用特殊消耗规则 =====
        # 构建当前分配上下文，用于规则匹配
        order_context = self._build_allocation_context(order_id, material_id)
        active_rules = self._get_applicable_consumption_rules(order_context)

        # L3优化: 传递订单上下文用于Value-aware排序
        inventory_list = self.sort_inventory_by_priority(self.inventory_cache[material_id], order_context=order_context)

        # 预计算库存总量（避免在循环中重复计算）
        total_inv_for_material = sum(i['quantity'] for i in self.inventory_cache[material_id])

        for inv in inventory_list:
            if allocated >= required_qty:
                break

            if 'delivery_date' in inv and required_date:
                if inv['delivery_date'] > required_date:
                    continue

            # P2修复：统一Hold物料过滤
            if not self.is_inventory_available(inv, required_date):
                continue

            # ===== 特殊消耗规则检查 =====
            # 基于匹配的规则过滤/调整库存记录
            rule_filter_result = self._apply_consumption_rules_to_inventory(
                inv, order_context, active_rules, required_qty - allocated, safety_stock,
                total_inv_for_material - allocated
            )
            if rule_filter_result is None:  # 规则明确排除此库存
                continue
            elif isinstance(rule_filter_result, dict):
                # 规则调整了可用数量或属性
                # 注意：需要同步更新原始库存缓存中的对象
                original_inv = None
                for orig in self.inventory_cache[material_id]:
                    if orig.get('id') == inv.get('id'):
                        original_inv = orig
                        break
                if original_inv:
                    # 更新原始库存缓存
                    for k, v in rule_filter_result.items():
                        original_inv[k] = v
                    inv = original_inv  # 使用原始对象，确保后续修改会影响缓存
                else:
                    inv = {**inv, **rule_filter_result}

            qty_available = inv['quantity']

            # 安全库存保护（可被特殊规则覆盖）
            safety_override = self._get_safety_stock_override(active_rules)
            if not inv.get('is_safety_stock', False) and safety_stock > 0:
                effective_safety = safety_stock * (1 - safety_override)  # override比例越高，保护越弱
                if total_inv_for_material - allocated - qty_available < effective_safety:
                    protect_qty = max(0, (total_inv_for_material - allocated) - effective_safety)
                    qty_available = min(qty_available, protect_qty)

            if qty_available <= 0:
                continue

            # 应用最大分配占比限制（来自特殊规则）
            max_alloc_pct = self._get_max_allocation_pct(active_rules)
            if max_alloc_pct and max_alloc_pct < 1.0:
                qty_available = min(qty_available, (required_qty - allocated) * max_alloc_pct / (1 - max_alloc_pct + 0.001))

            # ========== 资源集中模式 ==========
            # 不再限制单订单分配上限，允许高优先级订单拿走足够多的物料以完成订单
            # 核心目标：集中资源完成尽可能多的完整订单，而非让所有订单都"半饱"
            qty_to_allocate = min(qty_available, required_qty - allocated)

            with self.lock:
                allocations.append({
                    'inventory_id': inv['id'],
                    'material_id': material_id,
                    'quantity': qty_to_allocate,
                    'type': inv['type'],
                    'order_id': order_id,
                    'expiry_date': inv.get('expiry_date'),
                    'supplier_rating': inv.get('supplier_rating'),
                    'reliability_factor': inv.get('reliability_factor', 1.0),
                    'is_expiring_soon': self.check_expiry_urgency(inv, required_date) > 1,
                    'consumption_rules_applied': [r.name for r in active_rules] if active_rules else [],
                })

                allocated += qty_to_allocate
                inv['quantity'] -= qty_to_allocate

                self.allocation_history.append({
                    'material_id': material_id,
                    'quantity': qty_to_allocate,
                    'order_id': order_id,
                    'inventory_id': inv.get('id'),  # 关联库存记录ID（用于订单取消时精确释放）
                    'type': inv['type'],
                    'timestamp': datetime.now(),
                    'expiry_date': inv.get('expiry_date'),
                    'reliability_factor': inv.get('reliability_factor', 1.0),
                    'priority': 99,
                    'rules_applied': len(active_rules) if active_rules else 0,
                })

                if inv['quantity'] <= 0:
                    try:
                        self.inventory_cache[material_id].remove(inv)
                    except ValueError:
                        pass  # 已被其他路径移除，忽略

            # ========== 策略差异化紧急补货 ==========
            # 如果普通库存不够，尝试从紧急补货池分配（策略决定补货池大小）
            shortage = required_qty - allocated
            if shortage > 0 and self.emergency_inventory_pool > 0:
                with self.lock:
                    emergency_available = min(
                        self.emergency_inventory_pool - self.emergency_used,
                        shortage
                    )
                    if emergency_available > 0:
                        allocations.append({
                            'inventory_id': 0,
                            'material_id': material_id,
                            'quantity': emergency_available,
                            'type': 'emergency',
                            'order_id': order_id,
                            'expiry_date': None,
                            'supplier_rating': None,
                            'reliability_factor': 0.9,
                            'is_expiring_soon': False,
                            'consumption_rules_applied': [],
                        })
                        allocated += emergency_available
                        self.emergency_used += emergency_available

        return allocated, allocations

    def calculate_alternative_allocation(self, alternative_group, required_qty, order_id, required_date=None):
        """计算替代料分配 - 按BOM比例约束，支持比例补偿机制"""
        if alternative_group not in self.alternative_cache:
            return 0.0, []

        alternatives = self.alternative_cache[alternative_group]
        
        # 动态调整替代料优先级（根据实时库存）
        alternatives = self._adjust_alternative_priority_by_inventory(alternatives)
        
        total_ratio = sum(a['ratio'] for a in alternatives)

        allocated = 0.0
        allocations = []
        ratio_adjusted_demand = {}

        # ===== L3优化: 多因子评分模型（替代固定比例分配）=====
        # 对每个替代料计算综合评分，评分高的优先获得更多分配比例
        scored_alternatives = []
        for alt in alternatives:
            alt_mid = alt['material_id']

            # 因子1: 库存充足率 (0~1, 越高越好)
            inv_available = self.get_available_inventory(alt_mid, required_date)
            total_demand_est = required_qty * alt.get('bom_quantity', 1.0)
            inventory_factor = min(1.0, inv_available / max(total_demand_est, 1))

            # 因子2: 成本优势 (归一化, 越低越好→转越高分)
            cost_factor = 1.0  # 默认中性
            material_info = self.material_info_cache.get(alt_mid, {})
            std_cost = material_info.get('standard_cost', 0) or 0
            if std_cost > 0:
                # 相对成本：与组内其他替代料比较
                cost_factor = max(0.1, 1.0 - min(std_cost / 10000, 0.9))  # 简化处理

            # 因子3: 良率/报废率 (0~1, 报废率越低越好)
            scrap_rate = alt.get('scrap_rate', 0.0) or 0.0
            yield_factor = max(0.1, 1.0 - scrap_rate)

            # 因子4: 供应商可靠性 (0~1)
            supplier_info = self.supplier_info_cache.get(alt_mid, {})
            reliability = supplier_info.get('delivery_reliability', 0.8)
            reliability_factor = reliability

            # 因子5: 交期风险 (0~1, 风险越低越好)
            delivery_risk = 0.8  # 默认较低风险
            if 'delivery_date' not in str(type(required_date)):
                # 检查该物料是否有即将到期的库存
                if alt_mid in self.inventory_cache:
                    for inv_rec in self.inventory_cache[alt_mid]:
                        inv_expiry = inv_rec.get('expiry_date')
                        if inv_expiry and required_date:
                            try:
                                days_gap = (inv_expiry - required_date).days
                                if days_gap < 0:
                                    delivery_risk = 0.2  # 已过期
                                elif days_gap < 7:
                                    delivery_risk = 0.4
                            except (TypeError, ValueError):
                                pass

            # 综合评分 (加权求和)
            # 权重: 库存0.30 + 成本0.15 + 良率0.20 + 可靠性0.25 + 交期0.10
            total_score = (
                inventory_factor * 0.30 +
                cost_factor * 0.15 +
                yield_factor * 0.20 +
                reliability_factor * 0.25 +
                delivery_risk * 0.10
            )

            scored_alternatives.append({
                **alt,
                '_multi_factor_score': round(total_score, 4),
                '_inventory_factor': inventory_factor,
                '_reliability_factor': reliability_factor,
                '_yield_factor': yield_factor,
                '_available_qty': inv_available,
            })

        # 按综合评分降序排列（评分最高的替代料优先）
        scored_alternatives.sort(key=lambda x: x['_multi_factor_score'], reverse=True)

        # 基于评分重新计算分配比例（而非固定ratio）
        total_score_sum = sum(a['_multi_factor_score'] for a in scored_alternatives)
        for alt in scored_alternatives:
            if total_score_sum > 0:
                score_ratio = alt['_multi_factor_score'] / total_score_sum
            else:
                score_ratio = 1.0 / len(scored_alternatives)

            # 混合策略: 60%基于评分 + 40%基于原始BOM比例（保持工程约束）
            base_ratio = alt['ratio'] / total_ratio if total_ratio > 0 else 1.0 / len(alternatives)
            blended_ratio = score_ratio * 0.6 + base_ratio * 0.4

            ratio_adjusted_demand[alt['material_id']] = {
                'ratio': blended_ratio,           # 使用混合后的比例
                'original_ratio': base_ratio,     # 保留原始BOM比例供参考
                'score_ratio': score_ratio,       # 保留评分比例供参考
                'original_qty': required_qty * blended_ratio,
                'bom_qty': alt['bom_quantity'],
                'target_qty': required_qty * blended_ratio * alt['bom_quantity'],
                'scrap_rate': alt.get('scrap_rate', 0.0),
                'multi_factor_score': alt['_multi_factor_score'],
            }

        # 按评分降序排列（评分高的优先分配）
        sorted_materials = sorted(ratio_adjusted_demand.items(),
            key=lambda x: x[1].get('multi_factor_score', 0),
            reverse=True
        )

        # 第一轮：按原始比例分配
        shortage_by_material = {}
        for material_id, demand_info in sorted_materials:
            if allocated >= required_qty:
                break

            available = self.get_available_inventory(material_id, required_date)
            qty_to_allocate = min(available, demand_info['target_qty'])

            if qty_to_allocate > 0:
                actual_allocated, inv_allocations = self.allocate_inventory(
                    material_id, qty_to_allocate, order_id, required_date
                )

                for alloc in inv_allocations:
                    alloc['is_alternative'] = True
                    alloc['alternative_group'] = alternative_group
                    alloc['ratio'] = demand_info['ratio']
                    alloc['scrap_rate'] = demand_info['scrap_rate']

                allocations.extend(inv_allocations)
                allocated += actual_allocated
                
                # 记录未满足的需求
                if actual_allocated < demand_info['target_qty']:
                    shortage_by_material[material_id] = demand_info['target_qty'] - actual_allocated

        # 第二轮：比例补偿 - 将未满足的需求按剩余比例分配给其他替代料
        if shortage_by_material and allocated < required_qty:
            remaining_need = required_qty - allocated
            # 计算有库存的替代料的剩余比例
            available_alternatives = []
            for material_id, demand_info in sorted_materials:
                if material_id not in shortage_by_material:
                    available = self.get_available_inventory(material_id, required_date)
                    if available > 0:
                        available_alternatives.append((material_id, demand_info, available))
            
            if available_alternatives:
                # 按剩余库存比例重新分配
                total_remaining_ratio = sum(d['ratio'] for _, d, _ in available_alternatives)
                for material_id, demand_info, available in available_alternatives:
                    if remaining_need <= 0:
                        break
                    compensation_ratio = demand_info['ratio'] / total_remaining_ratio if total_remaining_ratio > 0 else 1.0 / len(available_alternatives)
                    compensation_qty = min(remaining_need * compensation_ratio, available)
                    
                    if compensation_qty > 0:
                        actual_allocated, inv_allocations = self.allocate_inventory(
                            material_id, compensation_qty, order_id, required_date
                        )
                        for alloc in inv_allocations:
                            alloc['is_alternative'] = True
                            alloc['alternative_group'] = alternative_group
                            alloc['ratio'] = demand_info['ratio']
                            alloc['scrap_rate'] = demand_info['scrap_rate']
                            alloc['is_compensation'] = True  # 标记为补偿分配
                        
                        allocations.extend(inv_allocations)
                        allocated += actual_allocated
                        remaining_need -= actual_allocated

        shortage = required_qty - allocated
        if shortage > 0:
            self.material_shortage_records.append({
                'material_id': alternatives[0]['material_id'] if alternatives else None,
                'alternative_group': alternative_group,
                'shortage_qty': shortage,
                'order_id': order_id,
                'required_date': required_date,
                'allocated': allocated,
                'required': required_qty
            })

        return allocated, allocations

    def _adjust_alternative_priority_by_inventory(self, alternatives):
        """L3优化: 根据多因子评分动态调整替代料优先级

        升级版: 不再仅依赖库存比率，而是综合5个维度评分
        评分高的替代料在分配时获得更高优先级
        """
        adjusted = []
        for alt in alternatives:
            material_id = alt['material_id']
            available = self.get_available_inventory(material_id)
            total_demand = sum(a.get('ratio', 0) for a in alternatives)

            # 多因子评分（与calculate_alternative_allocation中的评分逻辑一致）
            inventory_ratio = available / max(total_demand, 1)

            # 供应商可靠性
            supplier_info = self.supplier_info_cache.get(material_id, {})
            reliability = supplier_info.get('delivery_reliability', 0.8)

            # 良率
            scrap_rate = alt.get('scrap_rate', 0.0) or 0.0
            yield_score = max(0.1, 1.0 - scrap_rate)

            # 综合调整分数
            adjustment_score = (
                inventory_ratio * 0.40 +
                reliability * 0.30 +
                yield_score * 0.30
            )

            original_priority = alt.get('priority', 1)
            # 基于综合分数调整优先级（数值越小越优先）
            if adjustment_score < 0.2:
                adjusted_priority = original_priority + 4   # 极差，大幅降级
            elif adjustment_score < 0.4:
                adjusted_priority = original_priority + 2   # 较差
            elif adjustment_score > 0.8:
                adjusted_priority = max(1, original_priority - 1)  # 优秀，提升
            else:
                adjusted_priority = original_priority

            adjusted.append({
                **alt,
                'adjusted_priority': adjusted_priority,
                'inventory_ratio': inventory_ratio,
                'available_inventory': available,
                'adjustment_score': round(adjustment_score, 4),
                'reliability': reliability,
                'yield_score': yield_score,
            })

        adjusted.sort(key=lambda x: (x['adjusted_priority'], -x['adjustment_score']))
        return adjusted

    # ==================== 特殊消耗规则支持方法 ====================

    def _build_allocation_context(self, order_id, material_id):
        """
        构建库存分配的上下文字典，用于特殊消耗规则匹配
        
        Args:
            order_id: 当前订单ID
            material_id: 当前物料ID
            
        Returns:
            dict: 包含订单属性、物料属性等信息的上下文
        """
        context = {
            'material_id': material_id,
            'order_id': order_id,
            'factory_code': None,
            'customer_id': None,
            'customer_name': None,  # SalesOrder使用customer_name而非FK
            'order_priority': 3,
            'is_forecast': False,
            'shipping_method': None,
        }
        
        # 尝试从缓存或数据库获取订单信息
        order = self.order_cache.get(order_id) if hasattr(self, 'order_cache') else None
        if not order:
            try:
                from .models import SalesOrder
                order = SalesOrder.objects.filter(id=order_id).first()
            except Exception:
                pass
        
        if order:
            context.update({
                'factory_code': getattr(order, 'factory_code', None),
                'order_priority': getattr(order, 'priority', 3),
                'is_forecast': getattr(order, 'is_forecast', False),
                'shipping_method': getattr(order, 'shipping_method', None),
            })
            # 兼容两种客户字段：customer(FK的ID) 或 customer_name(字符串)
            if hasattr(order, 'customer_id') and order.customer_id:
                context['customer_id'] = order.customer_id
            if hasattr(order, 'customer_name') and order.customer_name:
                context['customer_name'] = order.customer_name
                # 如果有customer_name但无customer_id，尝试通过名称查找Customer对象ID
                if not context.get('customer_id'):
                    try:
                        from .models import Customer
                        customer_obj = Customer.objects.filter(
                            customer_name__icontains=order.customer_name
                        ).first()
                        if customer_obj:
                            context['customer_id'] = customer_obj.id
                    except Exception:
                        pass
        
        return context

    def _get_applicable_consumption_rules(self, context: dict):
        """
        获取适用于当前分配上下文的特殊消耗规则列表（按优先级排序）
        
        从数据库加载所有启用的规则，过滤出匹配当前上下文的规则，
        按规则优先级升序排列（高优规则优先应用）。
        
        Args:
            context: _build_allocation_context() 构建的上下文
            
        Returns:
            list[ConsumptionRule]: 匹配的规则列表，按priority升序排列
        """
        try:
            from .models import ConsumptionRule
            
            rules = ConsumptionRule.objects.filter(is_active=True).select_related(
                'customer', 'material'
            ).order_by('priority')
            
            applicable = []
            for rule in rules:
                if rule.matches(context):
                    applicable.append(rule)
            
            if applicable:
                logger.debug(
                    f"特殊消耗规则匹配: {len(applicable)}条规则生效 "
                    f"(订单={context.get('order_id')}, 物料={context.get('material_id')})"
                )
            
            return applicable
            
        except Exception as e:
            logger.debug(f"加载特殊消耗规则失败(非致命): {str(e)}")
            return []

    def _apply_consumption_rules_to_inventory(self, inv, context, active_rules,
                                              remaining_qty, safety_stock, total_remaining):
        """
        将特殊消耗规则应用到单条库存记录上
        
        检查所有匹配规则的动作定义，决定：
        - 是否排除此库存记录（返回None）
        - 是否调整可用数量（返回修改后的字段字典）
        - 是否放行（返回inv原样）
        
        Args:
            inv: 库存记录字典
            context: 分配上下文
            active_rules: 匹配的特殊消耗规则列表
            remaining_qty: 剩余需分配数量
            safety_stock: 安全库存量
            total_remaining: 剩余总库存量
            
        Returns:
            None | dict | 原始inv: 
              - None: 排除此库存
              - dict: 调整后的属性覆盖
              - 不返回/其他: 放行
        """
        if not active_rules:
            return inv  # 无规则，直接放行
        
        modifications = {}
        
        for rule in active_rules:
            actions = rule.action_definition or {}
            
            # 规则1：限制允许的库存类型
            allowed_types = actions.get('inventory_types')
            if allowed_types and inv.get('type') not in allowed_types:
                logger.debug(
                    f"特殊规则[{rule.name}]排除库存: 类型{inv.get('type')}不在"
                    f"允许列表{allowed_types}中"
                )
                return None  # 明确排除
            
            # 规则2：排除特定批次
            excluded_batches = actions.get('excluded_batches', [])
            batch_no = inv.get('batch_no', '')
            if excluded_batches and batch_no in excluded_batches:
                logger.debug(f"特殊规则[{rule.name}]排除批次: {batch_no}")
                return None
            
            # 规则3：要求特定批次前缀
            required_prefix = actions.get('required_batch_prefix')
            if required_prefix and not str(batch_no).startswith(required_prefix):
                logger.debug(
                    f"特殊规则[{rule.name}]排除批次: {batch_no}不匹配前缀{required_prefix}"
                )
                return None
            
            # 规则4：调整可用数量（最大分配占比）
            max_pct = actions.get('max_allocation_pct')
            if max_pct and isinstance(max_pct, (int, float)):
                capped_qty = inv['quantity'] * max_pct
                modifications['quantity'] = min(inv.get('quantity', 0), capped_qty)
            
            # 规则5：优先级加成（影响排序，此处仅记录）
            boost = actions.get('allocation_boost')
            if boost:
                current_reliability = inv.get('reliability_factor', 1.0)
                modifications['reliability_factor'] = current_reliability * float(boost)
        
        return modifications if modifications else inv

    def _get_safety_stock_override(self, active_rules):
        """
        获取安全库存覆盖比例（来自最高优先级的安全库存覆盖规则）
        
        Returns:
            float: 0=完全不可突破, 1=可完全突破, 中间值=部分覆盖
        """
        override = 0.0
        for rule in active_rules:
            ov = rule.get_action('safety_stock_override')
            if ov is not None and isinstance(ov, (int, float)) and ov > override:
                override = min(1.0, max(0.0, float(ov)))
        return override

    def _get_max_allocation_pct(self, active_rules):
        """
        获取最大分配占比限制（取所有规则中最严格的限制）
        
        Returns:
            float or None: 最大分配占比(0-1)，None表示无限制
        """
        strictest = None
        for rule in active_rules:
            pct = rule.get_action('max_allocation_pct')
            if pct is not None and isinstance(pct, (int, float)):
                if strictest is None or pct < strictest:
                    strictest = pct
        return strictest

    def _check_plan_stability(self, order_id, new_complete_rate):
        """检查计划稳定性 - 避免多轮模拟造成较大波动"""
        previous = self.previous_plan_results.get(order_id)
        if previous is None:
            return True, 1.0
        
        rate_change = abs(new_complete_rate - previous)
        
        if rate_change > 0.5:
            return False, 0.0
        elif rate_change > 0.3:
            return False, 0.5
        elif rate_change > 0.1:
            return True, 0.8
        else:
            return True, 1.0

    def grab_material(self, material_id, required_qty, order_id, required_date=None, priority_threshold=5):
        """抢料策略 - 从低优先级订单抢夺物料，考虑安全库存"""
        grabbed = 0.0
        grab_records = []

        if material_id not in self.inventory_cache:
            return grabbed, grab_records

        material_info = self.material_info_cache.get(material_id, {})
        safety_stock = material_info.get('safety_stock', 0)

        total_inv_for_material = sum(i['quantity'] for i in self.inventory_cache[material_id])

        # 批量预加载相关订单（避免循环内N+1查询）
        relevant_order_ids = list({ar['order_id'] for ar in self.allocation_history if ar['material_id'] == material_id})
        orders_map = {
            o.id: o for o in SalesOrder.objects.select_related('material').filter(id__in=relevant_order_ids)
        }

        for alloc_record in self.allocation_history:
            if alloc_record['material_id'] != material_id:
                continue

            order_alloc = orders_map.get(alloc_record['order_id'])
            if not order_alloc or order_alloc.priority <= priority_threshold:
                continue

            if total_inv_for_material - grabbed < safety_stock:
                break

            for inv in self.inventory_cache[material_id]:
                if inv['id'] == alloc_record.get('inventory_id') and inv['quantity'] > 0:
                    # P2修复：统一Hold物料过滤
                    if not self.is_inventory_available(inv, required_date):
                        continue

                    max_grab = min(inv['quantity'], required_qty - grabbed)
                    
                    if total_inv_for_material - grabbed - max_grab >= safety_stock:
                        qty_to_grab = max_grab
                    else:
                        qty_to_grab = max(0, total_inv_for_material - grabbed - safety_stock)

                    if qty_to_grab > 0:
                        grab_records.append({
                            'from_order_id': alloc_record['order_id'],
                            'to_order_id': order_id,
                            'material_id': material_id,
                            'quantity': qty_to_grab,
                            'original_order_priority': order_alloc.priority
                        })

                        inv['quantity'] -= qty_to_grab
                        grabbed += qty_to_grab

                        if inv['quantity'] <= 0:
                            self.inventory_cache[material_id].remove(inv)

                        break

            if grabbed >= required_qty:
                break

        return grabbed, grab_records

    def release_material_for_higher_priority(self, material_id, required_qty, new_order_id, new_order_priority, required_date=None):
        released = 0.0
        release_records = []

        if material_id not in self.allocation_history:
            return released, release_records

        material_info = self.material_info_cache.get(material_id, {})
        safety_stock = material_info.get('safety_stock', 0)

        relevant_allocations = [
            alloc for alloc in self.allocation_history
            if alloc['material_id'] == material_id and alloc['order_id'] != new_order_id
        ]

        relevant_allocations.sort(key=lambda x: x.get('priority', 99))

        for alloc in relevant_allocations:
            if released >= required_qty:
                break

            if alloc.get('priority', 99) >= new_order_priority:
                continue

            current_inventory = sum(i['quantity'] for i in self.inventory_cache.get(material_id, []))
            releasable = min(alloc.get('quantity', 0), required_qty - released)

            if current_inventory + releasable - safety_stock < 0:
                continue

            release_records.append({
                'from_order_id': alloc['order_id'],
                'to_order_id': new_order_id,
                'material_id': material_id,
                'released_quantity': releasable,
                'original_priority': alloc.get('priority', 99),
                'new_priority': new_order_priority,
                'reason': f'让料给更高优先级订单(P{new_order_priority})',
                'release_type': 'priority_release'
            })

            self.order_promise_changes[alloc['order_id']] += 1

            PlanLog.objects.create(
                log_type='WARNING',
                message=f'让料: 订单{alloc["order_id"]}释放物料{material_id}数量{releasable}给订单{new_order_id}'
            )

            released += releasable

            alloc['quantity'] -= releasable
            if alloc['quantity'] <= 0:
                alloc['status'] = 'released'

        return released, release_records

    def analyze_shortage(self, order, shortage_details):
        """缺料分析 - 精准报缺，考虑供应商交付可靠率"""
        shortage_report = {
            'order_id': order.id,
            'order_no': order.order_no,
            'demand_date': order.demand_date,
            'material_shortages': [],
            'alternative_suggestions': [],
            'procurement_suggestions': []
        }

        # 批量预加载物料数据（避免循环内N+1查询）
        material_ids_in_shortage = [s['material_id'] for s in shortage_details]
        materials_map = {m.id: m for m in Material.objects.filter(id__in=material_ids_in_shortage)}

        # 使用预加载的供应商物料关联缓存（避免每次调用都查DB）
        suppliers_by_material = self.supplier_material_cache

        shipping_days = self._get_effective_shipping_days(order)

        for shortage in shortage_details:
            material_id = shortage['material_id']
            material = materials_map.get(material_id)
            if not material:
                continue

            material_info = self.material_info_cache.get(material_id, {})

            suppliers = suppliers_by_material.get(material_id, [])

            production_time = self.PRODUCTION_LEAD_TIME
            best_supplier_lt = min((s.lead_time for s in suppliers), default=material_info.get('lead_time', 7)) if suppliers else material_info.get('lead_time', 7)
            latest_purchase_date = order.demand_date - timedelta(days=shipping_days + production_time + best_supplier_lt)
            days_to_latest_purchase = (latest_purchase_date - date.today()).days

            if days_to_latest_purchase <= 3:
                urgency_level = 'critical'
                urgency_label = '紧急'
            elif days_to_latest_purchase <= 14:
                urgency_level = 'urgent'
                urgency_label = '加急'
            elif days_to_latest_purchase <= 30:
                urgency_level = 'normal'
                urgency_label = '正常'
            else:
                urgency_level = 'relaxed'
                urgency_label = '宽松'

            if days_to_latest_purchase < 0:
                recommended_action = f'已超期{abs(days_to_latest_purchase)}天，需立即采购并协商加急'
            elif urgency_level == 'critical':
                recommended_action = '立即下单，优先选择空运或现货供应商'
            elif urgency_level == 'urgent':
                recommended_action = '本周内完成下单，关注供应商交付能力'
            else:
                recommended_action = '按计划采购，可考虑批量议价'

            shortage_info = {
                'material_id': material_id,
                'material_code': material.material_code,
                'material_name': material.material_name,
                'required_qty': shortage['required'],
                'available_qty': shortage['allocated'],
                'shortage_qty': shortage['shortage'],
                'shortage_type': self._classify_shortage(shortage, order),
                'safety_stock': material_info.get('safety_stock', 0),
                'lead_time': material_info.get('lead_time', 7),
                'min_order_qty': material_info.get('min_order_qty', 1),
                'latest_purchase_date': latest_purchase_date,
                'days_to_latest_purchase': days_to_latest_purchase,
                'urgency_level': urgency_level,
                'urgency_label': urgency_label,
                'recommended_action': recommended_action
            }

            alternative_group = self._find_alternative_group(order.material_id, material_id)
            if alternative_group:
                shortage_info['alternative_group'] = alternative_group
                shortage_info['alternative_materials'] = self._get_alternative_materials(alternative_group)

            supplier_list = []
            for s in suppliers:
                supplier_info = self.supplier_info_cache.get(s.supplier_id, {})
                delivery_reliability = supplier_info.get('delivery_reliability', 0.9)

                available_date = order.demand_date - timedelta(days=s.lead_time + shipping_days + self.PRODUCTION_LEAD_TIME)
                on_time_probability = self._calculate_on_time_probability(s.lead_time, delivery_reliability, order.demand_date)

                supplier_list.append({
                    'supplier_code': s.supplier.supplier_code,
                    'supplier_name': s.supplier.supplier_name,
                    'lead_time': s.lead_time,
                    'unit_price': round(float(s.unit_price or 0), 2),
                    'min_order_qty': s.min_order_qty,
                    'rating': supplier_info.get('rating', 'B'),
                    'delivery_reliability': delivery_reliability,
                    'on_time_probability': on_time_probability,
                    'available_date': available_date,
                    'is_recommended': on_time_probability >= 0.8,
                    'latest_order_date': latest_purchase_date,
                    'urgency_level': urgency_level
                })

            # 回退：SupplierMaterial无数据时，从Supplier表取默认供应商
            if not supplier_list:
                try:
                    from .models import Supplier
                    default_sup = Supplier.objects.first()
                    if default_sup:
                        supplier_list.append({
                            'supplier_code': default_sup.supplier_code or '',
                            'supplier_name': default_sup.supplier_name or '待配置供应商',
                            'lead_time': material_info.get('lead_time', 7),
                            'unit_price': 0,
                            'min_order_qty': 1,
                            'rating': '-',
                            'delivery_reliability': 0,
                            'on_time_probability': 0,
                            'available_date': latest_purchase_date,
                            'is_recommended': True,
                            'latest_order_date': latest_purchase_date,
                            'urgency_level': urgency_level
                        })
                except Exception:
                    pass

            supplier_list.sort(key=lambda x: (-x['on_time_probability'], x['unit_price']))
            shortage_info['suppliers'] = supplier_list

            shortage_report['material_shortages'].append(shortage_info)

        return shortage_report
    
    def generate_purchase_orders(self, results):
        """根据缺料分析自动生成采购订单草稿"""
        from .models import PurchaseOrder, Supplier
        
        purchase_orders = []
        po_counter = 0
        
        for result in results:
            if not result.get('shortage_report'):
                continue
            
            order_no = result.get('order_no', '')
            shortage_report = result['shortage_report']
            
            for shortage in shortage_report.get('material_shortages', []):
                material_code = shortage.get('material_code')
                shortage_qty = shortage.get('shortage_qty', 0)
                
                if shortage_qty <= 0:
                    continue
                
                # 获取推荐供应商
                suppliers = shortage.get('suppliers', [])
                if not suppliers:
                    continue
                
                # 选择最佳供应商（按时交付概率最高）
                best_supplier = suppliers[0]
                
                # 检查最小起订量
                min_order_qty = shortage.get('min_order_qty', 1)
                order_qty = max(shortage_qty, min_order_qty)
                
                # 生成采购订单号
                po_counter += 1
                po_no = f"PO-{result.get('order_id', '')}-{material_code}-{po_counter:04d}"
                
                # 计算预计到货日期
                lead_time = best_supplier.get('lead_time', 7)
                expected_date = date.today() + timedelta(days=lead_time)
                
                purchase_order_data = {
                    'po_no': po_no,
                    'related_order_no': order_no,
                    'material_code': material_code,
                    'material_name': shortage.get('material_name', ''),
                    'supplier_code': best_supplier.get('supplier_code', ''),
                    'supplier_name': best_supplier.get('supplier_name', ''),
                    'quantity': order_qty,
                    'unit_price': best_supplier.get('unit_price', 0),
                    'total_amount': round(order_qty * best_supplier.get('unit_price', 0), 2),
                    'expected_date': expected_date,
                    'urgency_level': shortage.get('urgency_level', 'normal'),
                    'latest_order_date': shortage.get('latest_purchase_date'),
                    'on_time_probability': best_supplier.get('on_time_probability', 0),
                    'status': 'draft',
                    'created_from': 'auto_generation',
                    'shortage_qty': shortage_qty,
                    'min_order_qty': min_order_qty
                }
                
                purchase_orders.append(purchase_order_data)
        
        return purchase_orders
    
    def save_purchase_orders(self, purchase_orders):
        """保存自动生成的采购订单到数据库"""
        from .models import PurchaseOrder, Supplier, Material
        
        saved_count = 0
        errors = []
        
        for po_data in purchase_orders:
            try:
                # 查找供应商
                supplier = Supplier.objects.filter(supplier_code=po_data['supplier_code']).first()
                if not supplier:
                    errors.append(f"供应商 {po_data['supplier_code']} 不存在")
                    continue
                
                # 查找物料
                material = Material.objects.filter(material_code=po_data['material_code']).first()
                if not material:
                    errors.append(f"物料 {po_data['material_code']} 不存在")
                    continue
                
                # 创建采购订单
                PurchaseOrder.objects.create(
                    po_no=po_data['po_no'],
                    supplier=supplier,
                    material=material,
                    quantity=po_data['quantity'],
                    unit_price=po_data['unit_price'],
                    total_amount=po_data['total_amount'],
                    expected_date=po_data['expected_date'],
                    status='draft',
                    remarks=f"自动生成 - 关联订单: {po_data['related_order_no']}, 紧急程度: {po_data['urgency_level']}"
                )
                saved_count += 1
                
            except Exception as e:
                errors.append(f"保存采购订单 {po_data['po_no']} 失败: {str(e)}")
        
        return {
            'saved_count': saved_count,
            'errors': errors,
            'total_generated': len(purchase_orders)
        }

    def _calculate_on_time_probability(self, lead_time, reliability, demand_date):
        """计算按时交付概率"""
        today = date.today()
        days_available = (demand_date - today).days
        days_needed = lead_time + 2
        
        if days_available <= 0:
            return 0.0
        
        time_buffer = max(0, days_available - days_needed)
        buffer_factor = min(1.0, time_buffer / 7)
        
        return min(1.0, reliability * (0.7 + buffer_factor * 0.3))

    def _get_effective_shipping_days(self, order):
        """根据运输方式获取有效物流天数（硬性约束第2条：海运45天/空运3天）"""
        method = getattr(order, 'shipping_method', 'sea') or 'sea'
        # 优先从映射表获取，若未匹配则回退到订单属性或默认值
        mapped_days = self.SHIPPING_DAYS_MAP.get(method.lower())
        if mapped_days is not None:
            return mapped_days
        return getattr(order, 'shipping_days', 45) or 45

    def _classify_shortage(self, shortage, order):
        """分类缺料原因"""
        if shortage['allocated'] == 0:
            return '完全缺料'
        required = shortage.get('required', 0) or 0
        if required <= 0:
            return '轻微缺料'
        ratio = shortage.get('shortage', 0) / required
        if ratio > 0.5:
            return '严重缺料'
        elif ratio > 0.2:
            return '部分缺料'
        else:
            return '轻微缺料'

    def _find_alternative_group(self, parent_material_id, child_material_id):
        """查找替代料组 - 返回归一化后的组key"""
        for bom in self.bom_cache.get(parent_material_id, []):
            if bom['child_id'] == child_material_id and bom['alternative_group']:
                # 归一化：将自身也加入组中，排序后作为唯一key
                alt_codes = [c.strip() for c in str(bom['alternative_group']).split(',') if c.strip()]
                if alt_codes:
                    all_member_codes = sorted(set(alt_codes + [str(child_material_id)]))
                    return ','.join(all_member_codes)
        return None

    def _get_alternative_materials(self, alternative_group):
        """获取替代料组的所有物料，包含采购比例"""
        from .models import SubstituteMaterial
        alternatives = self.alternative_cache.get(alternative_group, [])
        if not alternatives:
            return []
        # 批量查询避免 N+1 问题
        material_ids = [alt['material_id'] for alt in alternatives]
        material_map = {
            m.id: m
            for m in Material.objects.filter(id__in=material_ids).only('id', 'material_code', 'material_name')
        }
        # 加载采购比例（从SubstituteMaterial模型）
        purchase_ratio_map = {}
        try:
            for sm in SubstituteMaterial.objects.filter(
                material_id__in=material_ids, is_active=True
            ).values('material_id', 'purchase_ratio'):
                purchase_ratio_map[sm['material_id']] = sm['purchase_ratio']
        except Exception:
            pass

        result = []
        for alt in alternatives:
            material = material_map.get(alt['material_id'])
            if material:
                result.append({
                    'material_code': material.material_code,
                    'material_name': material.material_name,
                    'priority': alt['priority'],
                    'ratio': alt['ratio'],
                    'purchase_ratio': purchase_ratio_map.get(alt['material_id'], alt['ratio']),
                    'scrap_rate': alt.get('scrap_rate', 0.0)
                })
        return result

    def _check_order_capacity(self, order, order_qty=None):
        """检查订单的产能约束 - 优化版，支持产能不足时智能决策"""
        if order_qty is None:
            order_qty = float(getattr(order, 'quantity', 0) or 0)
        material_obj = getattr(order, 'material', None)
        product_code = material_obj.material_code if material_obj else None

        # 使用预计算的产品-工作中心映射
        if not hasattr(self, '_product_wc_map'):
            self._product_wc_map = {}
            for wc_code, wc_info in self.workcenter_info_cache.items():
                if not wc_info['is_active']:
                    continue
                if wc_info['available_products']:
                    for p in wc_info['available_products']:
                        self._product_wc_map.setdefault(p, []).append((wc_code, wc_info))
                else:
                    self._product_wc_map.setdefault('*', []).append((wc_code, wc_info))

        # 获取候选工作中心
        candidates = self._product_wc_map.get(product_code, []) + self._product_wc_map.get('*', [])

        for wc_code, wc_info in candidates:
            check_result = self.check_capacity_constraint(wc_code, product_code, order_qty, order.demand_date)
            if check_result['available']:
                self.allocate_capacity(wc_code, product_code, order_qty, order.demand_date)
                return {
                    'available': True,
                    'work_center_code': wc_code,
                    'work_center_name': wc_info['name'],
                    'allocated_quantity': order_qty,
                    'remaining_capacity': check_result['available_capacity'] - order_qty
                }

        alternative_wc = self.find_alternative_workcenter(product_code, order_qty, order.demand_date)
        if alternative_wc:
            wc = alternative_wc[0]
            self.allocate_capacity(wc['work_center_code'], product_code, order_qty, order.demand_date)
            return {
                'available': True,
                'work_center_code': wc['work_center_code'],
                'work_center_name': wc['work_center_name'],
                'allocated_quantity': order_qty,
                'remaining_capacity': wc['available_capacity'] - order_qty,
                'is_alternative': True
            }
        else:
            # 产能不足智能决策：计算延迟交货建议
            delay_suggestion = self._calculate_delay_suggestion(order, order_qty, candidates)
            return {
                'available': False,
                'reason': f'所有工作中心产能不足，无法满足订单 {order.order_no} 的生产需求',
                'delay_suggestion': delay_suggestion,
                'recommendation': 'delay' if delay_suggestion else 'reject'
            }
    
    def _calculate_delay_suggestion(self, order, order_qty, candidates):
        """计算产能不足时的延迟交货建议"""
        from datetime import timedelta

        demand_date = getattr(order, 'demand_date', None)
        if not demand_date:
            return None

        # 尝试延迟1-14天寻找可用产能
        for delay_days in range(1, 15):
            new_demand_date = demand_date + timedelta(days=delay_days)
            
            for wc_code, wc_info in candidates:
                check_result = self.check_capacity_constraint(wc_code, order.material.material_code if order.material else None, order_qty, new_demand_date)
                if check_result['available']:
                    return {
                        'delay_days': delay_days,
                        'new_demand_date': new_demand_date,
                        'work_center_code': wc_code,
                        'work_center_name': wc_info['name'],
                        'available_capacity': check_result['available_capacity'],
                        'impact': 'low' if delay_days <= 3 else ('medium' if delay_days <= 7 else 'high')
                    }
        
        return None  # 延迟14天仍无法满足

    def process_order(self, order):
        """处理单个订单的物料分配 - 考虑产能约束、提前交货、多工厂调拨、CTO配置"""
        order_qty = float(order.quantity or 0)
        # 计算有效需求日期（支持提前交货）
        required_date, is_early_delivery = self.calculate_effective_required_date(order)
        order_factory_code = getattr(order, 'factory_code', None) or 'default'
        
        # 获取订单的CTO配置选项
        config_options = getattr(order, 'config_options', None)

        capacity_result = self._check_order_capacity(order, order_qty)
        if not capacity_result['available']:
            return {
                'order_id': order.id,
                'order_no': order.order_no,
                'requirements': {},
                'allocated': {},
                'shortage_details': [],
                'shortage_report': None,
                'complete_rate': 0,
                'is_complete': False,
                'allocation_type': 'none',
                'capacity_constraint': capacity_result,
                'failure_reason': f'产能约束: {capacity_result["reason"]}'
            }

        # P1修复：按订单工厂代码加载对应BOM（多工厂差异化支持）
        if order_factory_code and order_factory_code != 'default':
            self.load_bom_cache(factory_code=order_factory_code)

        requirements = self.get_bom_requirements(order.material_id, order_qty, config_options=config_options)
        allocated_materials = {}
        shortage_details = []
        transfer_details = []

        for material_id, req_info in requirements.items():
            required_qty = req_info['quantity']
            scrap_rate = req_info.get('scrap_rate', 0.0)
            
            # 使用归一化函数获取组key，确保与缓存一致
            alternative_group = self._find_alternative_group(order.material_id, material_id)

            if alternative_group:
                allocated, allocations = self.calculate_alternative_allocation(
                    alternative_group, required_qty, order.id, required_date
                )
            else:
                allocated, allocations = self.allocate_inventory(
                    material_id, required_qty, order.id, required_date
                )

            # 本地库存不足时，尝试跨工厂调拨
            if allocated < required_qty and order_factory_code != 'default':
                shortage_qty = required_qty - allocated
                transferred, transfer_recs = self.execute_factory_transfer(
                    material_id, shortage_qty, order_factory_code, order.id, required_date
                )
                if transferred > 0:
                    allocated += transferred
                    allocations.extend([{
                        'material_id': material_id,
                        'quantity': rec['quantity'],
                        'type': 'transfer',
                        'from_factory': rec['from_factory'],
                        'order_id': order.id,
                    } for rec in transfer_recs])
                    transfer_details.extend(transfer_recs)

            allocated_materials[material_id] = {
                'required': required_qty,
                'allocated': allocated,
                'scrap_rate': scrap_rate,
                'allocations': allocations
            }

            if allocated < required_qty:
                shortage_details.append({
                    'material_id': material_id,
                    'required': required_qty,
                    'allocated': allocated,
                    'shortage': required_qty - allocated,
                    'scrap_rate': scrap_rate
                })

        total_required = sum(r['quantity'] for r in requirements.values())
        # 修复: 替代料分配时allocated可能因bom_quantity倍数放大而远超required
        # 齐套率计算应使用min(allocated, required)，避免complete_rate>100%
        total_allocated = sum(min(a['allocated'], a['required']) for a in allocated_materials.values() if a.get('required', 0) > 0)

        complete_rate = total_allocated / total_required if total_required > 0 else 0
        # 硬上限: 完全齐套率最大为1.0（即使物理分配量超过需求量）
        complete_rate = min(complete_rate, 1.0)
        is_complete = complete_rate >= 1.0

        shortage_report = self.analyze_shortage(order, shortage_details) if shortage_details else None
        
        # 部分齐套订单拆分：计算可交付数量和待交付数量
        deliverable_qty = 0
        backorder_qty = 0
        split_suggestion = None
        
        if not is_complete and complete_rate > 0:
            # 计算可交付数量（受限于最紧缺物料的可用比例）
            min_ratio = 1.0
            for material_id, alloc_data in allocated_materials.items():
                if alloc_data['required'] > 0:
                    ratio = alloc_data['allocated'] / alloc_data['required']
                    min_ratio = min(min_ratio, ratio)
            
            deliverable_qty = int(order_qty * min_ratio)
            backorder_qty = order_qty - deliverable_qty
            
            if deliverable_qty > 0:
                split_suggestion = {
                    'original_qty': order_qty,
                    'deliverable_qty': deliverable_qty,
                    'backorder_qty': backorder_qty,
                    'deliverable_rate': min_ratio,
                    'recommendation': 'split' if backorder_qty > 0 else 'full_deliver'
                }

        return {
            'order_id': order.id,
            'order_no': order.order_no,
            'requirements': requirements,
            'allocated': allocated_materials,
            'shortage_details': shortage_details,
            'shortage_report': shortage_report,
            'complete_rate': complete_rate,
            'is_complete': is_complete,
            'allocation_type': self._classify_allocation_type(complete_rate),
            'is_early_delivery': is_early_delivery,
            'required_date': required_date,
            'transfer_details': transfer_details,
            'factory_code': order_factory_code,
            'split_suggestion': split_suggestion,
            'deliverable_qty': deliverable_qty,
            'backorder_qty': backorder_qty,
        }

    def _classify_allocation_type(self, complete_rate):
        """分类订单齐套类型"""
        if complete_rate >= 1.0:
            return 'complete'
        elif complete_rate > 0:
            return 'partial'
        else:
            return 'none'

    def sort_orders_by_dynamic_priority(self, orders, strategy=None):
        """按动态优先级排序订单（strategy 参数影响排序维度）

        核心目标：在现有资源条件下完成最多的订单
        - 允许抢占物料：资源无法同时满足两个订单时，集中资源全力完成一个
        - 抢占包括BOM替代物料的情况

        6种策略（三层排序：L1基底completability → L2主特色 → L3辅助）：
        - delivery_first:    交期近 + 历史交付差 的排最前面，优先保障高风险订单交付
        - cost_first:        缺料量最大的先分配，最大限度减少加急采购成本
        - inventory_first:   低水位物料对应的订单优先，防止断料风险
        - stability_first:   大额订单优先，保障生产连续性和产出最大化
        - supplier_first:    优质供应商物料对应的订单优先，降低供应链风险
        - expiry_first:      临期物料 + 低库存 组合最优先，减少呆滞损失

        所有策略均以 completability（可完成度）为基底权重，确保"能完成的订单优先做"
        """
        # 策略对应各维度乘数配置
        # ============================================================
        # 权重层级设计（三层排序机制）：
        #   L1 基底层 completability=65000 → 确保"能完成的订单优先做"
        #   L2 主特色层 150000~200000     → 策略核心业务目标，绝对主导排序
        #   L3 辅助层 40000~120000         → 同级别内的二次精细区分
        #   通用参考 urgency=200~500       → 所有策略的基础参考因子
        # ============================================================
        _STRATEGY_WEIGHTS = {
            # ----------------------------------------------------------
            # delivery_first: 交付保障策略
            # 业务目标：离交期最近的、历史交付表现差的订单排在最前面
            #           优先将资源分配给这些高风险订单
            #           多余资源继续按交期时间顺序分配
            # 主导因子：urgency(交期紧迫) + delivery(历史交付差→高分)
            # ----------------------------------------------------------
            'delivery_first':   {'urgency': 180000,    # L2主: 交期越近得分越高，绝对主导
                                 'shortage': 0,
                                 'customer': 0,
                                 'delivery': 120000,   # L3辅: 历史交付差的订单得分更高（需重点保障）
                                 'value': 0,
                                 'stock': 0,
                                 'expiry': 0,
                                 'supplier': 0,
                                 'completability': 65000},  # L1基底: 能完成才值得投入资源

            # ----------------------------------------------------------
            # cost_first: 成本控制策略
            # 业务目标：缺料量最大的订单优先分配资源，最大限度减少加急采购成本
            #           缺料越严重的订单不优先处理 → 加急采购费用越高
            # 主导因子：shortage(缺料严重度)
            # ----------------------------------------------------------
            'cost_first':       {'urgency': 200,       # 通用参考
                                 'shortage': 200000,   # L2主: 缺料量越大越优先（绝对主导）
                                 'customer': 0,
                                 'delivery': 0,
                                 'value': 0,
                                 'stock': 0,
                                 'expiry': 0,
                                 'supplier': 0,
                                 'completability': 65000},  # L1基底

            # ----------------------------------------------------------
            # inventory_first: 库存补充策略
            # 业务目标：低水位（库存不足）物料对应的订单优先分配资源
            #           防止低库存物料断料导致生产中断
            # 主导因子：stock(库存水位低→高分)
            # ----------------------------------------------------------
            'inventory_first':  {'urgency': 200,       # 通用参考
                                 'shortage': 40000,    # L3辅: 缺料情况辅助判断
                                 'customer': 0,
                                 'delivery': 0,
                                 'value': 0,
                                 'stock': 180000,     # L2主: 库存水位越低越优先（绝对主导）
                                 'expiry': 0,
                                 'supplier': 0,
                                 'completability': 65000},  # L1基底

            # ----------------------------------------------------------
            # stability_first: 生产稳定策略
            # 业务目标：大额订单优先分配资源，保障生产连续性和产出最大化
            #           完成大订单比分散完成多个小订单更能维持产线稳定
            # 主导因子：value(订单金额)
            # ----------------------------------------------------------
            'stability_first':  {'urgency': 200,       # 通用参考
                                 'shortage': 0,
                                 'customer': 0,
                                 'delivery': 0,
                                 'value': 200000,      # L2主: 订单金额越大越优先（绝对主导）
                                 'stock': 0,
                                 'expiry': 0,
                                 'supplier': 0,
                                 'completability': 65000},  # L1基底

            # ----------------------------------------------------------
            # supplier_first: 供应安全策略
            # 业务目标：优质供应商（评级高）物料对应的订单优先分配
            #           好供应商物料质量和交付更有保障，降低供应链风险
            # 主导因子：supplier(供应商评级)
            # ----------------------------------------------------------
            'supplier_first':   {'urgency': 200,       # 通用参考
                                 'shortage': 0,
                                 'customer': 0,
                                 'delivery': 0,
                                 'value': 0,
                                 'stock': 0,
                                 'expiry': 0,
                                 'supplier': 180000,   # L2主: 供应商评级越高越优先（绝对主导）
                                 'completability': 65000},  # L1基底

            # ----------------------------------------------------------
            # expiry_first: 效期管理策略
            # 业务目标：临期物料 + 低库存 的组合最优先处理
            #           临期物料不尽快消耗将导致呆滞损失
            #           同时低库存物料也有断料风险
            # 主导因子：expiry(到期紧迫) + stock(低库存辅助)
            # ----------------------------------------------------------
            'expiry_first':     {'urgency': 300,       # 略高于其他策略（效期与交期相关）
                                 'shortage': 0,
                                 'customer': 0,
                                 'delivery': 0,
                                 'value': 0,
                                 'stock': 80000,      # L3辅: 低库存物料组合考虑
                                 'expiry': 150000,    # L2主: 到期日越近越优先（绝对主导）
                                 'supplier': 0,
                                 'completability': 65000},  # L1基底
        }
        weights = _STRATEGY_WEIGHTS.get(strategy, {}) if strategy else {}

        # 预加载所有物料的库存和 BOM 信息（避免 N+1 查询）
        material_ids = list(set(getattr(o, 'material_id', None) for o in orders if getattr(o, 'material_id', None)))
        inv_data = dict(
            Inventory.objects.filter(material_id__in=material_ids)
            .values('material_id')
            .annotate(total=Sum('quantity'))
            .values_list('material_id', 'total')
        )
        bom_data = {}
        for bom in BillOfMaterials.objects.filter(parent_material_id__in=material_ids):
            bom_data.setdefault(bom.parent_material_id, []).append(bom.child_material_id)

        # 预加载交付历史数据（用于 delivery_first 策略）
        order_ids = [o.id for o in orders]
        self._delivery_cache = {}
        for r in MaterialPlanResult.objects.filter(order_id__in=order_ids).values('order_id', 'complete_rate'):
            self._delivery_cache[r['order_id']] = float(r.get('complete_rate', 0.5))

        # 预加载物料到期数据（用于 expiry_first 策略）
        self._expiry_cache = {}
        from .models import Material
        for mi in Material.objects.filter(id__in=material_ids):
            expiry_date = getattr(mi, 'expiry_date', None)
            if expiry_date:
                try:
                    days_to_expiry = (expiry_date - date.today()).days
                    self._expiry_cache[mi.id] = days_to_expiry
                except (TypeError, AttributeError):
                    pass

        # 预加载供应商评级数据（用于 supplier_first 策略）
        self._supplier_cache = {}
        from .models import SupplierMaterial, Supplier
        for sm in SupplierMaterial.objects.filter(material_id__in=material_ids).select_related('supplier'):
            if sm.supplier:
                rating = float(sm.supplier.rating or 3)
                self._supplier_cache[sm.material_id] = min(1.0, rating / 5.0)

        # 预加载客户信用数据（用于 customer 权重）
        customer_names = list(set(getattr(o, 'customer_name', None) for o in orders if getattr(o, 'customer_name', None)))
        self._customer_cache = {}
        from .models import Customer
        for c in Customer.objects.filter(customer_name__in=customer_names):
            credit_limit = float(c.credit_limit or 0)
            if credit_limit >= 500000:
                self._customer_cache[c.customer_name] = 1.0
            elif credit_limit >= 200000:
                self._customer_cache[c.customer_name] = 0.7
            elif credit_limit >= 50000:
                self._customer_cache[c.customer_name] = 0.5
            else:
                self._customer_cache[c.customer_name] = 0.3

        scored_orders = []
        for order in orders:
            urgency_score = self._get_urgency_score(order)
            shortage_score = self._get_shortage_score(order, inv_data, bom_data)
            customer_score = self._get_customer_score(order)
            delivery_score = self._get_delivery_score(order)
            value_score = self._get_value_score(order)
            stock_score = self._get_stock_score(order, inv_data)
            expiry_score = self._get_expiry_score(order)
            supplier_score = self._get_supplier_score(order)
            completability_score = self._get_completability_score(order, inv_data, bom_data)

            score = (
                urgency_score       * weights.get('urgency',       0) +
                shortage_score      * weights.get('shortage',      0) +
                customer_score      * weights.get('customer',      0) +
                delivery_score      * weights.get('delivery',      0) +
                value_score         * weights.get('value',         0) +
                stock_score         * weights.get('stock',         0) +
                expiry_score        * weights.get('expiry',        0) +
                supplier_score      * weights.get('supplier',      0) +
                completability_score * weights.get('completability', 0)
            )
            scored_orders.append((score, order))

        scored_orders.sort(key=lambda x: x[0], reverse=True)
        return [order for _, order in scored_orders]

    def _get_urgency_score(self, order):
        """交期紧迫度得分（0~1，越高越紧急）"""
        demand_date = getattr(order, 'demand_date', None)
        if demand_date:
            try:
                days_to_deadline = (demand_date - date.today()).days
            except (TypeError, AttributeError):
                days_to_deadline = 999
        else:
            days_to_deadline = 999
        if days_to_deadline <= 3:   return 1.0
        elif days_to_deadline <= 7: return 0.8
        elif days_to_deadline <= 14: return 0.5
        elif days_to_deadline <= 30: return 0.3
        return 0.1

    def _get_shortage_score(self, order, inv_data, bom_data):
        """缺料严重度得分（0~1，基于 BOM 缺口比例）"""
        material_id = getattr(order, 'material_id', None)
        if not material_id:
            return 0.5
        demand_qty = float(getattr(order, 'quantity', 0) or 0)
        if demand_qty <= 0:
            return 0.5
        child_ids = bom_data.get(material_id, [])
        if not child_ids:
            stock = inv_data.get(material_id, 0)
            shortage_ratio = max(0, demand_qty - stock) / demand_qty
            return shortage_ratio
        total_shortage = 0
        for child_id in child_ids:
            child_demand = demand_qty
            child_stock = inv_data.get(child_id, 0)
            total_shortage += max(0, child_demand - child_stock)
        shortage_ratio = min(1.0, total_shortage / (demand_qty * len(child_ids) + 0.001))
        return shortage_ratio

    def _get_customer_score(self, order):
        """客户信用得分（0~1）"""
        customer_name = getattr(order, 'customer_name', None)
        if not customer_name:
            return 0.5
        if hasattr(self, '_customer_cache') and customer_name in self._customer_cache:
            return self._customer_cache[customer_name]
        return 0.5

    def _get_delivery_score(self, order):
        """历史交付得分（0~1，基于历史齐套率）"""
        order_id = getattr(order, 'id', None)
        if order_id and hasattr(self, '_delivery_cache') and order_id in self._delivery_cache:
            return self._delivery_cache[order_id]
        return 0.5

    def _get_value_score(self, order):
        """订单价值得分（0~1）"""
        return min(1.0, float(getattr(order, 'total_amount', 0) or 0) / 100000)

    def _get_stock_score(self, order, inv_data):
        """库存水位得分（0~1，库存越低得分越高）"""
        material_id = getattr(order, 'material_id', None)
        if not material_id:
            return 0.5
        demand_qty = float(getattr(order, 'quantity', 0) or 0)
        stock = inv_data.get(material_id, 0)
        if demand_qty <= 0:
            return 0.5
        return max(0, 1.0 - stock / demand_qty)

    def _get_expiry_score(self, order):
        """物料到期紧迫度得分（0~1，基于物料有效期）"""
        material_id = getattr(order, 'material_id', None)
        if not material_id or not hasattr(self, '_expiry_cache') or material_id not in self._expiry_cache:
            return 0.5
        expiry_days = self._expiry_cache[material_id]
        if expiry_days <= 30:   return 1.0
        elif expiry_days <= 90: return 0.7
        elif expiry_days <= 180: return 0.4
        return 0.2

    def _get_supplier_score(self, order):
        """供应商评级得分（0~1）"""
        material_id = getattr(order, 'material_id', None)
        if not material_id or not hasattr(self, '_supplier_cache') or material_id not in self._supplier_cache:
            return 0.5
        return self._supplier_cache[material_id]

    def _get_completability_score(self, order, inv_data, bom_data):
        """可完成度得分（0~1，越高表示该订单越容易被完全满足）

        核心逻辑：
        - 展开订单的BOM，获取所有子物料需求
        - 对每个子物料计算：可用库存 / 需求量 = 满足率
        - 取所有物料满足率的最小值（木桶效应）
        - 考虑替代物料组：如果某物料缺货但替代物料有库存，满足率可以提高

        这个分数直接影响排序：能完成的订单排在前面，避免资源分散
        """
        material_id = getattr(order, 'material_id', None)
        if not material_id:
            return 0.3  # 无物料信息的订单默认低完成度

        order_qty = getattr(order, 'quantity', 1) or 1

        try:
            requirements = self.get_bom_requirements(material_id, order_qty)
            if not requirements:
                # 无BOM需求的订单（可能是成品直接出库），检查成品库存
                total_inv = float(inv_data.get(material_id, 0) or 0)
                if total_inv >= order_qty:
                    return 1.0
                elif total_inv > 0:
                    return min(0.9, total_inv / order_qty)
                return 0.2

            min_fulfillment_rate = 1.0  # 初始假设都能满足

            for req_material_id, req_info in requirements.items():
                req_qty = req_info.get('required_quantity', 0) or 0
                if req_qty <= 0:
                    continue

                # 获取该物料的可用库存总量
                available_qty = 0.0
                if req_material_id in self.inventory_cache:
                    for inv_rec in self.inventory_cache.get(req_material_id, []):
                        required_date = getattr(order, 'demand_date', None)
                        if self.is_inventory_available(inv_rec, required_date):
                            available_qty += inv_rec.get('quantity', 0)

                # 基础满足率
                base_rate = available_qty / req_qty if req_qty > 0 else 1.0

                # 检查替代物料组是否能补充（BOM替代物料抢占支持）
                alt_group = self._find_alternative_group(material_id, req_material_id)
                if alt_group and base_rate < 1.0:
                    # 有替代物料组且基础满足率不足，尝试从替代物料补充
                    alt_available = 0.0
                    for alt_mid in alt_group:
                        if alt_mid == req_material_id:
                            continue
                        if alt_mid in self.inventory_cache:
                            for inv_rec in self.inventory_cache.get(alt_mid, []):
                                required_date = getattr(order, 'demand_date', None)
                                if self.is_inventory_available(inv_rec, required_date):
                                    alt_available += inv_rec.get('quantity', 0)

                    if alt_available > 0:
                        # 替代物料可以弥补部分缺口
                        gap = max(0, req_qty - available_qty)
                        alt_contribution = min(alt_available, gap)
                        enhanced_rate = (available_qty + alt_contribution) / req_qty
                        base_rate = max(base_rate, enhanced_rate)

                # 更新最小满足率（木桶效应）
                min_fulfillment_rate = min(min_fulfillment_rate, base_rate)

            # 将满足率映射到0~1评分（使用sigmoid-like曲线增强区分度）
            if min_fulfillment_rate >= 1.0:
                return 1.0      # 完全可以满足
            elif min_fulfillment_rate >= 0.8:
                return 0.9      # 基本能满足
            elif min_fulfillment_rate >= 0.5:
                return 0.7 + (min_fulfillment_rate - 0.5) * 0.4  # 0.7~0.9
            elif min_fulfillment_rate >= 0.2:
                return 0.3 + (min_fulfillment_rate - 0.2) * 1.33  # 0.3~0.7
            else:
                return min_fulfillment_rate * 1.5  # 0~0.3，极度缺乏的订单得分很低

        except Exception as e:
            logger.warning(f'计算completability_score异常: {e}')
            return 0.5  # 异常时返回中性分

    def auto_adjust_priority(self, orders=None):
        """自动调整订单优先级 - 基于交期紧迫度、客户等级、订单价值、齐套率动态计算

        Args:
            orders: 待调整的订单QuerySet，为None时自动查询所有未完成订单

        Returns:
            dict: 调整汇总（调整数量、优先级变更明细等）
        """
        from .models import Customer

        if orders is None:
            orders = SalesOrder.objects.select_related('material').filter(
                ~Q(status__in=['cancelled', 'delivered', 'shipped'])
            )
        orders_list = list(orders)

        if not orders_list:
            PlanLog.objects.create(
                log_type='INFO',
                message='订单优先级自动调整：无待处理订单'
            )
            return {'adjusted_count': 0, 'total_orders': 0, 'changes': []}

        # 获取当前激活的优先级规则
        rule = PriorityRule.objects.filter(is_active=True).first()
        if rule:
            urgency_weight = rule.urgency_weight
            customer_weight = rule.customer_weight
            delivery_weight = rule.delivery_weight
            value_weight = rule.value_weight
        else:
            urgency_weight = 0.3
            customer_weight = 0.2
            delivery_weight = 0.3
            value_weight = 0.1

        # 齐套率权重（默认与订单价值权重一致）
        kit_rate_weight = max(0.1, 1.0 - urgency_weight - customer_weight - delivery_weight - value_weight)

        # 批量预加载客户交付优先级 {customer_name: delivery_priority}
        customer_priority_map = {}
        for cust in Customer.objects.filter(is_active=True):
            customer_priority_map[cust.customer_name] = cust.delivery_priority

        # 批量预加载齐套率 {order_id: complete_rate}
        kit_rate_map = {}
        for plan_result in MaterialPlanResult.objects.filter(order_id__in=[o.id for o in orders_list]):
            kit_rate_map[plan_result.order_id] = plan_result.complete_rate

        today = date.today()
        scored_orders = []

        for order in orders_list:
            # 1. 交期紧迫度得分：越近分数越高
            days_remaining = (order.demand_date - today).days
            if days_remaining <= 0:
                urgency_score = 1.0  # 已逾期或今日到期
            elif days_remaining <= 3:
                urgency_score = 0.95
            elif days_remaining <= 7:
                urgency_score = 0.8
            elif days_remaining <= 14:
                urgency_score = 0.6
            elif days_remaining <= 30:
                urgency_score = 0.4
            elif days_remaining <= 60:
                urgency_score = 0.2
            else:
                urgency_score = 0.1

            # 2. 客户等级得分：基于Customer.delivery_priority (1最高~5最低)
            customer_score = 0.5
            delivery_priority = customer_priority_map.get(order.customer_name)
            if delivery_priority is not None:
                # delivery_priority: 1(最高)~5(最低) → 转换为0~1得分(越高越好)
                customer_score = max(0.0, (6 - delivery_priority) / 5.0)

            # 3. 订单价值得分
            order_value = float(order.quantity or 0) * float(order.unit_price or 0)
            value_score = min(1.0, order_value / 100000)

            # 4. 齐套率得分：齐套率越低，越需要关注，得分越高
            complete_rate = kit_rate_map.get(order.id, 1.0)
            kit_score = max(0.0, 1.0 - complete_rate)

            # 综合得分
            total_score = (
                urgency_score * urgency_weight +
                customer_score * customer_weight +
                urgency_score * delivery_weight +  # 交期紧迫度复用urgency_score
                value_score * value_weight +
                kit_score * kit_rate_weight
            )

            scored_orders.append((total_score, order))

        # 按综合得分降序排序
        scored_orders.sort(key=lambda x: x[0], reverse=True)

        # 映射到1-5优先级：按排名分5档
        total = len(scored_orders)
        changes = []

        with transaction.atomic():
            for idx, (score, order) in enumerate(scored_orders):
                # 按百分位映射到1-5
                percentile = idx / total
                if percentile < 0.2:
                    new_priority = 1
                elif percentile < 0.4:
                    new_priority = 2
                elif percentile < 0.6:
                    new_priority = 3
                elif percentile < 0.8:
                    new_priority = 4
                else:
                    new_priority = 5

                old_priority = order.priority
                if new_priority != old_priority:
                    SalesOrder.objects.filter(id=order.id).update(priority=new_priority)
                    changes.append({
                        'order_id': order.id,
                        'order_no': order.order_no,
                        'old_priority': old_priority,
                        'new_priority': new_priority,
                        'score': round(score, 4),
                    })

        # 记录日志
        if changes:
            change_summary = ', '.join(
                [f"{c['order_no']}({c['old_priority']}→{c['new_priority']})" for c in changes[:20]]
            )
            suffix = f'...等共{len(changes)}条' if len(changes) > 20 else ''
            PlanLog.objects.create(
                log_type='PLANNING',
                message=f'订单优先级自动调整完成，共调整{len(changes)}/{total}条订单: {change_summary}{suffix}'
            )
        else:
            PlanLog.objects.create(
                log_type='INFO',
                message=f'订单优先级自动调整完成，{total}条订单优先级无变化'
            )

        logger.info(f'订单优先级自动调整完成: 总计{total}条，调整{len(changes)}条')

        return {
            'adjusted_count': len(changes),
            'total_orders': total,
            'changes': changes,
        }

    def run_planning(self, orders=None, max_orders_per_batch=1000, strategy=None, refresh_cache=True):
        """执行物料计划 - 分层优化架构(L1-L6)

        六层架构执行流程:
          L1 战略层: NSGA-II决定策略参数（由外部调用optimize_allocation时触发）
          L2 计划层: BOM展开(DAG缓存) + 订单优先级排序
          L3 分配层: Value-aware库存分配 + 多因子替代料评分
          L4 修复层: 让料 + 抢占机制
          L5 仿真层: JIT优化 + 交付变更检测
          L6 学习层: 性能统计收集(供后续NSGA-II/RL学习使用)
        """
        plan_start_time = time.time()

        # 存储策略（用于订单排序）
        self.strategy = strategy

        # ========== 全局共享紧急补货池 ==========
        GLOBAL_EMERGENCY_POOL_RATIO = 0.15
        self.emergency_inventory_pool = 0.0
        self.emergency_used = 0.0

        # 执行订单优先级自动调整
        self.auto_adjust_priority()

        if orders is None:
            orders = SalesOrder.objects.select_related('material').filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
            ).order_by('priority', 'demand_date').only(
                'id', 'order_no', 'material_id', 'quantity', 'demand_date',
                'priority', 'status', 'customer_name', 'total_amount',
                'factory_code', 'is_forecast', 'created_at', 'updated_at'
            )

        # ====== L2 计划层: 数据加载与BOM DAG构建 ======
        load_start = time.time()
        if refresh_cache:
            self.load_material_info_cache()
            self.load_supplier_info_cache()
            self.load_forbidden_materials()
            self.load_workcenter_info_cache()
            self.load_factory_calendar()
            self.load_inventory_cache()
            self.load_bom_cache()  # 内部自动触发 _build_bom_dag()
            self.load_priority_rule()
        load_elapsed = time.time() - load_start

        # 初始化紧急补货池（基于总库存和全局共享比例）
        total_inv = sum(
            sum(inv['quantity'] for inv in inv_list)
            for inv_list in self.inventory_cache.values()
        )
        self.emergency_inventory_pool = total_inv * GLOBAL_EMERGENCY_POOL_RATIO
        self.emergency_used = 0.0

        forbidden_changes = self.check_forbidden_material_changes()
        if forbidden_changes['has_changes']:
            PlanLog.objects.create(
                log_type='WARNING',
                message=f'计划执行前检测到禁用料变化，可能影响物料分配结果'
            )

        orders_list = list(orders)

        # ====== L2 计划层: 订单优先级排序 ======
        # 预测订单隔离：先处理正式订单，再处理预测订单
        formal_orders = [o for o in orders_list if not getattr(o, 'is_forecast', False)]
        forecast_orders = [o for o in orders_list if getattr(o, 'is_forecast', False)]

        # 先按策略优先级排序正式订单（6种动态策略）
        formal_orders = self.sort_orders_by_dynamic_priority(formal_orders, strategy=strategy)

        all_results = []
        batch_count = 0

        process_start = time.time()

        # ====== L3 分配层: 逐单分配（Value-aware + 多因子替代料）======
        # 阶段1：处理正式订单（支持分批处理）
        if formal_orders:
            if len(formal_orders) > max_orders_per_batch:
                # 分批处理大订单集，每批处理完后释放内存
                for batch_start in range(0, len(formal_orders), max_orders_per_batch):
                    batch = formal_orders[batch_start:batch_start + max_orders_per_batch]
                    batch_count += 1
                    logger.info(f'处理第{batch_count}批订单，本批{len(batch)}条')
                    batch_results = self._run_planning(batch)
                    # 分批保存中间结果，释放内存
                    self._record_promise_changes(batch_results)
                    self._save_planning_results(batch_results)
                    all_results.extend(batch_results)
                    del batch
                    del batch_results
            else:
                formal_results = self._run_planning(formal_orders)
                all_results.extend(formal_results)
        
        # 阶段2：处理预测订单（使用剩余库存）
        if forecast_orders:
            # 重新加载库存缓存（排除已分配的库存）
            self._refresh_available_inventory()
            forecast_orders = self.sort_orders_by_dynamic_priority(forecast_orders, strategy=strategy)
            forecast_results = self._run_planning(forecast_orders)
            all_results.extend(forecast_results)

        process_elapsed = time.time() - process_start

        # ====== L4 修复层: 让料 + 抢占（在_run_planning内部执行）======
        # _run_planning() 内部包含三阶段: 分配→让料→抢占

        save_start = time.time()
        # 记录交期变更计数
        if batch_count > 0:
            if forecast_orders:
                forecast_results_list = [r for r in all_results if r.get('order_id') in {o.id for o in forecast_orders}]
                self._record_promise_changes(forecast_results_list)
        else:
            self._record_promise_changes(all_results)
        if batch_count > 0:
            forecast_results_all = [r for r in all_results if r.get('order_id') in {o.id for o in forecast_orders}] if forecast_orders else []
            if forecast_results_all:
                self._save_planning_results(forecast_results_all)
        else:
            self._save_planning_results(all_results)
        self._save_transfer_records(all_results)
        self._enforce_delivery_change_constraint(all_results)
        delivery_alerts = self.check_delivery_change_alerts()
        save_elapsed = time.time() - save_start

        # ====== L5 仿真层: JIT优化 ======
        jit_start = time.time()
        jit_result = self.optimize_inventory_jit(all_results)
        jit_elapsed = time.time() - jit_start

        total_elapsed = time.time() - plan_start_time

        # ====== L6 学习层: 性能统计收集 ======
        perf_info = {
            'total_elapsed_seconds': round(total_elapsed, 2),
            'data_load_seconds': round(load_elapsed, 2),
            'order_process_seconds': round(process_elapsed, 2),
            'result_save_seconds': round(save_elapsed, 2),
            'jit_optimization_seconds': round(jit_elapsed, 2),
            'total_orders': len(orders_list),
            'formal_orders': len(formal_orders),
            'forecast_orders': len(forecast_orders),
            'max_orders_per_batch': max_orders_per_batch,
            'batch_count': batch_count,
            # L6: 优化统计（供NSGA-II/RL学习使用）
            'optimization_stats': {
                'bom_dag_cache_hits': getattr(self, '_bom_dag_stats', {}).get('hits', 0),
                'bom_dag_cache_misses': getattr(self, '_bom_dag_stats', {}).get('misses', 0),
                'bom_dag_built': getattr(self, '_bom_dag_built', False),
                'consumption_priority': self.consumption_priority,
                'strategy_used': strategy,
                'total_allocations': len(self.allocation_history),
                'total_transfers': len(self.transfer_records),
                'total_shortages': len(self.material_shortage_records),
            }
        }

        logger.info(
            f'物料计划执行完成 - 总耗时:{total_elapsed:.2f}s, '
            f'数据加载:{load_elapsed:.2f}s, 订单处理:{process_elapsed:.2f}s, '
            f'结果保存:{save_elapsed:.2f}s, JIT优化:{jit_elapsed:.2f}s, '
            f'订单数:{len(orders_list)}, 批次数:{batch_count}'
        )

        return {'results': all_results, 'jit_optimization': jit_result, 'performance': perf_info, 'delivery_alerts': delivery_alerts}
    
    def run_incremental_planning(self, changed_material_ids=None, changed_order_ids=None):
        """增量计算 - 只重算受影响的订单
        
        Args:
            changed_material_ids: 变化的物料ID列表（库存变化、BOM变化等）
            changed_order_ids: 变化的订单ID列表（新增、修改、删除等）
        """
        if not changed_material_ids and not changed_order_ids:
            logger.info('无变化，跳过增量计算')
            return {'results': [], 'affected_orders': 0}
        
        # 找出受影响的订单
        affected_order_ids = set()
        
        # 1. 直接变化的订单
        if changed_order_ids:
            affected_order_ids.update(changed_order_ids)
        
        # 2. 使用变化物料的订单
        if changed_material_ids:
            # 通过BOM找出使用这些物料的成品
            affected_finished_goods = set()
            for bom in self.bom_cache.values():
                for bom_item in bom:
                    if bom_item['child_id'] in changed_material_ids:
                        affected_finished_goods.add(bom_item.get('parent_id'))
            
            # 找出使用这些成品的订单
            if affected_finished_goods:
                affected_orders = SalesOrder.objects.filter(
                    material_id__in=affected_finished_goods,
                    status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
                ).values_list('id', flat=True)
                affected_order_ids.update(affected_orders)
            
            # 直接使用这些物料的订单（作为成品）
            direct_orders = SalesOrder.objects.filter(
                material_id__in=changed_material_ids,
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
            ).values_list('id', flat=True)
            affected_order_ids.update(direct_orders)
        
        if not affected_order_ids:
            logger.info('无受影响的订单，跳过增量计算')
            return {'results': [], 'affected_orders': 0}
        
        logger.info(f'增量计算：{len(affected_order_ids)} 个受影响的订单')
        
        # 重新加载数据
        self.load_material_info_cache()
        self.load_supplier_info_cache()
        self.load_forbidden_materials()
        self.load_workcenter_info_cache()
        self.load_factory_calendar()
        self.load_inventory_cache()
        self.load_bom_cache()
        self.load_priority_rule()
        
        # 获取受影响的订单
        affected_orders = SalesOrder.objects.select_related('material').filter(
            id__in=affected_order_ids,
            status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
        ).order_by('priority', 'demand_date')
        
        # 执行计划
        orders_list = list(affected_orders)
        results = self._run_planning(orders_list)
        
        # 保存结果
        self._record_promise_changes(results)  # 先记录变更计数
        self._save_planning_results(results)   # 再保存（包含变更计数）
        self._save_transfer_records(results)
        self._enforce_delivery_change_constraint(results)
        
        return {'results': results, 'affected_orders': len(affected_order_ids)}
    
    def _refresh_available_inventory(self):
        """刷新可用库存 - 库存字典在分配时已被直接修改，此方法用于日志记录"""
        # 库存在 allocate_inventory 中已被直接修改（inv['quantity'] -= qty_to_allocate）
        # 所以预测订单自然只能看到剩余库存，无需额外处理
        logger.info('库存已刷新，预测订单将使用剩余库存')

    def _sync_dynamic_data(self, stage_name=""):
        """动态数据同步检查点 - 在策略执行过程中实时感知外部数据变化

        核心问题：当前算法在执行前一次性加载所有数据（静态快照），
        但实际业务中数据是持续变化的。此方法在三阶段的每个关键节点
        被调用，确保分配决策基于最新的真实数据。

        检查并处理的动态变化：
        1. Hold库存到期自动解封 → 解封后的库存补充到 inventory_cache 可用池
        2. 在途库存(transit)到货 → 自动转为本地库存(local)并补充到缓存
        3. 采购订单(PurchaseOrder)完成到货 → 对应物料增加库存
        4. 订单取消/状态变更 → 释放该订单已占用的资源回可用池
        5. 消费规则(ConsumptionRule)变化 → 刷新规则缓存

        Args:
            stage_name: 当前阶段名称（如 "阶段1-分配前"、"阶段2-让料前"、"阶段3-抢占前"）

        Returns:
            dict: 本次同步的变更统计 {
                'hold_released': int,       # Hold解封数量
                'transit_converted': int,   # 在途转本地数量
                'purchase_arrived': int,     # 采购到货数量
                'orders_cancelled': int,    # 取消订单数
                'resources_released': float, # 释放资源总量
                'rules_refreshed': bool,    # 规则是否刷新
                'total_changes': int        # 总变更条目数
            }
        """
        today = date.today()
        stats = {
            'hold_released': 0,
            'transit_converted': 0,
            'purchase_arrived': 0,
            'orders_cancelled': 0,
            'resources_released': 0.0,
            'rules_refreshed': False,
            'total_changes': 0,
        }
        sync_log_entries = []

        try:
            # ========== 1. Hold库存到期解封检测 ==========
            # 场景：Hold库存到了 hold_until 日期后应自动变为可用
            # 问题：load_inventory_cache 时未到期的Hold被跳过了，
            #       但执行期间可能已有新的Hold到期
            from .models import Inventory as InvModel
            newly_released_holds = InvModel.objects.filter(
                is_hold=True,
                hold_until__isnull=False,
                hold_until__lte=today,
                quantity__gt=0
            ).select_related('material')

            for inv in newly_released_holds:
                material_id = inv.material_id
                qty = float(inv.quantity or 0)
                if qty <= 0:
                    continue

                # 检查是否已在缓存中（避免重复添加）
                already_cached = False
                for cached_inv in self.inventory_cache.get(material_id, []):
                    if cached_inv.get('id') == inv.id:
                        already_cached = True
                        break

                if not already_cached:
                    entry = {
                        'id': inv.id,
                        'quantity': qty,
                        'type': inv.inventory_type or 'local',
                        'expiry_date': inv.expiry_date,
                        'warehouse': getattr(inv, 'warehouse', ''),
                        'batch_no': getattr(inv, 'batch_no', ''),
                        'factory_code': getattr(inv, 'factory_code', '') or 'default',
                        'created_at': getattr(inv, 'created_at', None),
                        'is_safety_stock': False,
                        'hold_until': None,  # 已解封
                    }
                    self.inventory_cache[material_id].append(entry)

                    # 更新DB：标记为非Hold
                    inv.is_hold = False
                    inv.hold_quantity = 0
                    inv.save(update_fields=['is_hold', 'hold_quantity', 'updated_at'])

                    stats['hold_released'] += 1
                    stats['total_changes'] += 1
                    sync_log_entries.append(
                        f'Hold解封: 物料{material_id} 数量{qty} (原Hold至{inv.hold_until})'
                    )

            # ========== 2. 在途库存(transit)到货检测 ==========
            # 场景：在途库存的 hold_until（预计到达日）<= 今天，应转为本地可用库存
            transit_arrived = InvModel.objects.filter(
                inventory_type='transit',
                is_hold=False,
                hold_until__isnull=False,
                hold_until__lte=today,
                quantity__gt=0
            ).select_related('material')

            for inv in transit_arrived:
                material_id = inv.material_id
                qty = float(inv.quantity or 0)
                if qty <= 0:
                    continue

                # 检查缓存中是否还有这条transit记录（可能已消耗完）
                still_in_cache = False
                for cached_inv in self.inventory_cache.get(material_id, []):
                    if cached_inv.get('id') == inv.id and cached_inv.get('type') == 'transit':
                        still_in_cache = True
                        # 更新为local类型
                        cached_inv['type'] = 'local'
                        cached_inv['hold_until'] = None
                        break

                if not still_in_cache:
                    # 新增一条local记录
                    entry = {
                        'id': inv.id,
                        'quantity': qty,
                        'type': 'local',  # 从transit转为local
                        'expiry_date': inv.expiry_date,
                        'warehouse': getattr(inv, 'warehouse', ''),
                        'factory_code': getattr(inv, 'factory_code', '') or 'default',
                        'is_safety_stock': False,
                    }
                    self.inventory_cache[material_id].append(entry)

                # 更新DB：转换类型
                inv.inventory_type = 'local'
                inv.hold_until = None
                inv.hold_reason = f'[计划执行中自动转入] transit→local, 到达日:{today}'
                inv.save(update_fields=[
                    'inventory_type', 'hold_until', 'hold_reason', 'updated_at'
                ])

                stats['transit_converted'] += 1
                stats['total_changes'] += 1
                sync_log_entries.append(
                    f'在途到货: 物料{material_id} 数量{qty} (transit→local)'
                )

            # ========== 3. 采购订单到货检测 ==========
            # 场景：PurchaseOrder 状态为 completed/delivered/received 且预计交付日期<=今天
            #         对应物料的库存应该增加
            from .models import PurchaseOrder as POModel
            arrived_pos = POModel.objects.filter(
                status__in=['completed', 'delivered', 'received'],
                delivery_date__lte=today,
                quantity__gt=0
            ).select_related('material')

            # 收集已处理的PO ID（避免重复处理）
            processed_po_ids = getattr(self, '_processed_purchase_order_ids', set())

            for po in arrived_pos:
                if po.id in processed_po_ids:
                    continue

                material_id = po.material_id
                qty = float(po.quantity or 0)
                if qty <= 0:
                    continue

                # 将到货物料加入inventory_cache（使用PO实际数量）
                entry = {
                    'id': f'po_{po.id}',
                    'quantity': qty,
                    'type': 'local',
                    'expiry_date': None,
                    'warehouse': 'MAIN',  # 采购到货默认主仓库
                    'factory_code': 'default',
                    'is_safety_stock': False,
                    'source': 'purchase_order',
                    'po_id': po.id,
                }
                self.inventory_cache[material_id].append(entry)

                processed_po_ids.add(po.id)
                stats['purchase_arrived'] += 1
                stats['total_changes'] += 1
                sync_log_entries.append(
                    f'采购到货: 物料{material_id} 数量{qty} (PO#{po.po_no})'
                )

            self._processed_purchase_order_ids = processed_po_ids

            # ========== 4. 订单取消/状态变更 → 资源释放 ==========
            # 场景：正在执行的订单列表中如果有订单被取消或状态变更为cancelled/delivered
            #         其在 allocation_history 中占用的资源应释放回可用池
            active_order_ids = set()
            if hasattr(self, '_current_orders_list'):
                active_order_ids = {o.id for o in self._current_orders_list}

            if active_order_ids:
                cancelled_allocs = []
                for alloc in list(self.allocation_history):
                    alloc_order_id = alloc.get('order_id')
                    if alloc_order_id and alloc_order_id not in active_order_ids:
                        # 该订单不在活跃列表中（可能已取消），释放其占用
                        if alloc.get('quantity', 0) > 0 and alloc.get('status') != 'released':
                            cancelled_allocs.append(alloc)

                for alloc in cancelled_allocs:
                    material_id = alloc['material_id']
                    released_qty = alloc.get('quantity', 0)

                    # 回补到inventory_cache
                    if material_id in self.inventory_cache:
                        # 找到对应的库存记录并恢复数量
                        target_inv_id = alloc.get('inventory_id')
                        for cached_inv in self.inventory_cache[material_id]:
                            if cached_inv.get('id') == target_inv_id:
                                cached_inv['quantity'] = (cached_inv.get('quantity') or 0) + released_qty
                                break
                        else:
                            # 找不到原记录，新增一条
                            self.inventory_cache[material_id].append({
                                'id': target_inv_id,
                                'quantity': released_qty,
                                'type': alloc.get('type', 'local'),
                                'source': 'released_from_cancelled_order',
                            })

                    # 标记allocation_history中的记录为已释放
                    alloc['quantity'] = 0
                    alloc['status'] = 'released'

                    stats['orders_cancelled'] += 1
                    stats['resources_released'] += released_qty
                    stats['total_changes'] += 1
                    sync_log_entries.append(
                        f'订单取消释放: 订单{alloc["order_id"]} 释放物料{material_id} 数量{released_qty}'
                    )

            # ========== 5. 消费规则变化检测 ==========
            # 场景：ConsumptionRule 在执行过程中可能被管理员修改
            #       需要刷新规则缓存以应用最新规则
            rule_last_check = getattr(self, '_rule_last_check_time', None)
            current_time = datetime.now()
            # 每60秒检查一次规则变化（避免频繁DB查询）
            if rule_last_check is None or (current_time - rule_last_check).total_seconds() > 60:
                from .models import ConsumptionRule
                latest_rule_update = ConsumptionRule.objects.filter(is_active=True).aggregate(
                    latest_update=Max('updated_at')
                )['latest_update']
                cached_rule_time = getattr(self, '_rule_cache_timestamp', None)

                if latest_rule_update and (cached_rule_time is None or latest_rule_update > cached_rule_time):
                    # 规则有更新，重新加载
                    self._consumption_rules_cache = None  # 清空缓存触发重新加载
                    self._rule_cache_timestamp = latest_rule_update
                    stats['rules_refreshed'] = True
                    stats['total_changes'] += 1
                    sync_log_entries.append('消费规则已刷新（检测到规则变更）')

                self._rule_last_check_time = current_time

            # ========== 日志输出 ==========
            if sync_log_entries:
                logger.info(
                    f'[动态数据同步-{stage_name}] 检测到 {stats["total_changes"]} 项变化:\n'
                    + '\n'.join(f'  → {entry}' for entry in sync_log_entries)
                )
                PlanLog.objects.create(
                    log_type='INFO',
                    message=f'动态数据同步[{stage_name}]: '
                            f'Hold解封={stats["hold_released"]}, 在途到货={stats["transit_converted"]}, '
                            f'采购到货={stats["purchase_arrived"]}, 订单取消释放={stats["orders_cancelled"]}'
                )

        except Exception as e:
            logger.warning(f'动态数据同步异常 [{stage_name}]: {e}')
            PlanLog.objects.create(
                log_type='WARNING',
                message=f'动态数据同步失败 [{stage_name}]: {str(e)}'
            )

        return stats

    def _run_planning(self, orders_list):
        """执行物料计划 - 三阶段：分配→让料→抢占

        阶段1：串行分配（保证真正的资源竞争）
        阶段2：串行让料（对缺料订单执行让料逻辑）
        阶段3：抢占（资源集中模式 - 从低优先级订单抢夺物料以完成更多订单）
               核心目标：在现有资源条件下完成最多的订单
               允许抢占包括BOM替代物料的情况

        每个阶段开始前执行动态数据同步（Hold解封/在途到货/采购到货/订单取消/规则变化）
        """
        # 记录当前活跃订单列表（用于动态同步中的订单取消检测）
        self._current_orders_list = orders_list

        # ========== 执行前全局同步 ==========
        pre_sync = self._sync_dynamic_data(stage_name="执行前")

        # 阶段1：串行分配（保证真正的资源竞争）
        results = []
        for idx, order in enumerate(orders_list):
            # 每处理N个订单做一次轻量级同步（避免长时间运行期间数据过期）
            if idx > 0 and idx % 50 == 0:
                self._sync_dynamic_data(stage_name=f"阶段1-分配中(第{idx}单)")
            result = self.process_order(order)
            results.append(result)

        # ========== 阶段1结束后同步 ==========
        sync_after_alloc = self._sync_dynamic_data(stage_name="阶段1结束-阶段2前")
        if sync_after_alloc.get('total_changes', 0) > 0:
            logger.info(f'阶段1后检测到{sync_after_alloc["total_changes"]}项数据变化，将影响后续让料/抢占决策')

        # 阶段2：串行让料（对缺料订单执行让料逻辑）
        for i, result in enumerate(results):
            if not result['is_complete'] and result.get('shortage_details'):
                order = orders_list[i] if i < len(orders_list) else None
                if order:
                    effective_date = result.get('required_date',
                        order.demand_date - timedelta(days=self._get_effective_shipping_days(order) + self.PRODUCTION_LEAD_TIME))
                    for shortage in result['shortage_details']:
                        mid = shortage['material_id']
                        released, release_recs = self.release_material_for_higher_priority(
                            mid, shortage['shortage'], order.id, order.priority,
                            effective_date
                        )
                        if released > 0:
                            result['release_records'] = result.get('release_records', []) + release_recs
                            alloc_mat = result.get('allocated') or {}
                            mid_data = alloc_mat.get(mid) or {}
                            if isinstance(mid_data, dict):
                                mid_data['allocated'] = (mid_data.get('allocated') or 0) + released
                            shortage['shortage'] -= released
                            if shortage['shortage'] <= 0:
                                shortage['shortage'] = 0

        # ========== 阶段2结束后同步 ==========
        sync_after_release = self._sync_dynamic_data(stage_name="阶段2结束-阶段3前")
        if sync_after_release.get('total_changes', 0) > 0:
            logger.info(f'阶段2后检测到{sync_after_release["total_changes"]}项数据变化，新增资源可用于抢占')

        # ========== 阶段3：抢占（资源集中）==========
        # 核心逻辑：对仍未完成的订单，尝试从低优先级已分配订单处抢夺物料
        # 抢夺原则：
        #   1. 只抢夺能使本订单完成（或大幅提升齐套率）的物料
        #   2. 优先从"部分齐套"的低优先级订单抢（避免破坏"完全齐套"订单）
        #   3. 支持BOM替代物料的抢占
        #   4. 如果抢夺会导致被抢订单从"完全齐套"降为"未齐套"，需评估净收益
        preempt_log_total = []
        for i, result in enumerate(results):
            if result.get('is_complete'):
                continue  # 已完成的订单不需要抢占

            order = orders_list[i] if i < len(orders_list) else None
            if not order or not result.get('shortage_details'):
                continue

            effective_date = result.get('required_date',
                order.demand_date - timedelta(days=self._get_effective_shipping_days(order) + self.PRODUCTION_LEAD_TIME))

            for shortage in result['shortage_details']:
                if shortage.get('shortage', 0) <= 0:
                    continue

                mid = shortage['material_id']
                need_qty = shortage['shortage']

                # 尝试直接抢占该物料
                preempted, preempt_recs = self._preempt_material_from_lower_priority(
                    mid, need_qty, order.id, order.priority, results, orders_list, effective_date
                )
                if preempted > 0:
                    self._apply_preempt_result(result, shortage, mid, preempted, preempt_recs)
                    preempt_log_total.extend(preempt_recs)
                    need_qty -= preempted

                # 如果主物料仍不足，尝试抢占替代物料（BOM替代物料组支持）
                if need_qty > 0:
                    alt_group = self._find_alternative_group(
                        getattr(order, 'material_id', None), mid
                    )
                    if alt_group:
                        for alt_mid in alt_group:
                            if alt_mid == mid:
                                continue
                            if need_qty <= 0:
                                break
                            alt_preempted, alt_recs = self._preempt_material_from_lower_priority(
                                alt_mid, need_qty, order.id, order.priority, results, orders_list, effective_date
                            )
                            if alt_preempted > 0:
                                # 替代物料抢占成功，记入结果（作为替代分配）
                                self._apply_alt_preempt_result(result, mid, alt_mid, alt_preempted, alt_recs)
                                preempt_log_total.extend(alt_recs)
                                need_qty -= alt_preempted

        # 重新计算所有受影响订单的齐套状态
        self._recalculate_completion_status(results)

        if preempt_log_total:
            logger.info(f'阶段3抢占完成: 共{len(preempt_log_total)}次抢占操作')

        return results

    def _recalculate_completion_status(self, results):
        """重新计算所有订单的齐套状态（阶段3抢占后调用）

        抢占操作会改变多个订单的分配量，需要统一重算：
        - complete_rate（cap在[0,1]范围内）
        - is_complete / allocation_type 分类
        - shortage_details 缺料明细
        """
        for result in results:
            # 防御: allocated可能为None/list/non-dict
            allocated_materials = result.get('allocated')
            if not isinstance(allocated_materials, dict):
                # 非dict类型(None/list/int等): 视为无分配
                result['complete_rate'] = 0.0
                result['is_complete'] = False
                result['allocation_type'] = 'none'
                result['shortage_details'] = []
                continue

            # 重算总需求和总分配（每物料cap: allocated不超过required）
            total_required = 0.0
            total_allocated_capped = 0.0
            new_shortage_details = []

            for material_id, alloc_data in allocated_materials.items():
                if not isinstance(alloc_data, dict):
                    continue
                req = float(alloc_data.get('required', 0) or 0)
                alloc = float(alloc_data.get('allocated', 0) or 0)
                scrap_rate = alloc_data.get('scrap_rate', 0.0)

                total_required += req
                # cap: 齐套率计算中分配量不超过需求量
                total_allocated_capped += min(alloc, req) if req > 0 else 0

                # 重建缺料明细
                if alloc < req:
                    new_shortage_details.append({
                        'material_id': material_id,
                        'required': req,
                        'allocated': alloc,
                        'shortage': req - alloc,
                        'scrap_rate': scrap_rate,
                    })

            # 重算齐套率（硬上限100%）
            complete_rate = (total_allocated_capped / total_required) if total_required > 0 else 0
            complete_rate = min(complete_rate, 1.0)

            result['complete_rate'] = complete_rate
            result['is_complete'] = complete_rate >= 1.0

            # 重算分类
            if complete_rate >= 1.0:
                result['allocation_type'] = 'complete'
            elif complete_rate > 0:
                result['allocation_type'] = 'partial'
            else:
                result['allocation_type'] = 'none'

            # 更新缺料明细
            result['shortage_details'] = new_shortage_details

    def _preempt_material_from_lower_priority(self, material_id, required_qty, requester_order_id,
                                              requester_priority, results, orders_list, required_date=None):
        """从低优先级订单抢占物料

        抢夺策略（按优先级排序尝试）：
        1. 优先从"部分齐套"订单抢（破坏性最小）
        2. 其次从"未齐套"订单抢
        3. 最后才考虑从"完全齐套"订单抢（需评估净收益）

        Returns:
            (preempted_qty, list_of_preempt_records)
        """
        preempted = 0.0
        preempt_records = []

        if material_id not in self.allocation_history:
            return preempted, preempt_records

        # 收集所有持有该物料的低优先级分配记录
        candidates = []
        for alloc in self.allocation_history:
            if alloc['material_id'] != material_id:
                continue
            if alloc['order_id'] == requester_order_id:
                continue
            if alloc.get('quantity', 0) <= 0:
                continue
            if alloc.get('status') == 'released':
                continue

            # 找到被抢订单的结果，评估其当前状态
            victim_result = None
            victim_order = None
            for idx, r in enumerate(results):
                if r.get('order_id') == alloc['order_id']:
                    victim_result = r
                    victim_order = orders_list[idx] if idx < len(orders_list) else None
                    break

            if not victim_result:
                continue

            victim_priority = getattr(victim_order, 'priority', 99) if victim_order else 99

            # 只抢夺更低优先级的订单
            if victim_priority >= requester_priority:
                continue

            # 评估被抢订单的齐套状态
            victim_is_complete = victim_result.get('is_complete', False)
            victim_complete_rate = victim_result.get('complete_rate', 0)

            # 计算被抢订单对该物料的持有量占其需求的比例
            victim_holding_for_this_mat = alloc.get('quantity', 0)
            victim_shortage_details = victim_result.get('shortage_details') or []

            # 计算抢夺后对被抢订单的影响
            victim_req_for_mat = 0
            victim_alloc_for_mat = 0
            for sd in victim_shortage_details:
                if sd.get('material_id') == material_id:
                    victim_req_for_mat = sd.get('required', 0) or 0
                    victim_alloc_for_mat = sd.get('allocated', 0) or 0
                    break

            # 如果被抢订单完全齐套，计算抢夺后是否会破坏其齐套状态
            will_break_complete = False
            if victim_is_complete and victim_req_for_mat > 0:
                post_alloc = victim_alloc_for_mat - min(victim_holding_for_this_mat, required_qty - preempted)
                if post_alloc < victim_req_for_mat:
                    will_break_complete = True

            # 抢夺优先级分数：部分齐套 > 未齐套 > 完全齐套（但会破坏的排最后）
            if victim_is_complete and will_break_complete:
                preempt_rank = 300  # 最低优先级：会破坏已完成的订单
            elif victim_is_complete:
                preempt_rank = 200  # 抢了也不会破坏完成态（物料有富余）
            elif victim_complete_rate >= 0.5:
                preempt_rank = 100  # 部分齐套
            else:
                preempt_rank = 50   # 未齐套/接近未齐套

            candidates.append({
                'alloc': alloc,
                'victim_result': victim_result,
                'victim_order': victim_order,
                'victim_priority': victim_priority,
                'victim_is_complete': victim_is_complete,
                'will_break_complete': will_break_complete,
                'preempt_rank': preempt_rank,
                'available_qty': alloc.get('quantity', 0),
            })

        # 按抢占优先级排序（rank低的先抢）+ 同rank内按优先级升序（最低优先级的先抢）
        candidates.sort(key=lambda c: (c['preempt_rank'], c['victim_priority']))

        for cand in candidates:
            if preempted >= required_qty:
                break

            qty_to_take = min(cand['available_qty'], required_qty - preempted)

            # 对于"会破坏已完成订单"的情况，额外检查净收益
            if cand['will_break_complete']:
                # 只有当抢夺能让请求者完成时才允许破坏一个已完成订单
                requester_result = None
                for r in results:
                    if r.get('order_id') == requester_order_id:
                        requester_result = r
                        break
                if requester_result:
                    # 检查抢夺后请求者的预期complete_rate
                    current_total_alloc = sum(
                        (sd.get('allocated', 0) or 0) for sd in requester_result.get('shortage_details', [])
                    )
                    new_total_alloc = current_total_alloc + qty_to_take
                    total_required = sum(
                        (sd.get('required', 0) or 0) for sd in requester_result.get('shortage_details', [])
                    )
                    if total_required > 0 and new_total_alloc / total_required < 1.0:
                        # 抢夺后仍无法完成请求者订单，不值得破坏已完成订单
                        continue

            # 执行抢占
            with self.lock:
                cand['alloc']['quantity'] -= qty_to_take
                if cand['alloc']['quantity'] <= 0:
                    cand['alloc']['status'] = 'preempted'

                # 更新被抢订单的分配数据
                victim_sd_list = cand['victim_result'].get('shortage_details') or {}
                if isinstance(victim_sd_list, dict):
                    mat_data = victim_sd_list.get(material_id)
                    if isinstance(mat_data, dict):
                        mat_data['allocated'] = max(0, (mat_data.get('allocated') or 0) - qty_to_take)
                        mat_data['shortage'] = (mat_data.get('shortage') or 0) + qty_to_take

                self.order_promise_changes[cand['alloc']['order_id']] += 1

            rec = {
                'from_order_id': cand['alloc']['order_id'],
                'to_order_id': requester_order_id,
                'material_id': material_id,
                'preempted_quantity': qty_to_take,
                'victim_priority': cand['victim_priority'],
                'requester_priority': requester_priority,
                'victim_was_complete': cand['victim_is_complete'],
                'will_break_complete': cand['will_break_complete'],
                'reason': f'抢占: 从P{cand["victim_priority"]}订单抢夺物料给P{requester_priority}订单',
                'preempt_type': 'resource_concentration'
            }

            try:
                PlanLog.objects.create(
                    log_type='WARNING',
                    message=f'物料抢占: 订单{cand["alloc"]["order_id"]}({("已完成" if cand["victim_is_complete"] else "未完成")})'
                            f'被抢夺物料{material_id}数量{qty_to_take} → 订单{requester_order_id}'
                )
            except Exception:
                pass

            preempt_records.append(rec)
            preempted += qty_to_take

        return preempted, preempt_records

    def _apply_preempt_result(self, result, shortage, material_id, preempted_qty, preempt_recs):
        """将抢占结果应用到目标订单"""
        alloc_mat = result.get('allocated') or {}
        mid_data = alloc_mat.get(material_id) or {}
        if isinstance(mid_data, dict):
            mid_data['allocated'] = (mid_data.get('allocated') or 0) + preempted_qty
        shortage['shortage'] = max(0, shortage.get('shortage', 0) - preempted_qty)
        result['preempt_records'] = result.get('preempt_records', []) + preempt_recs

    def _apply_alt_preempt_result(self, result, original_mid, alt_material_id, preempted_qty, preempt_recs):
        """将替代物料抢占结果应用到目标订单"""
        # 在分配结果中记录替代物料分配
        alt_alloc = result.get('alternative_allocated') or {}
        alt_data = alt_alloc.get(original_mid) or {}
        if isinstance(alt_data, dict):
            alt_data[alt_material_id] = (alt_data.get(alt_material_id) or 0) + preempted_qty
        else:
            alt_alloc[original_mid] = {alt_material_id: preempted_qty}
        result['alternative_allocated'] = alt_alloc
        result['preempt_records'] = result.get('preempt_records', []) + preempt_recs

    def _save_planning_results(self, results):
        """保存计划结果 - 批量优化"""
        if not results:
            return

        # 预加载所有相关订单（1次查询）
        order_ids = [r['order_id'] for r in results if r.get('order_id')]
        orders_map = {
            o.id: o for o in SalesOrder.objects.select_related('material').filter(id__in=order_ids)
        }

        # 读取旧的交期变更次数（用于跨运行累加）
        old_change_counts = {}
        if order_ids:
            old_results = MaterialPlanResult.objects.filter(order_id__in=order_ids).values('order_id', 'delivery_change_count')
            for r in old_results:
                old_change_counts[r['order_id']] = r['delivery_change_count']

        # 批量更新订单状态已移除（不再回写SalesOrder.status）
        plan_results_to_create = []
        allocations_to_create = []
        logs_to_create = []

        for result in results:
            order = orders_map.get(result['order_id'])
            if not order:
                continue

            # 修复: 不再修改SalesOrder.status到DB（避免影响下次查询范围）
            # 原逻辑会将pending→complete/partial，导致视图下次查询时这些订单被排除
            # 订单状态应仅作为本次计划结果记录在MaterialPlanResult中，不回写原表
            # bulk_orders.append(order)  # 不再将order加入批量更新列表

            # 准备 MaterialPlanResult 数据（旧交期变更次数 + 本次新增）
            old_change_count = old_change_counts.get(order.id, 0)
            new_change_count = self.order_promise_changes.get(order.id, 0)
            plan_results_to_create.append(MaterialPlanResult(
                order=order,
                is_complete=result['is_complete'],
                # 修复: 写入DB前cap complete_rate在[0,1]范围（替代料bom_quantity放大修正）
                complete_rate=min(max(result.get('complete_rate', 0) or 0, 0.0), 1.0),
                shortage_details=str(result['shortage_details']) if result['shortage_details'] else None,
                allocation_details=str(result.get('allocated', {})),
                is_early_delivery=result.get('is_early_delivery', False),
                transfer_details=result.get('transfer_details') if result.get('transfer_details') else None,
                delivery_change_count=old_change_count + new_change_count,
            ))

            # 准备 OrderAllocation 数据
            allocated = result.get('allocated') or {}
            if not isinstance(allocated, dict):
                allocated = {}
            for material_id, alloc_data in allocated.items():
                if not isinstance(alloc_data, dict):
                    continue
                allocations_list = alloc_data.get('allocations') or []
                for alloc in allocations_list:
                    if not isinstance(alloc, dict):
                        continue
                    # inventory_id 必须是整数或None（PO在途库存的ID是'po_xxx'字符串，需转为None）
                    _raw_inv_id = alloc.get('inventory_id')
                    try:
                        _safe_inv_id = int(_raw_inv_id) if _raw_inv_id is not None else None
                    except (ValueError, TypeError):
                        _safe_inv_id = None
                    allocations_to_create.append(OrderAllocation(
                        order=order,
                        material_id=material_id,
                        allocated_quantity=alloc.get('quantity', 0),
                        required_quantity=alloc_data.get('required', 0),
                        inventory_id=_safe_inv_id,
                        is_alternative=alloc.get('is_alternative', False),
                        reliability_factor=alloc.get('reliability_factor', 1.0)
                    ))

            # 准备 PlanLog 数据
            logs_to_create.append(PlanLog(
                log_type='PLANNING',
                order_id=order.id,
                message=f'物料计划完成: 齐套率={result["complete_rate"]:.0%}, 类型={result["allocation_type"]}'
            ))

        # 批量执行数据库操作
        try:
            with transaction.atomic():
                # 修复: 不再批量更新SalesOrder.status（避免影响下次查询范围）
                # 订单状态仅记录在MaterialPlanResult.is_complete/complete_rate中

                # 先删除旧的分配记录和计划结果
                if order_ids:
                    OrderAllocation.objects.filter(order_id__in=order_ids).delete()
                    MaterialPlanResult.objects.filter(order_id__in=order_ids).delete()

                # 批量创建
                if plan_results_to_create:
                    MaterialPlanResult.objects.bulk_create(plan_results_to_create, batch_size=500)
                if allocations_to_create:
                    OrderAllocation.objects.bulk_create(allocations_to_create, batch_size=500)
                if logs_to_create:
                    PlanLog.objects.bulk_create(logs_to_create, batch_size=500)
        except Exception as e:
            logger.error(f"批量保存计划结果错误: {e}")

    def _save_transfer_records(self, results):
        """保存跨工厂调拨记录到数据库 - 批量优化"""
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')

        # 收集所有调拨记录
        transfer_details_all = []
        for result in results:
            transfer_details = result.get('transfer_details', [])
            if not transfer_details:
                continue
            for rec in transfer_details:
                transfer_details_all.append((result['order_id'], rec))

        if not transfer_details_all:
            return

        # 批量查询物料和订单（避免逐条查询）
        material_ids = list({rec['material_id'] for _, rec in transfer_details_all})
        order_ids = list({order_id for order_id, _ in transfer_details_all})
        materials_map = {m.id: m for m in Material.objects.filter(id__in=material_ids)}
        orders_map = {o.id: o for o in SalesOrder.objects.filter(id__in=order_ids)}

        # 构建批量创建对象
        transfer_objects = []
        for transfer_seq, (order_id, rec) in enumerate(transfer_details_all, 1):
            material = materials_map.get(rec['material_id'])
            if not material:
                continue
            order_obj = orders_map.get(order_id)
            expected_arrival = date.today() + timedelta(days=rec.get('transfer_days', self.DEFAULT_TRANSFER_DAYS))

            transfer_objects.append(FactoryTransfer(
                transfer_no=f'TF-{timestamp}-{transfer_seq:04d}',
                material=material,
                from_factory=rec['from_factory'],
                to_factory=rec['to_factory'],
                quantity=rec['quantity'],
                transfer_days=rec.get('transfer_days', self.DEFAULT_TRANSFER_DAYS),
                transfer_cost=rec.get('transfer_cost', 0),
                status='approved',
                related_order=order_obj,
                expected_arrival_date=expected_arrival,
                reason=rec.get('reason', '')
            ))

        # 批量创建
        try:
            with transaction.atomic():
                if transfer_objects:
                    FactoryTransfer.objects.bulk_create(transfer_objects, batch_size=500)
        except Exception as e:
            logger.error(f"批量保存调拨记录失败: {e}")

        total_transfers = len(transfer_objects)
        if total_transfers > 0:
            PlanLog.objects.create(
                log_type='INFO',
                message=f'本次计划生成{total_transfers}条工厂调拨记录'
            )

    def _record_promise_changes(self, results):
        """记录交期变更 - 仅更新内存中的计数，持久化由 _save_planning_results 统一处理"""
        for result in results:
            if result['shortage_details']:
                self.order_promise_changes[result['order_id']] += 1

    def _enforce_delivery_change_constraint(self, results):
        """强制执行交期变更<2次约束 - 超限订单标记为风险并触发预警"""
        violation_orders = []
        for result in results:
            change_count = self.order_promise_changes.get(result['order_id'], 0)
            if change_count >= 2:
                violation_orders.append({
                    'order_id': result['order_id'],
                    'order_no': result.get('order_no', ''),
                    'change_count': change_count,
                    'priority': 0,
                    'complete_rate': result.get('complete_rate', 0)
                })
                order_obj = SalesOrder.objects.select_related('material').filter(id=result['order_id']).first()
                if order_obj:
                    violation_orders[-1]['priority'] = order_obj.priority
                    PlanLog.objects.create(
                        log_type='ERROR',
                        message=f'交期变更超限: 订单{result.get("order_no", "")}已变更{change_count}次(限制<2次)，优先级{order_obj.priority}'
                    )

        if violation_orders:
            violation_orders.sort(key=lambda x: (-x['priority'], -x['complete_rate']))
            PlanLog.objects.create(
                log_type='WARNING',
                message=f'共{len(violation_orders)}个订单交期变更次数达到上限，需人工介入: {", ".join([v["order_no"] for v in violation_orders[:5]])}'
            )

            # 用dict查找代替嵌套循环 O(n) 替代 O(n²)
            results_by_order = {r['order_id']: r for r in results}
            for v in violation_orders:
                r = results_by_order.get(v['order_id'])
                if r:
                    r['delivery_violation'] = True
                    r['violation_details'] = {
                        'change_count': v['change_count'],
                        'limit': 2,
                        'severity': 'critical' if v['change_count'] >= 3 else 'warning'
                    }

        return violation_orders

    def check_delivery_change_alerts(self):
        """检查交期变化预警 - 当交期变化次数>=2时自动发出预警通知"""
        alerts = []
        today = date.today()

        # 1. 查询交期变更次数>=2的物料计划结果
        alert_results = MaterialPlanResult.objects.filter(
            delivery_change_count__gte=2
        ).select_related('order')

        for plan_result in alert_results:
            order = plan_result.order
            if not order:
                continue

            # 避免重复通知：检查是否已存在未读的同类预警
            existing = Notification.objects.filter(
                title=f'交期变更预警 - {order.order_no}',
                notification_type='warning',
                is_read=False,
            ).exists()
            if existing:
                continue

            Notification.objects.create(
                title=f'交期变更预警 - {order.order_no}',
                message=(
                    f'订单 {order.order_no} 交期变更已达 {plan_result.delivery_change_count} 次（限制2次）。\n'
                    f'客户名称：{order.customer_name}\n'
                    f'需求日期：{order.demand_date}\n'
                    f'齐套率：{plan_result.complete_rate:.0%}\n'
                    f'请及时跟进处理。'
                ),
                notification_type='warning',
            )

            alerts.append({
                'type': 'delivery_change',
                'order_no': order.order_no,
                'change_count': plan_result.delivery_change_count,
                'customer_name': order.customer_name,
                'demand_date': str(order.demand_date),
            })

        # 2. 查询采购订单中实际交期晚于预计交期的记录
        late_pos = PurchaseOrder.objects.filter(
            actual_delivery_date__isnull=False,
            actual_delivery_date__gt=F('delivery_date'),
            status__in=['completed', 'partial', 'partial_shipped', 'shipped']
        ).select_related('supplier', 'material')

        for po in late_pos:
            delay_days = (po.actual_delivery_date - po.delivery_date).days

            existing = Notification.objects.filter(
                title=f'采购交期延迟预警 - {po.po_no}',
                notification_type='warning',
                is_read=False,
            ).exists()
            if existing:
                continue

            Notification.objects.create(
                title=f'采购交期延迟预警 - {po.po_no}',
                message=(
                    f'采购订单 {po.po_no} 实际交期晚于预计交期 {delay_days} 天。\n'
                    f'供应商：{po.supplier.supplier_name}\n'
                    f'物料：{po.material.material_name}({po.material.material_code})\n'
                    f'预计交付：{po.delivery_date}，实际交付：{po.actual_delivery_date}\n'
                    f'请关注供应商交期表现。'
                ),
                notification_type='warning',
            )

            alerts.append({
                'type': 'late_delivery',
                'po_no': po.po_no,
                'supplier_name': po.supplier.supplier_name,
                'delay_days': delay_days,
                'delivery_date': str(po.delivery_date),
                'actual_delivery_date': str(po.actual_delivery_date),
            })

        if alerts:
            logger.info(f'交期变化预警: 共生成 {len(alerts)} 条预警通知')

        return {
            'total_alerts': len(alerts),
            'delivery_change_alerts': len([a for a in alerts if a['type'] == 'delivery_change']),
            'late_delivery_alerts': len([a for a in alerts if a['type'] == 'late_delivery']),
            'details': alerts,
        }

    def get_supplier_delivery_performance(self):
        """统计每个供应商的交期表现 - 承诺次数、延迟次数、延迟率、平均延迟天数"""
        # 获取所有有实际交期的采购订单
        completed_pos = PurchaseOrder.objects.filter(
            actual_delivery_date__isnull=False,
        ).select_related('supplier').values(
            'supplier_id', 'supplier__supplier_code', 'supplier__supplier_name',
            'delivery_date', 'actual_delivery_date'
        )

        supplier_stats = defaultdict(lambda: {
            'supplier_id': None,
            'supplier_code': '',
            'supplier_name': '',
            'commit_count': 0,
            'late_count': 0,
            'total_delay_days': 0,
        })

        for po in completed_pos:
            sid = po['supplier_id']
            stats = supplier_stats[sid]
            stats['supplier_id'] = sid
            stats['supplier_code'] = po['supplier__supplier_code']
            stats['supplier_name'] = po['supplier__supplier_name']
            stats['commit_count'] += 1
            actual_date = po.get('actual_delivery_date')
            delivery_date = po.get('delivery_date')
            if actual_date and delivery_date and actual_date > delivery_date:
                stats['late_count'] += 1
                stats['total_delay_days'] += (actual_date - delivery_date).days

        performance_list = []
        for sid, stats in supplier_stats.items():
            commit_count = stats['commit_count']
            late_count = stats['late_count']
            delay_rate = late_count / commit_count if commit_count > 0 else 0
            avg_delay_days = stats['total_delay_days'] / late_count if late_count > 0 else 0
            performance_list.append({
                'supplier_id': stats['supplier_id'],
                'supplier_code': stats['supplier_code'],
                'supplier_name': stats['supplier_name'],
                'commit_count': commit_count,
                'late_count': late_count,
                'delay_rate': round(delay_rate, 4),
                'avg_delay_days': round(avg_delay_days, 1),
            })

        # 按延迟率降序排序
        performance_list.sort(key=lambda x: x['delay_rate'], reverse=True)
        return performance_list

    def optimize_inventory_jit(self, results):
        """JIT库存水位优化 - 基于需求预测动态调整安全库存和采购时机"""
        optimization_suggestions = []
        
        material_demand_map = defaultdict(float)
        material_lead_times = {}
        
        for result in results:
            for mid, alloc_data in result.get('allocated', {}).items():
                required = alloc_data.get('required', 0)
                allocated = alloc_data.get('allocated', 0)
                material_demand_map[mid] += max(0, required - allocated)
                
                material_info = self.material_info_cache.get(mid, {})
                if mid not in material_lead_times and material_info.get('lead_time'):
                    material_lead_times[mid] = material_info['lead_time']
        
        today = date.today()
        
        for material_id, total_demand in material_demand_map.items():
            if total_demand <= 0:
                continue
                
            material_info = self.material_info_cache.get(material_id, {})
            current_stock = sum(i.get('quantity', 0) for i in self.inventory_cache.get(material_id, []))
            safety_stock = int(material_info.get('safety_stock', 0))
            lead_time = material_lead_times.get(material_id, material_info.get('lead_time', 7))
            standard_cost = material_info.get('standard_cost', 0)
            
            daily_demand_estimate = total_demand / 30
            jit_safety_stock = daily_demand_estimate * lead_time * 1.2
            
            excess_stock = max(0, current_stock - (total_demand + jit_safety_stock))
            holding_cost_excess = excess_stock * standard_cost * 0.02
            
            if safety_stock > jit_safety_stock and safety_stock > 0:
                reduction_ratio = (safety_stock - jit_safety_stock) / safety_stock
                if reduction_ratio > 0.15:
                    optimization_suggestions.append({
                        'material_id': material_id,
                        'type': 'safety_stock_reduction',
                        'current_safety_stock': safety_stock,
                        'recommended_jit_level': round(jit_safety_stock, 2),
                        'reduction_pct': round(reduction_ratio * 100, 1),
                        'estimated_saving_monthly': round(holding_cost_excess, 2),
                        'rationale': f'基于日均需求{daily_demand_estimate:.1f}和交期{lead_time}天计算',
                        'priority': 'high' if reduction_ratio > 0.3 else 'medium'
                    })
                    
                    PlanLog.objects.create(
                        log_type='INFO',
                        message=f'JIT优化建议: 物料{material_id}安全库存可从{safety_stock}降至{jit_safety_stock:.1f}(降{reduction_ratio*100:.0f}%)'
                    )
            
            if excess_stock > 0 and standard_cost > 0:
                days_of_supply = current_stock / daily_demand_estimate if daily_demand_estimate > 0 else 999
                if days_of_supply > 60:
                    optimization_suggestions.append({
                        'material_id': material_id,
                        'type': 'excess_inventory',
                        'current_stock': current_stock,
                        'days_of_supply': round(days_of_supply, 0),
                        'excess_quantity': round(excess_stock, 2),
                        'estimated_holding_cost_monthly': round(holding_cost_excess, 2),
                        'recommended_action': '暂停采购或寻找出库渠道消化库存',
                        'priority': 'high' if days_of_supply > 90 else 'medium'
                    })
        
        optimization_suggestions.sort(key=lambda x: (
            {'high': 0, 'medium': 1, 'low': 2}.get(x.get('priority', 'low'), 2),
            -x.get('estimated_saving_monthly', 0) - x.get('estimated_holding_cost_monthly', 0)
        ))
        
        return {
            'suggestions': optimization_suggestions,
            'total_suggestions': len(optimization_suggestions),
            'total_estimated_monthly_saving': round(sum(s.get('estimated_saving_monthly', 0) + s.get('estimated_holding_cost_monthly', 0) for s in optimization_suggestions), 2),
            'high_priority_count': sum(1 for s in optimization_suggestions if s.get('priority') == 'high')
        }

    def get_planning_summary(self, results):
        """获取计划汇总统计 - 包含更多指标"""
        if not results:
            return {
                'total_orders': 0,
                'complete_orders': 0,
                'partial_orders': 0,
                'pending_orders': 0,
                'avg_complete_rate': 0.0,
                'complete_rate': 0.0,
                'total_shortage_orders': 0,
                'total_promise_changes': 0,
                'stable_orders': 0,
                'avg_supplier_reliability': 0.0,
                'total_safety_stock_usage': 0,
                'failure_analysis': {
                    'total_failed': 0,
                    'by_reason': {
                        'capacity_constraint': 0,
                        'material_shortage': 0,
                        'forbidden_material': 0,
                        'safety_stock_insufficient': 0,
                        'supplier_unavailable': 0,
                        'other': 0
                    },
                    'details': {
                        'capacity_constraint': [],
                        'material_shortage': [],
                        'forbidden_material': [],
                        'safety_stock_insufficient': [],
                        'supplier_unavailable': [],
                        'other': []
                    }
                }
            }

        # ========== 统一的分类阈值 ==========
        # 所有策略使用相同的分类阈值，策略差异只体现在订单排序上
        # 完全齐套：分配率 >= 100%
        # 部分齐套：分配率 > 0 且 < 100%
        # 未齐套：分配率 = 0
        complete_threshold = 1.0  # 统一阈值：100%才算完全齐套

        total_orders = len(results)
        # 使用统一阈值进行分类
        complete_orders = sum(1 for r in results if (r.get('complete_rate') or 0) >= complete_threshold)
        partial_orders = sum(1 for r in results if 0 < (r.get('complete_rate') or 0) < complete_threshold)
        pending_orders = sum(1 for r in results if (r.get('complete_rate') or 0) == 0)

        # 需求满足率 = 已满足需求量 / 总需求量
        # 修复: 替代料分配时allocated可能远超required，需每订单cap后再累加
        total_demand_qty = 0.0
        satisfied_demand_qty = 0.0
        for r in results:
            # 从 allocated_materials 计算总需求量和已满足量
            allocated_materials = r.get('allocated', {})
            if not isinstance(allocated_materials, dict):
                allocated_materials = {}
            req_qty = sum(float(ad.get('required', 0) or 0) for ad in allocated_materials.values() if isinstance(ad, dict))
            # 每物料cap: 已分配量不超过需求量（替代料bom_quantity倍数放大修正）
            alloc_qty = sum(min(float(ad.get('allocated', 0) or 0), float(ad.get('required', 0) or 0))
                           for ad in allocated_materials.values() if isinstance(ad, dict))
            total_demand_qty += req_qty
            satisfied_demand_qty += alloc_qty

        avg_complete_rate = (satisfied_demand_qty / total_demand_qty * 100) if total_demand_qty > 0 else 0
        # 硬上限: 平均齐套率最大为100%
        avg_complete_rate = min(avg_complete_rate, 100.0)

        total_shortage_orders = sum(1 for r in results if r.get('shortage_details'))
        total_promise_changes = sum(self.order_promise_changes.get(r.get('order_id'), 0) for r in results)

        total_safety_stock_usage = 0
        total_reliability_factor = 0
        reliability_count = 0

        for result in results:
            allocated = result.get('allocated') or {}
            if not isinstance(allocated, dict):
                continue
            for alloc_data in allocated.values():
                if not isinstance(alloc_data, dict):
                    continue
                allocations = alloc_data.get('allocations') or []
                for alloc in allocations:
                    total_reliability_factor += alloc.get('reliability_factor', 1.0)
                    reliability_count += 1
                    if alloc.get('is_safety_stock', False):
                        total_safety_stock_usage += alloc.get('quantity', 0)

        avg_reliability_factor = total_reliability_factor / reliability_count if reliability_count > 0 else 0

        failure_analysis = self._analyze_failure_reasons(results)

        # 跨工厂调拨统计
        total_transfer_orders = sum(1 for r in results if r.get('transfer_details'))
        total_transfer_qty = sum(
            sum(rec['quantity'] for rec in r.get('transfer_details', []))
            for r in results
        )
        total_transfer_cost = sum(
            sum(rec.get('transfer_cost', 0) for rec in r.get('transfer_details', []))
            for r in results
        )

        # 提前交货统计
        early_delivery_orders = sum(1 for r in results if r.get('is_early_delivery'))

        return {
            'total_orders': total_orders,
            'complete_orders': complete_orders,
            'partial_orders': partial_orders,
            'pending_orders': pending_orders,
            'avg_complete_rate': avg_complete_rate,
            'complete_rate': complete_orders / total_orders if total_orders > 0 else 0,
            'total_shortage_orders': total_shortage_orders,
            'total_promise_changes': total_promise_changes,
            'stable_orders': sum(1 for r in results if self.order_promise_changes.get(r['order_id'], 0) < 2),
            'avg_supplier_reliability': avg_reliability_factor,
            'total_safety_stock_usage': total_safety_stock_usage,
            'failure_analysis': failure_analysis,
            'transfer_summary': {
                'total_transfer_orders': total_transfer_orders,
                'total_transfer_quantity': total_transfer_qty,
                'total_transfer_cost': round(total_transfer_cost, 2),
            },
            'early_delivery_orders': early_delivery_orders,
        }

    def _analyze_failure_reasons(self, results):
        """分析未达成订单的原因 - 批量优化"""
        failure_reasons = {
            'capacity_constraint': [],
            'material_shortage': [],
            'forbidden_material': [],
            'safety_stock_insufficient': [],
            'supplier_unavailable': [],
            'other': []
        }

        # 预加载所有失败订单的信息（1次查询）
        # 修复: 使用.get()避免result缺少is_complete键时KeyError
        failed_results = [r for r in results if not r.get('is_complete', False)]
        if not failed_results:
            return {'total_failed': 0, 'by_reason': {k: 0 for k in failure_reasons}, 'details': failure_reasons}

        failed_order_ids = [r['order_id'] for r in failed_results]
        orders_map = {
            o.id: o for o in SalesOrder.objects.filter(id__in=failed_order_ids).only('id', 'priority', 'demand_date')
        }

        # 预加载所有禁用料（1次查询）
        forbidden_materials = set(
            SupplierMaterial.objects.filter(is_forbidden=True).values_list('material_id', flat=True)
        )

        for result in failed_results:
            order_obj = orders_map.get(result.get('order_id'))
            order_info = {
                'order_id': result.get('order_id'),
                'order_no': result.get('order_no', '?'),
                'priority': order_obj.priority if order_obj else 0,
                'demand_date': order_obj.demand_date.strftime('%Y-%m-%d') if order_obj else '',
                'complete_rate': result.get('complete_rate', 0)
            }

            if result.get('failure_reason') and '产能约束' in (result.get('failure_reason') or {}):
                failure_reasons['capacity_constraint'].append(order_info)
            elif result.get('shortage_details'):
                sd = result['shortage_details']
                has_forbidden = any(s.get('material_id') in forbidden_materials for s in sd)
                has_safety_stock = any(
                    s.get('shortage', 0) > self.material_info_cache.get(s.get('material_id'), {}).get('safety_stock', 0)
                    for s in sd
                )

                if has_forbidden:
                    order_info['shortage_materials'] = [s.get('material_id') for s in sd]
                    failure_reasons['forbidden_material'].append(order_info)
                elif has_safety_stock:
                    failure_reasons['safety_stock_insufficient'].append(order_info)
                else:
                    order_info['shortage_materials'] = [s.get('material_id') for s in sd]
                    failure_reasons['material_shortage'].append(order_info)
            else:
                failure_reasons['other'].append(order_info)

        summary = {
            'total_failed': sum(len(v) for v in failure_reasons.values()),
            'by_reason': {
                reason: len(orders) for reason, orders in failure_reasons.items()
            },
            'details': failure_reasons
        }

        return summary

    def cancel_order_release_materials(self, order_id):
        """砍单物料自动释放 - 订单取消时释放已占物料分配，恢复库存可用数量"""
        try:
            order = SalesOrder.objects.get(id=order_id)
        except SalesOrder.DoesNotExist:
            return {'success': False, 'message': f'订单ID={order_id}不存在'}

        allocations = OrderAllocation.objects.filter(order_id=order_id)
        if not allocations.exists():
            # 即使没有分配记录，也记录日志
            PlanLog.objects.create(
                log_type='INFO',
                message=f'砍单释放: 订单{order.order_no}(ID={order_id})无物料分配记录，跳过释放',
                order_id=order_id
            )
            return {
                'success': True,
                'message': f'订单{order.order_no}无物料分配记录',
                'released_materials': [],
                'total_released': 0
            }

        released_materials = []
        total_released = 0

        with transaction.atomic():
            for alloc in allocations:
                material_id = alloc.material_id
                released_qty = alloc.allocated_quantity

                if released_qty <= 0:
                    continue

                material = alloc.material
                material_code = material.material_code if material else str(material_id)

                # 将释放数量恢复到Inventory的quantity
                # save()会自动重算 available_quantity = quantity - hold_quantity - locked_quantity
                # 优先恢复到原分配的库存记录（通过inventory_id）
                restored = False
                if alloc.inventory_id:
                    try:
                        inv = Inventory.objects.select_for_update().get(id=alloc.inventory_id)
                        inv.quantity += released_qty
                        inv.save(update_fields=['quantity', 'available_quantity', 'updated_at'])
                        restored = True
                    except Inventory.DoesNotExist:
                        pass

                # 若原库存记录不存在，恢复到该物料的第一条匹配库存
                if not restored:
                    inv = Inventory.objects.select_for_update().filter(
                        material_id=material_id
                    ).first()
                    if inv:
                        inv.quantity += released_qty
                        inv.save(update_fields=['quantity', 'available_quantity', 'updated_at'])
                        restored = True

                # 记录释放信息
                released_materials.append({
                    'material_id': material_id,
                    'material_code': material_code,
                    'material_name': material.material_name if material else '',
                    'released_quantity': released_qty,
                    'required_quantity': alloc.required_quantity,
                    'shortage_quantity': alloc.shortage_quantity,
                    'inventory_restored': restored
                })
                total_released += released_qty

                # 记录计划日志
                PlanLog.objects.create(
                    log_type='WARNING',
                    message=f'砍单释放: 订单{order.order_no}释放物料{material_code}数量{released_qty}，库存已恢复',
                    order_id=order_id,
                    material_id=material_id
                )

            # 删除该订单的所有分配记录
            allocations.delete()

        # 创建通知
        material_summary = '、'.join(
            [f"{m['material_code']}({m['released_quantity']})" for m in released_materials]
        )
        Notification.objects.create(
            title='物料释放通知',
            message=f'订单{order.order_no}已取消，自动释放物料: {material_summary}，共释放{total_released}件',
            notification_type='warning'
        )

        logger.info(f'砍单释放完成: 订单{order.order_no}，释放{len(released_materials)}种物料，共{total_released}件')

        return {
            'success': True,
            'message': f'订单{order.order_no}物料释放完成',
            'order_no': order.order_no,
            'released_materials': released_materials,
            'total_released': total_released
        }

    def process_transit_inventory(self, material_id=None):
        """
        处理在途库存转入可用库存的逻辑

        当在途库存(transit type)的预计到达日期 <= 今天时，
        自动将该批库存从 transit 类型转换为 local 类型。

        同时更新相关的 OrderAllocation 记录，
        使之前因等待在途库存而处于 partial 状态的订单
        可以重新评估是否可以完全齐套。

        注意：Inventory模型没有独立的expected_arrival_date字段，
        对于transit类型的库存，使用hold_until字段作为预计到达日期。
        这是因为在途库存在运输过程中会被标记为hold状态，
        hold_until表示预计到货/解冻日期。

        Args:
            material_id: 可选，指定物料ID。若为None则处理所有物料的在途库存

        Returns:
            dict: {
                'converted_count': int,      # 转换的库存记录数
                'converted_quantity': int,   # 转换的总数量
                'affected_orders': int,      # 受影响的订单数
                'upgraded_to_complete': int, # 从部分齐套升级到完全齐套的订单数
                'details': [...]             # 每条转换的详情
            }
        """
        today = date.today()
        converted_count = 0
        converted_quantity = 0
        details = []
        affected_order_ids = set()

        try:
            # 1. 查询符合条件的在途库存
            transit_query = Inventory.objects.filter(
                inventory_type='transit',
                is_hold=False
            )

            if material_id:
                transit_query = transit_query.filter(material_id=material_id)

            # 使用hold_until作为预计到达日期（对于在途库存，这表示预计到货日期）
            transit_inventories = transit_query.filter(
                hold_until__isnull=False,
                hold_until__lte=today
            ).select_related('material')

            logger.info(f'查询到 {len(transit_inventories)} 条待转换的在途库存记录')

            # 2. 转换已到期的在途库存
            with transaction.atomic():
                for inv in transit_inventories:
                    old_type = inv.inventory_type
                    old_qty = inv.quantity
                    material_code = inv.material.material_code if inv.material else str(inv.material_id)

                    # a. 将inventory_type改为'local'
                    inv.inventory_type = 'local'

                    # b. 清空transit相关的临时字段
                    inv.hold_until = None
                    inv.hold_reason = f'[自动转入] 从{old_type}类型转换为local类型，原预计到达日: {today}'
                    inv.is_hold = False
                    inv.hold_quantity = 0

                    # 保存更改（save()会自动重算available_quantity）
                    inv.save(update_fields=[
                        'inventory_type', 'hold_until', 'hold_reason',
                        'is_hold', 'hold_quantity', 'available_quantity', 'updated_at'
                    ])

                    converted_count += 1
                    converted_quantity += int(old_qty or 0)

                    detail = {
                        'inventory_id': inv.id,
                        'material_id': inv.material_id,
                        'material_code': material_code,
                        'quantity': int(old_qty or 0),
                        'from_type': old_type,
                        'to_type': 'local',
                        'original_expected_date': str(today),
                        'converted_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                    details.append(detail)

                    # c. 记录转换日志到PlanLog
                    PlanLog.objects.create(
                        log_type='INFO',
                        message=f'在途库存自动转入: 物料{material_code}(ID={inv.material_id}) '
                               f'数量{old_qty}从{old_type}转为local类型',
                        material_id=inv.material_id
                    )

                    logger.info(f'转换在途库存: {material_code} 数量{old_qty} {old_type}->local')

            # 3. 查找受影响的OrderAllocation（shortage_quantity > 0的订单）
            affected_material_ids = list(set([d['material_id'] for d in details]))

            if affected_material_ids:
                # 查找有缺料的订单分配记录
                affected_allocations = OrderAllocation.objects.filter(
                    material_id__in=affected_material_ids,
                    shortage_quantity__gt=0
                ).select_related('order', 'material')

                affected_order_ids = set(alloc.order_id for alloc in affected_allocations)

                logger.info(f'发现 {len(affected_order_ids)} 个受影响的订单可能因在途库存转入而改善')

            # 4. 尝试重新分配新转为可用的库存（可选优化步骤）
            upgraded_to_complete = 0
            if affected_order_ids and converted_quantity > 0:
                try:
                    # 重新加载受影响订单并尝试重新评估
                    from .models import SalesOrder, MaterialPlanResult
                    affected_orders = SalesOrder.objects.filter(
                        id__in=list(affected_order_ids),
                        status__in=['partial', 'allocated']
                    )

                    for order in affected_orders:
                        # 检查该订单是否现在可以完全齐套
                        order_allocs = OrderAllocation.objects.filter(order_id=order.id)
                        total_shortage = sum(alloc.shortage_quantity for alloc in order_allocs)

                        if total_shortage <= 0:
                            # 所有物料都已满足，升级状态
                            old_status = order.status
                            order.status = 'complete'
                            order.save(update_fields=['status', 'updated_at'])

                            # 更新计划结果
                            plan_result, _ = MaterialPlanResult.objects.update_or_create(
                                order=order,
                                defaults={
                                    'is_complete': True,
                                    'complete_rate': 1.0,
                                    'updated_at': datetime.now()
                                }
                            )

                            PlanLog.objects.create(
                                log_type='INFO',
                                message=f'订单{order.order_no}(ID={order.id})因在途库存转入而从'
                                       f'{old_status}升级为完全齐套(complete)',
                                order_id=order.id
                            )

                            upgraded_to_complete += 1
                            logger.info(f'订单{order.order_no}升级为完全齐套')

                except Exception as e:
                    logger.warning(f'重新评估受影响订单时出错: {str(e)}')
                    # 不中断主流程，继续返回结果

            result = {
                'converted_count': converted_count,
                'converted_quantity': converted_quantity,
                'affected_orders': len(affected_order_ids),
                'upgraded_to_complete': upgraded_to_complete,
                'details': details
            }

            logger.info(f'在途库存处理完成: 转换{converted_count}条记录, 总数量{converted_quantity}, '
                       f'影响{len(affected_order_ids)}个订单, 升级{upgraded_to_complete}个至完全齐套')

            return result

        except Exception as e:
            logger.error(f'处理在途库存时发生错误: {str(e)}', exc_info=True)
            PlanLog.objects.create(
                log_type='ERROR',
                message=f'处理在途库存失败: {str(e)}'
            )
            raise

    def get_inventory_availability_by_type(self, material_id):
        """
        获取物料按库存类型分类的可用数量汇总

        返回指定物料的各类库存（local、transit、supplier）的数量统计，
        包括总可用量和最早可用的日期信息。
        对于transit类型，使用hold_until作为预计到达日期；
        对于supplier类型，使用delivery_date作为承诺交付日期。

        Args:
            material_id: 物料ID

        Returns:
            dict: {
                'local': {'quantity': int, 'available': int},
                'transit': {'quantity': int, 'available': int, 'earliest_arrival': date},
                'supplier': {'quantity': int, 'available': int, 'commitments': [...]},
                'total_available': int
            }
        """
        today = date.today()
        result = {
            'local': {'quantity': 0, 'available': 0},
            'transit': {'quantity': 0, 'available': 0, 'earliest_arrival': None},
            'supplier': {'quantity': 0, 'available': 0, 'commitments': []},
            'total_available': 0
        }

        try:
            # 1. 查询本地库存 (inventory_type='local')
            local_invs = Inventory.objects.filter(
                material_id=material_id,
                inventory_type='local',
                is_hold=False
            )

            local_quantity = sum(inv.quantity for inv in local_invs)
            local_available = sum(inv.available_quantity for inv in local_invs)

            result['local'] = {
                'quantity': local_quantity,
                'available': local_available
            }

            # 2. 查询在途库存 (inventory_type='transit')
            transit_invs = Inventory.objects.filter(
                material_id=material_id,
                inventory_type='transit',
                is_hold=False
            )

            transit_quantity = sum(inv.quantity for inv in transit_invs)
            transit_available = sum(inv.available_quantity for inv in transit_invs)

            # 找最早的可到达日期（使用hold_until字段）
            earliest_arrival = None
            if transit_invs.exists():
                arrival_dates = [
                    inv.hold_until for inv in transit_invs
                    if inv.hold_until and inv.hold_until >= today
                ]
                if arrival_dates:
                    earliest_arrival = min(arrival_dates)

            result['transit'] = {
                'quantity': transit_quantity,
                'available': transit_available,
                'earliest_arrival': earliest_arrival
            }

            # 3. 查询供应商承诺 (通过SupplierCommitment模型)
            commitments = SupplierCommitment.objects.filter(
                material_id=material_id
            ).select_related('supplier', 'material')

            supplier_quantity = 0
            supplier_commitments_list = []

            for comm in commitments:
                comm_qty = int(comm.quantity or 0)
                supplier_quantity += comm_qty

                commitment_info = {
                    'id': comm.id,
                    'supplier_id': comm.supplier_id,
                    'supplier_name': comm.supplier.supplier_name if comm.supplier else '',
                    'quantity': comm_qty,
                    'delivery_date': str(comm.delivery_date) if comm.delivery_date else None,
                    'order_no': comm.order_no,
                    'days_to_delivery': (comm.delivery_date - today).days if comm.delivery_date else None
                }
                supplier_commitments_list.append(commitment_info)

            # 按交付日期排序
            supplier_commitments_list.sort(key=lambda x: x.get('days_to_delivery') or float('inf'))

            result['supplier'] = {
                'quantity': supplier_quantity,
                'available': supplier_quantity,  # 供应商承诺全部视为潜在可用
                'commitments': supplier_commitments_list
            }

            # 4. 计算总可用量（本地可用 + 即将到货的在途库存）
            # 策略：本地立即可用 + 今日及之前到期的在途库存
            transit_arriving_today = sum(
                inv.available_quantity for inv in transit_invs
                if inv.hold_until and inv.hold_until <= today
            )

            result['total_available'] = local_available + transit_arriving_today

            logger.info(f'物料{material_id}库存可用性汇总: local={local_available}, '
                       f'transit={transit_available}(今日到货={transit_arriving_today}), '
                       f'supplier={supplier_quantity}, 总可用={result["total_available"]}')

            return result

        except Exception as e:
            logger.error(f'获取物料{material_id}库存可用性时发生错误: {str(e)}', exc_info=True)
            raise


class MultiObjectiveOptimizer:
    """多目标优化器 - 考虑供应商可靠率和库存成本"""

    STRATEGY_CONFIGS = {
        'delivery_first': {
            'consumption_priority': 'PRIORITY',
            'description': '优先保证交付率',
            'weights': {'delivery_rate': 0.5, 'inventory_level': 0.15, 'change_stability': 0.2, 'supplier_reliability': 0.1, 'cost_efficiency': 0.05}
        },
        'inventory_first': {
            'consumption_priority': 'INVENTORY_FIRST',
            'description': '优先消耗本地库存',
            'weights': {'delivery_rate': 0.2, 'inventory_level': 0.45, 'change_stability': 0.15, 'supplier_reliability': 0.1, 'cost_efficiency': 0.1}
        },
        'supplier_first': {
            'consumption_priority': 'SUPPLIER_FIRST',
            'description': '优先使用供应商库存',
            'weights': {'delivery_rate': 0.25, 'inventory_level': 0.15, 'change_stability': 0.15, 'supplier_reliability': 0.35, 'cost_efficiency': 0.1}
        },
        'stability_first': {
            'consumption_priority': 'FIFO',
            'description': '优先保证交期稳定',
            'weights': {'delivery_rate': 0.2, 'inventory_level': 0.15, 'change_stability': 0.5, 'supplier_reliability': 0.1, 'cost_efficiency': 0.05}
        },
        'cost_first': {
            'consumption_priority': 'LIFO',
            'description': '优先降低库存成本',
            'weights': {'delivery_rate': 0.25, 'inventory_level': 0.2, 'change_stability': 0.1, 'supplier_reliability': 0.15, 'cost_efficiency': 0.3}
        },
        'expiry_first': {
            'consumption_priority': 'EXPIRY_FIRST',
            'description': '优先消耗即将过期库存',
            'weights': {'delivery_rate': 0.25, 'inventory_level': 0.3, 'change_stability': 0.15, 'supplier_reliability': 0.15, 'cost_efficiency': 0.15}
        }
    }

    def __init__(self, strategy='delivery_first'):
        self.strategy = strategy
        config = self.STRATEGY_CONFIGS.get(strategy, self.STRATEGY_CONFIGS['delivery_first'])
        self.weights = config['weights']
        self.consumption_priority = config['consumption_priority']

    def set_strategy(self, strategy):
        """设置优化策略"""
        if strategy in self.STRATEGY_CONFIGS:
            self.strategy = strategy
            config = self.STRATEGY_CONFIGS[strategy]
            self.weights = config['weights']
            self.consumption_priority = config['consumption_priority']
            return True
        return False

    def get_available_strategies(self):
        """获取所有可用策略"""
        return {
            key: {'description': val['description'], 'weights': val['weights']}
            for key, val in self.STRATEGY_CONFIGS.items()
        }

    def auto_select_strategy(self, orders_data):
        """根据订单数据自动选择最佳策略"""
        total_orders = len(orders_data)
        if total_orders == 0:
            return 'delivery_first'

        high_priority_orders = sum(1 for o in orders_data if o.get('priority', 5) <= 3)
        urgent_ratio = high_priority_orders / total_orders

        avg_quantity = sum(float(o.get('quantity', 0)) for o in orders_data) / total_orders

        recent_shortages = sum(1 for o in orders_data if o.get('has_shortage', False))
        shortage_ratio = recent_shortages / total_orders

        if urgent_ratio > 0.4:
            return 'delivery_first'
        elif shortage_ratio > 0.3:
            return 'supplier_first'
        elif avg_quantity > 1000:
            return 'inventory_first'
        else:
            return 'delivery_first'

    def evaluate_solution(self, plan_results, inventory_data=None):
        """评估解决方案 - 增加供应商可靠性和成本效率指标"""
        total_score = 0.0

        delivery_rate = plan_results.get('complete_rate', 0)
        total_score += delivery_rate * self.weights['delivery_rate']

        avg_complete_rate = plan_results.get('avg_complete_rate', 0)
        inventory_score = 1 - (avg_complete_rate * 0.2)
        total_score += inventory_score * self.weights['inventory_level']

        stable_orders = plan_results.get('stable_orders', 0)
        total_orders = plan_results.get('total_orders', 1)
        change_stability = stable_orders / total_orders if total_orders > 0 else 0
        total_score += change_stability * self.weights['change_stability']

        supplier_reliability = plan_results.get('avg_supplier_reliability', 0.5)
        total_score += supplier_reliability * self.weights['supplier_reliability']

        cost_efficiency = self._calculate_cost_efficiency(plan_results)
        total_score += cost_efficiency * self.weights['cost_efficiency']

        return {
            'total_score': total_score,
            'strategy': self.strategy,
            'weights': self.weights,
            'delivery_score': delivery_rate * self.weights['delivery_rate'],
            'inventory_score': inventory_score * self.weights['inventory_level'],
            'stability_score': change_stability * self.weights['change_stability'],
            'reliability_score': supplier_reliability * self.weights['supplier_reliability'],
            'cost_score': cost_efficiency * self.weights['cost_efficiency']
        }

    def _calculate_cost_efficiency(self, plan_results):
        """计算成本效率"""
        complete_rate = plan_results.get('complete_rate', 0)
        reliability = plan_results.get('avg_supplier_reliability', 0.5)
        
        return (complete_rate * 0.6) + (reliability * 0.4)

    def optimize_allocation(self, orders, strategy=None):
        """优化物料分配"""
        if strategy is None:
            strategy = self.strategy

        if not orders:
            return {
                'results': [],
                'summary': {'total_orders': 0, 'complete_orders': 0, 'partial_orders': 0},
                'scores': {},
                'strategy': strategy,
                'consumption_priority': self.consumption_priority
            }

        orders_data = [{'priority': getattr(o, 'priority', 50) or 50, 'quantity': getattr(o, 'quantity', 0) or 0} for o in orders]

        if strategy == 'auto':
            strategy = self.auto_select_strategy(orders_data)
            self.set_strategy(strategy)

        planner = MaterialPlanner(consumption_priority=self.consumption_priority, strategy=strategy)
        planning_result = planner.run_planning(list(orders) if hasattr(orders, '__iter__') else orders, strategy=strategy)
        results = planning_result.get('results', planning_result) if isinstance(planning_result, dict) else planning_result
        summary = planner.get_planning_summary(results)

        scores = self.evaluate_solution(summary)

        return {
            'results': results,
            'summary': summary,
            'scores': scores,
            'strategy': strategy,
            'consumption_priority': self.consumption_priority
        }

    def batch_optimize(self, orders, strategies=None):
        """批量优化 - 尝试多种策略选择最佳"""
        if strategies is None:
            strategies = list(self.STRATEGY_CONFIGS.keys())

        best_result = None
        best_score = -1
        all_results = []

        for strategy in strategies:
            if strategy in self.STRATEGY_CONFIGS:
                optimizer = MultiObjectiveOptimizer(strategy=strategy)
                result = optimizer.optimize_allocation(orders)
                result['scores']['total_score'] = result['scores'].get('total_score', 0)
                all_results.append(result)

                if result['scores']['total_score'] > best_score:
                    best_score = result['scores']['total_score']
                    best_result = result

        best_result['all_results'] = all_results
        best_result['best_score'] = best_score

        return best_result


class InventoryAIAnalyzer:
    """AI库存分配合理性分析器 - 基于统计学习的智能分析"""

    def __init__(self):
        self.model = 'statistical'

    def analyze_allocation_rationality(self, allocations, inventory_data, orders_data):
        """分析物料分配合理性 - 多维度智能评估"""
        analysis = {
            'allocation_quality': 0.0,
            'inventory_utilization': 0.0,
            'expiry_risk': 0.0,
            'supplier_risk': 0.0,
            'stagnation_risk': 0.0,
            'procurement_recommendations': [],
            'potential_risks': [],
            'suggestions': [],
            'expiring_items': [],
            'root_cause_analysis': []
        }

        allocation_by_material = defaultdict(list)
        for alloc in allocations:
            allocation_by_material[alloc['material_id']].append(alloc)

        today = date.today()
        total_materials = len(allocation_by_material)

        if total_materials == 0:
            return analysis

        for material_id, allocs in allocation_by_material.items():
            total_allocated = sum(a.get('allocated_quantity', a.get('quantity', 0)) for a in allocs)
            inventory_item = inventory_data.get(material_id, {})
            total_inventory = inventory_item.get('quantity', 1)
            
            utilization = total_allocated / total_inventory if total_inventory > 0 else 0
            analysis['inventory_utilization'] += utilization
            
            risk_score = self._calculate_material_risk_score(allocs, inventory_item, today)
            
            if utilization > 0.95:
                analysis['potential_risks'].append({
                    'material_id': material_id,
                    'risk_type': 'high_allocation',
                    'description': f'物料分配比例过高({utilization:.1%})，存在断供风险',
                    'severity': 'high',
                    'risk_score': risk_score
                })
                analysis['root_cause_analysis'].append({
                    'category': 'allocation_concentration',
                    'material_id': material_id,
                    'cause': '单物料过度集中分配',
                    'impact': 'high',
                    'recommendation': '建议分散供应商或增加安全库存'
                })

            if utilization < 0.15 and total_inventory > 0:
                stagnation_amount = total_inventory * (1 - utilization)
                analysis['stagnation_risk'] += stagnation_amount
                analysis['potential_risks'].append({
                    'material_id': material_id,
                    'risk_type': 'stagnation',
                    'description': f'物料利用率过低({utilization:.1%})，存在呆滞风险，预估呆滞金额¥{stagnation_amount * inventory_item.get("standard_cost", 0):.0f}',
                    'severity': 'medium',
                    'risk_score': risk_score * 0.6
                })
                analysis['procurement_recommendations'].append({
                    'type': 'reduce_procurement',
                    'material_id': material_id,
                    'message': f'该物料库存充足但需求低，建议减少采购量或寻找替代出库渠道',
                    'estimated_saving': stagnation_amount * inventory_item.get('standard_cost', 0),
                    'priority': 'low'
                })

            for alloc in allocs:
                expiry_date = alloc.get('expiry_date')
                if expiry_date and (expiry_date - today).days < 30:
                    days_to_expiry = (expiry_date - today).days
                    analysis['expiring_items'].append({
                        'material_id': material_id,
                        'expiry_date': expiry_date,
                        'allocated_quantity': alloc.get('allocated_quantity', alloc.get('quantity', 0)),
                        'days_until_expiry': days_to_expiry
                    })
                    analysis['expiry_risk'] += 1
                    if days_to_expiry <= 7:
                        analysis['root_cause_analysis'].append({
                            'category': 'expiring_stock',
                            'material_id': material_id,
                            'cause': f'临期库存({days_to_expiry}天后过期)仍被分配',
                            'impact': 'medium',
                            'recommendation': '优先消耗临期物料，避免报废损失'
                        })

                reliability = alloc.get('reliability_factor', 1.0)
                if reliability < 0.75:
                    analysis['supplier_risk'] += alloc.get('allocated_quantity', alloc.get('quantity', 0)) * (1 - reliability)
                    analysis['root_cause_analysis'].append({
                        'category': 'supplier_risk',
                        'material_id': material_id,
                        'cause': f'供应商可靠率仅{(reliability*100):.0f}%，存在交期延误风险',
                        'impact': 'high' if reliability < 0.6 else 'medium',
                        'recommendation': '建议启用备选供应商或增加安全提前期'
                    })
                    
                    analysis['procurement_recommendations'].append({
                        'type': 'backup_supplier',
                        'material_id': material_id,
                        'message': f'主供应商可靠率偏低({(reliability*100):.0f}%)，建议开发备选供应商',
                        'priority': 'high' if reliability < 0.6 else 'medium'
                    })

        if total_materials > 0:
            analysis['inventory_utilization'] /= total_materials
            analysis['expiry_risk'] /= total_materials

        total_alloc_qty = sum(sum(a.get('allocated_quantity', a.get('quantity', 0)) for a in v) for v in allocation_by_material.values())
        analysis['supplier_risk'] = min(1.0, analysis['supplier_risk'] / (total_alloc_qty + 0.001))

        quality_factors = {
            'utilization_score': min(1.0, analysis['inventory_utilization']) * 25,
            'expiry_factor': max(0, 1 - analysis['expiry_risk'] * 5) * 20,
            'supplier_factor': max(0, 1 - analysis['supplier_risk']) * 25,
            'stagnation_factor': max(0, 1 - min(1, analysis['stagnation_risk'] / 10000)) * 20,
            'diversity_bonus': min(10, total_materials * 0.5)
        }
        analysis['allocation_quality'] = sum(quality_factors.values())
        
        analysis['quality_breakdown'] = quality_factors

        return analysis

    def _calculate_material_risk_score(self, allocations, inventory_info, today):
        """计算单个物料的综合风险分数"""
        score = 0.0
        
        total_alloc = sum(a.get('allocated_quantity', a.get('quantity', 0)) for a in allocations)
        total_inv = inventory_info.get('quantity', 1)
        if total_inv > 0:
            concentration = total_alloc / total_inv
            score += concentration ** 2 * 40
        
        expiry_count = sum(1 for a in allocations if a.get('expiry_date') and (a['expiry_date'] - today).days < 60)
        score += expiry_count * 15
        
        low_rel_count = sum(1 for a in allocations if a.get('reliability_factor', 1.0) < 0.8)
        score += low_rel_count * 20
        
        return min(100, score)

    def predict_demand_trend(self, historical_data):
        """基于历史数据预测需求趋势 - 使用移动平均+趋势外推"""
        if len(historical_data) < 3:
            return {'trend': 'stable', 'forecast': [], 'confidence': 0.0}
        
        values = [d.get('quantity', 0) for d in historical_data]
        n = len(values)
        
        ma_short = sum(values[-3:]) / 3 if n >= 3 else values[-1]
        ma_long = sum(values) / n
        
        trend_ratio = ma_short / ma_long if ma_long > 0 else 1.0
        
        if trend_ratio > 1.15:
            trend = 'rising'
        elif trend_ratio < 0.85:
            trend = 'declining'
        else:
            trend = 'stable'
        
        last_val = values[-1]
        forecast = [last_val * (trend_ratio ** (i + 1)) for i in range(1, 4)]
        
        variance = np.var(values) if len(values) > 1 else 0
        mean_val = np.mean(values) if values else 1
        cv = (variance ** 0.5) / mean_val if mean_val > 0 else 1
        confidence = max(0.1, min(0.95, 1 - cv))
        
        return {
            'trend': trend,
            'ma_3day': round(ma_short, 2),
            'ma_total': round(ma_long, 2),
            'trend_pct': round((trend_ratio - 1) * 100, 1),
            'forecast': [round(f, 2) for f in forecast],
            'confidence': round(confidence, 2)
        }

    def generate_procurement_plan(self, shortage_report, current_inventory):
        """生成智能采购计划 - 综合缺料、库存水位、供应商能力"""
        procurement_plan = []
        
        for item in shortage_report.get('material_shortages', []):
            material_id_str = str(item.get('material_code', ''))
            shortage_qty = item.get('shortage_qty', 0)
            safety_stock = item.get('safety_stock', 0)
            lead_time = item.get('lead_time', 7)
            urgency = item.get('urgency_level', 'normal')
            
            suppliers = item.get('suppliers', [])
            best_supplier = suppliers[0] if suppliers else None
            
            order_qty = max(shortage_qty, safety_stock * 0.5)
            min_order = item.get('min_order_qty', 1)
            if order_qty < min_order:
                order_qty = min_order
            
            batch_multiplier = max(1, int(np.ceil(order_qty / min_order)))
            final_qty = batch_multiplier * min_order
            
            plan_item = {
                'material_code': item.get('material_code'),
                'material_name': item.get('material_name'),
                'order_quantity': final_qty,
                'shortage_quantity': shortage_qty,
                'safety_buffer': final_qty - shortage_qty,
                'urgency': urgency,
                'recommended_supplier': best_supplier.get('supplier_name') if best_supplier else '待定',
                'supplier_lead_time': best_supplier.get('lead_time', lead_time) if best_supplier else lead_time,
                'unit_price': best_supplier.get('unit_price', 0) if best_supplier else 0,
                'estimated_cost': final_qty * (best_supplier.get('unit_price', 0) if best_supplier else 0),
                'latest_order_date': item.get('latest_purchase_date'),
                'action': item.get('recommended_action', '')
            }
            procurement_plan.append(plan_item)
        
        procurement_plan.sort(key=lambda x: {'critical': 0, 'urgent': 1, 'normal': 2, 'relaxed': 3}.get(x.get('urgency', 'normal'), 2))
        
        return {
            'items': procurement_plan,
            'total_items': len(procurement_plan),
            'total_estimated_cost': sum(p['estimated_cost'] for p in procurement_plan),
            'critical_count': sum(1 for p in procurement_plan if p.get('urgency') == 'critical'),
            'urgent_count': sum(1 for p in procurement_plan if p.get('urgency') == 'urgent')
        }
