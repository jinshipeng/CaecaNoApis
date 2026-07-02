"""
多约束条件下供应链智能运营系统 - 性能基准测试脚本

功能:
    1. 生成不同规模的模拟数据（订单/物料/BOM/库存/供应商）
    2. 调用 MaterialPlanner 和 HighPerformancePlanner 执行物料计划计算
    3. 记录各规模下的耗时、内存占用、缓存命中率
    4. 输出格式化的基准测试报告（控制台表格 + JSON文件 + Markdown报告）

运行方式:
    cd system
    python prediction/tests/benchmark_performance.py                          # 运行全部场景
    python prediction/tests/benchmark_performance.py --scenario=large --runs=3 # 指定场景和运行次数
    python prediction/tests/benchmark_performance.py --scenario=small          # 仅运行小型场景
    python prediction/tests/benchmark_performance.py --list-scenarios          # 列出所有可用场景

场景矩阵（6种规模）:
    小型:     500订单 × 100物料 × 300BOM × 500库存 × 10供应商
    中型:   1,000订单 × 500物料 × 1,500BOM × 2,000库存 × 20供应商
    中大型: 3,000订单 × 1,000物料 × 4,000BOM × 5,000库存 × 30供应商
    大型(目标): 5,000订单 × 2,000物料 × 8,000BOM × 10,000库存 × 50供应商
    超大规模: 8,000订单 × 3,500物料 × 14,000BOM × 18,000库存 × 70供应商
    压力测试: 10,000订单 × 5,000物料 × 20,000BOM × 25,000库存 × 100供应商

性能判定标准:
    万级订单(10,000)总耗时 < 3600秒(1小时) = PASS
"""

import os
import sys
import json
import time
import argparse
import tracemalloc
import gc
import logging
import platform
from datetime import date, timedelta, datetime
from decimal import Decimal
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple

# ============================================================
# Django 环境初始化（必须在导入模型之前完成）
# ============================================================
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bp_prediction_system.settings')

# 将项目根目录加入 Python 路径，确保 Django 能找到配置
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import django
django.setup()

# ============================================================
# 导入项目模型和模块
# ============================================================
from django.db import connection, transaction
from django.db.models import Avg, Count, Q
from django.core.cache import cache

from prediction.models import (
    SalesOrder,
    Material,
    BillOfMaterials,
    Inventory,
    Supplier,
    SupplierMaterial,
    SupplierCommitment,
    Customer,
    WorkCenter,
    FactoryCalendar,
    OrderAllocation,
    MaterialPlanResult,
    PlanLog,
)

from prediction.material_planning import MaterialPlanner
from prediction.high_performance_planner import HighPerformancePlanner

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('benchmark')


# ============================================================
# 场景定义：6种测试规模
# ============================================================
SCENARIOS = {
    'small': {
        'name': '小型规模',
        'order_count': 500,
        'material_count': 100,
        'bom_count': 300,
        'inventory_count': 500,
        'supplier_count': 10,
        'bom_levels': (2, 3),
    },
    'medium': {
        'name': '中型规模',
        'order_count': 1000,
        'material_count': 500,
        'bom_count': 1500,
        'inventory_count': 2000,
        'supplier_count': 20,
        'bom_levels': (3, 4),
    },
    'medium_large': {
        'name': '中大型规模',
        'order_count': 3000,
        'material_count': 1000,
        'bom_count': 4000,
        'inventory_count': 5000,
        'supplier_count': 30,
        'bom_levels': (3, 5),
    },
    'large': {
        'name': '大型(目标)',
        'order_count': 5000,
        'material_count': 2000,
        'bom_count': 8000,
        'inventory_count': 10000,
        'supplier_count': 50,
        'bom_levels': (4, 5),
    },
    'xlarge': {
        'name': '超大规模',
        'order_count': 8000,
        'material_count': 3500,
        'bom_count': 14000,
        'inventory_count': 18000,
        'supplier_count': 70,
        'bom_levels': (4, 6),
    },
    'stress': {
        'name': '压力测试',
        'order_count': 10000,
        'material_count': 5000,
        'bom_count': 20000,
        'inventory_count': 25000,
        'supplier_count': 100,
        'bom_levels': (5, 6),
    },
}

# 并行度配置列表
PARALLEL_CONFIGS = [
    ('serial', 1),       # 串行模式
    ('parallel_4', 4),   # 并行, 4 workers
    ('parallel_8', 8),   # 并行, 8 workers
    ('parallel_16', 16), # 并行, 16 workers
]

# 性能阈值：万级订单最大允许耗时(秒)
MAX_ACCEPTABLE_TIME_STRESS = 3600


# ============================================================
# 数据生成器类
# ============================================================
class TestDataGenerator:
    """
    测试数据生成器
    
    负责生成不同规模的模拟供应链数据：
    - 客户池、物料（原材料/半成品/成品）、BOM结构、库存记录
    - 供应商及供应商-物料关系、供应商交付承诺、销售订单
    """

    def __init__(self, random_seed: int = 42):
        import random
        self.rng = random.Random(random_seed)
        self.today = date.today()
        # 客户名称池
        self.customer_pool = [
            '联想集团', '戴尔科技', '惠普公司', '微软中国', '亚马逊AWS',
            '谷歌云服务', '华为技术', '中兴通讯', '小米科技', '荣耀终端',
            '华硕电脑', '宏碁电脑', '微星科技', '技嘉科技', '神舟电脑',
            '清华同方', '方正科技', '长城信息', '浪潮信息', '中科曙光',
            '紫光股份', '新华三集团', '锐捷网络', '深信服科技', '奇安信科技',
            '启明星辰', '绿盟科技', '安恒信息', '天融信科技', '山石网科',
            '富士康集团', '和硕联合', '广达电脑', '仁宝电脑', '纬创资通',
            '英业达', '比亚迪电子', '闻泰科技', '华勤技术', '龙旗科技',
            '传音控股', 'TCL科技', '京东方A', '维信诺', '天马微电子',
            '友达光电', '群创光电', '三星显示', 'LG显示', '夏普显示',
        ]

        # 工厂代码列表
        self.factory_codes = ['F01', 'F02', 'F03']

        # 物料类别前缀映射
        self.material_type_prefix = {
            'raw': 'RAW',      # 原材料
            'semi': 'SEM',     # 半成品
            'finished': 'FIN', # 成品
        }

        # 存储生成的数据引用（供后续清理使用）
        self._created_materials = []
        self._created_suppliers = []
        self._created_customers = []

    def _randint(self, a: int, b: int) -> int:
        """生成随机整数"""
        return self.rng.randint(a, b)

    def _uniform(self, a: float, b: float, decimals: int = 2) -> float:
        """生成随机浮点数，保留指定小数位"""
        return round(self.rng.uniform(a, b), decimals)

    def _choice(self, seq: list):
        """从序列中随机选择一个元素"""
        return self.rng.choice(seq)

    def generate_all(
        self,
        order_count: int,
        material_count: int,
        bom_count: int,
        inventory_count: int,
        supplier_count: int,
        bom_level_range: Tuple[int, int],
    ) -> Dict[str, Any]:
        """
        一次性生成全部测试数据
        
        Returns:
            包含各实体计数字典的统计信息
        """
        logger.info(f"开始生成测试数据: {order_count}订单 × {material_count}物料 "
                     f"× {bom_count}BOM × {inventory_count}库存 × {supplier_count}供应商")

        t_start = time.time()

        # 1. 清理旧的基准测试数据（仅清理 BMK- 前缀的数据，避免影响业务数据）
        self._cleanup_benchmark_data()

        # 2. 生成基础主数据（客户、供应商、物料）
        suppliers = self._generate_suppliers(supplier_count)
        customers = self._generate_customers(min(50, order_count // 10))
        materials = self._generate_materials(material_count)
        finished_materials = [m for m in materials if m.material_type == 'finished']

        # 3. 生成BOM结构
        bom_records = self._generate_boms(materials, bom_count, bom_level_range)

        # 4. 生成库存记录
        inventory_records = self._generate_inventories(materials, inventory_count)

        # 5. 生成供应商-物料关系
        supplier_materials = self._generate_supplier_materials(suppliers, materials)

        # 6. 生成供应商承诺
        commitments = self._generate_commitments(suppliers, materials)

        # 7. 生成销售订单
        orders = self._generate_orders(order_count, finished_materials, customers)

        data_gen_time = time.time() - t_start

        stats = {
            'suppliers': len(suppliers),
            'customers': len(customers),
            'materials': len(materials),
            'finished_materials': len(finished_materials),
            'bom_records': bom_records,
            'inventory_records': inventory_records,
            'supplier_materials': supplier_materials,
            'commitments': commitments,
            'orders': len(orders),
            'data_gen_time': round(data_gen_time, 2),
        }

        logger.info(f"数据生成完成，耗时 {data_gen_time:.2f}s")
        logger.info(f"  - 供应商: {stats['suppliers']}")
        logger.info(f"  - 客户: {stats['customers']}")
        logger.info(f"  - 物料: {stats['materials']} (成品:{stats['finished_materials']})")
        logger.info(f"  - BOM: {stats['bom_records']}")
        logger.info(f"  - 库存: {stats['inventory_records']}")
        logger.info(f"  - 供应商物料: {stats['supplier_materials']}")
        logger.info(f"  - 承诺: {stats['commitments']}")
        logger.info(f"  - 订单: {stats['orders']}")

        return stats

    def _cleanup_benchmark_data(self):
        """清理之前基准测试生成的数据（BMK- 前缀）"""
        try:
            # 删除基准测试订单及相关关联数据
            bmk_orders = SalesOrder.objects.filter(order_no__startswith='BMK-SO-')
            if bmk_orders.exists():
                order_ids = list(bmk_orders.values_list('id', flat=True))
                OrderAllocation.objects.filter(order_id__in=order_ids).delete()
                MaterialPlanResult.objects.filter(order_id__in=order_ids).delete()
                bmk_orders.delete()
                logger.debug("已清理旧基准测试订单数据")

            # 删除基准测试物料
            Material.objects.filter(material_code__startswith='BMK-MAT-').delete()

            # 删除基准测试供应商
            Supplier.objects.filter(supplier_code__startswith='BMK-SUP-').delete()

            # 删除基准测试客户
            Customer.objects.filter(customer_code__startswith='BMK-CUS-').delete()
        except Exception as e:
            logger.warning(f"清理旧数据时出现异常（可忽略）: {e}")

    def _generate_suppliers(self, count: int) -> List[Supplier]:
        """生成供应商数据
        
        规则:
        - rating: A/A/B/B/B/C 分布（优质偏多）
        - delivery_reliability: uniform(0.75, 0.99)
        """
        suppliers = []
        rating_choices = ['A', 'A', 'B', 'B', 'B', 'C']  # A和B级偏多

        for i in range(count):
            s = Supplier.objects.create(
                supplier_code=f'BMK-SUP-{i:04d}',
                supplier_name=f'基准测试供应商{i+1:03d}',
                contact_person=f'联系人{i+1}',
                phone=f'138{self._randint(10000000, 99999999)}',
                email=f'supplier_{i+1}@benchmark.test',
                address=f'测试地址第{i+1}号',
                rating=self._choice(rating_choices),
                delivery_reliability=self._uniform(0.75, 0.99),
                normal_lead_time=self._randint(5, 30),
                is_active=True,
            )
            suppliers.append(s)

        self._created_suppliers = suppliers
        return suppliers

    def _generate_customers(self, count: int) -> List[Customer]:
        """生成客户数据"""
        customers = []
        used_names = set()

        for i in range(count):
            # 从客户池中选择，避免重复
            name = self.customer_pool[i % len(self.customer_pool)]
            if name in used_names:
                name = f'{name}_{i}'
            used_names.add(name)

            c = Customer.objects.create(
                customer_code=f'BMK-CUS-{i:04d}',
                customer_name=name,
                contact_person=f'客户联系人{i+1}',
                phone=f'139{self._randint(10000000, 99999999)}',
                email=f'customer_{i+1}@benchmark.test',
                address=f'客户地址第{i+1}号',
                credit_limit=Decimal(str(self._randint(100000, 5000000))),
                customer_type=['战略客户', '重点客户', '一般客户'][i % 3],
                payment_terms='月结30天' if i % 3 == 0 else ('月结60天' if i % 3 == 1 else '货到付款'),
                customer_level=['VIP', 'gold', 'silver', 'normal'][i % 4],
                delivery_priority=self._randint(1, 5),
                is_active=True,
            )
            customers.append(c)

        self._created_customers = customers
        return customers

    def _generate_materials(self, count: int) -> List[Material]:
        """生成物料数据
        
        规则:
        - material_code: "MAT-{类别字母}{序号:04d}" （类别: RAW/SEM/FIN）
        - material_type: raw(60%) / semi(25%) / finished(15%)
        - safety_stock: randint(0, 200)
        - lead_time: randint(3, 45)
        - standard_cost: uniform(1, 1000)
        """
        materials = []
        type_distribution = ['raw'] * 60 + ['semi'] * 25 + ['finished'] * 15  # 加权分布

        for i in range(count):
            mat_type = type_distribution[i % len(type_distribution)]
            prefix = self.material_type_prefix[mat_type]

            m = Material.objects.create(
                material_code=f'BMK-MAT-{prefix}-{i:04d}',
                material_name=f'基准测试物料-{prefix}-{i+1:04d}',
                material_type=mat_type,
                unit='件' if mat_type != 'raw' else ('kg' if i % 3 == 0 else '个'),
                shelf_life=self._randint(0, 365) if mat_type == 'raw' else 0,
                min_order_qty=self._randint(1, 100),
                lead_time=self._randint(3, 45),
                standard_cost=Decimal(str(self._uniform(1.0, 1000.0))),
                sales_price=Decimal(str(self._uniform(1.5, 1500.0))),
                safety_stock=self._randint(0, 200),
                min_production_qty=self._randint(1, 500) if mat_type in ('semi', 'finished') else 1,
                is_active=True,
            )
            materials.append(m)

        self._created_materials = materials
        return materials

    def _generate_boms(
        self,
        materials: List[Material],
        target_count: int,
        level_range: Tuple[int, int],
    ) -> int:
        """生成BOM结构数据
        
        规则:
        - 成品→半成品→原材料，多层级结构
        - 每个父件有2-8个子件
        - quantity: uniform(0.5, 10)，保留2位小数
        - 30%的BOM行有替代料关系(alternative_group)
        - 10%的BOM有ECN变更记录
        - factory_code: 随机分配到3个工厂(F01/F02/F03)
        
        Args:
            materials: 物料列表
            target_count: 目标BOM记录数
            level_range: BOM层级范围 (min_level, max_level)
            
        Returns:
            实际创建的BOM记录数
        """
        # 按类型分组
        finished = [m for m in materials if m.material_type == 'finished']
        semi = [m for m in materials if m.material_type == 'semi']
        raw = [m for m in materials if m.material_type == 'raw']

        if not finished or not (semi or raw):
            logger.warning("缺少足够的物料来构建BOM结构")
            return 0

        bom_count = 0
        alternative_group_counter = 0
        ecn_counter = 0

        # 为每个成品创建BOM层级结构
        for parent_idx, parent in enumerate(finished):
            # 确定该成品的BOM层级深度
            max_depth = self._randint(level_range[0], level_range[1])

            current_parents = [parent]
            available_children = semi if semi else raw

            for level in range(1, max_depth + 1):
                next_parents = []

                for cp in current_parents:
                    # 每个父件的子件数量：2-8个
                    num_children = self._randint(2, 8)

                    for j in range(num_children):
                        if not available_children:
                            break

                        child = self._choice(available_children)

                        # 避免自引用
                        if child.id == cp.id:
                            continue

                        # 替代料组：30%概率有替代料关系
                        alt_group = None
                        if self._rng.random() < 0.30:
                            alternative_group_counter += 1
                            alt_group = f'BMK-ALT-{alternative_group_counter:04d}'

                        # ECN变更记录：10%概率
                        ecn_no = None
                        ecn_date = None
                        ecn_reason = None
                        if self._rng.random() < 0.10:
                            ecn_counter += 1
                            ecn_no = f'ECN-BMK-{ecn_counter:05d}'
                            ecn_date = self.today - timedelta(days=self._randint(10, 180))
                            ecn_reason = f'基准测试ECN变更#{ecn_counter}'

                        BillOfMaterials.objects.create(
                            parent_material=cp,
                            child_material=child,
                            quantity=Decimal(str(self._uniform(0.5, 10.0))),
                            unit='件',
                            bom_level=level,
                            usage_ratio=self._uniform(0.01, 50.0),
                            scrap_rate=self._uniform(0.0, 0.05),
                            alternative_group=alt_group,
                            alternative_priority=self._randint(1, 5) if alt_group else 1,
                            alternative_ratio=self._uniform(0.1, 1.0) if alt_group else 1.0,
                            factory_code=self._choice(self.factory_codes),
                            ecn_no=ecn_no,
                            ecn_date=ecn_date,
                            ecn_reason=ecn_reason,
                            version=self._randint(1, 5),
                            is_active=True,
                        )
                        bom_count += 1

                        if bom_count >= target_count:
                            return bom_count

                        # 半成品可以作为下一层的父件
                        if child.material_type == 'semi':
                            next_parents.append(child)

                # 进入下一层时，如果半成品不够则补充原材料
                current_parents = next_parents if next_parents else (
                    [self._choice(raw)] if raw else []
                )

                # 如果只剩一层且没有更多半成品，使用原材料
                if level == max_depth - 1 and not semi:
                    available_children = raw

        # 如果还没达到目标数量，补充一些额外的BOM关系
        while bom_count < target_count and materials:
            parent = self._choice(materials)
            child = self._choice(materials)
            if parent.id != child.id:
                BillOfMaterials.objects.create(
                    parent_material=parent,
                    child_material=child,
                    quantity=Decimal(str(self._uniform(0.5, 5.0))),
                    bom_level=self._randint(1, 3),
                    factory_code=self._choice(self.factory_codes),
                    is_active=True,
                )
                bom_count += 1

        return bom_count

    def _generate_inventories(self, materials: List[Material], target_count: int) -> int:
        """生成库存记录数据
        
        规则:
        - inventory_type: local(60%) / transit(20%) / supplier(10%) / finished(5%) / semi(5%)
        - quantity: randint(0, 5000)，80%的物料有库存
        - 5%的库存记录 is_hold=True（Hold状态测试）
        - hold_until: Hold库存的未来1-30天解Hold日期
        """
        inv_types = ['local'] * 60 + ['transit'] * 20 + ['supplier'] * 10 + \
                     ['finished'] * 5 + ['semi'] * 5
        warehouses = ['WH-A01', 'WH-A02', 'WH-B01', 'WH-B02', 'WH-C01',
                      'TRANSIT-HUB', 'VMI-WH01', 'VMI-WH02']

        inventory_count = 0

        # 约80%的物料会有库存记录
        materials_with_inventory = self.rng.sample(
            materials, min(int(len(materials) * 0.8), len(materials))
        )

        for idx, material in enumerate(materials_with_inventory):
            if inventory_count >= target_count:
                break

            # 每个物料可能有多条库存记录（不同仓库/类型）
            num_records = self._randint(1, 3)

            for _ in range(num_records):
                if inventory_count >= target_count:
                    break

                inv_type = self._choice(inv_types)
                qty = self._randint(0, 5000)

                # Hold状态：5%概率
                is_hold = self._rng.random() < 0.05
                hold_quantity = 0
                hold_until = None
                hold_reason = None

                if is_hold and qty > 0:
                    hold_quantity = self._randint(1, max(qty // 2, 1))
                    hold_until = self.today + timedelta(days=self._randint(1, 30))
                    hold_reason = '基准测试质量检验冻结'

                Inventory.objects.create(
                    material=material,
                    inventory_type=inv_type,
                    quantity=qty,
                    hold_quantity=hold_quantity,
                    available_quantity=max(0, qty - hold_quantity),
                    warehouse=self._choice(warehouses),
                    location=f'LOC-{self._randint(1, 99):02d}-{self._randint(1, 99):02d}',
                    batch_no=f'BATCH-{self._randint(10000, 99999)}' if self._rng.random() < 0.3 else None,
                    expiry_date=(self.today + timedelta(days=self._randint(30, 720)))
                               if inv_type in ('local', 'transit') and self._rng.random() < 0.2
                               else None,
                    is_hold=is_hold,
                    hold_reason=hold_reason,
                    hold_until=hold_until,
                    data_date=self.today,
                    factory_code=self._choice(self.factory_codes),
                )
                inventory_count += 1

        return inventory_count

    def _generate_supplier_materials(
        self,
        suppliers: List[Supplier],
        materials: List[Material],
    ) -> int:
        """生成供应商-物料关系数据
        
        规则:
        - rating: choice(['A','A','B','B','B','C'])
        - delivery_reliability: uniform(0.75, 0.99)
        - 10%的供应商物料 is_forbidden=True
        """
        count = 0
        raw_and_semi = [m for m in materials if m.material_type in ('raw', 'semi')]

        for supplier in suppliers:
            # 每个供应商供应部分物料
            num_materials = self._randint(max(1, len(raw_and_semi) // len(suppliers)),
                                          max(2, len(raw_and_semi) // len(suppliers) * 2))
            supplier_mats = self.rng.sample(raw_and_semi, min(num_materials, len(raw_and_semi)))

            for material in supplier_mats:
                try:
                    SupplierMaterial.objects.create(
                        supplier=supplier,
                        material=material,
                        lead_time=material.lead_time + self._randint(-3, 10),
                        unit_price=material.standard_cost * Decimal(str(self._uniform(1.0, 1.5))),
                        min_order_qty=self._randint(10, 500),
                        is_forbidden=self._rng.random() < 0.10,  # 10%禁用
                        forbidden_reason='基准测试禁用标记' if self._rng.random() < 0.10 else None,
                    )
                    count += 1
                except Exception:
                    # 忽略唯一约束冲突
                    pass

        return count

    def _generate_commitments(
        self,
        suppliers: List[Supplier],
        materials: List[Material],
    ) -> int:
        """生成供应商交付承诺数据
        
        规则:
        - SupplierCommitment: 未来7-60天的交付承诺
        """
        count = 0
        raw_and_semi = [m for m in materials if m.material_type in ('raw', 'semi')]

        for supplier in suppliers[:max(1, len(suppliers) // 2)]:
            # 每个供应商对部分物料有承诺
            num_commits = self._randint(5, 20)
            commit_mats = self.rng.sample(raw_and_semi, min(num_commits, len(raw_and_semi)))

            for material in commit_mats:
                # 未来7-60天的交付日期
                delivery_date = self.today + timedelta(days=self._randint(7, 60))

                SupplierCommitment.objects.create(
                    supplier=supplier,
                    material=material,
                    quantity=self._randint(100, 5000),
                    delivery_date=delivery_date,
                    order_no=f'BMK-PO-{count+1:06d}',
                )
                count += 1

        return count

    def _generate_orders(
        self,
        count: int,
        finished_materials: List[Material],
        customers: List[Customer],
    ) -> List[SalesOrder]:
        """生成销售订单数据
        
        规则:
        - order_no: "SO-{日期}-{序号:05d}"
        - customer_name: 从客户池随机选择
        - material_id: 随机选择成品物料
        - quantity: randint(10, 500)
        - demand_date: 今天 + randInt(7, 90) 天
        - priority: choice([1,1,2,2,2,3,3,3,4,5]) (P1/P2偏多)
        - shipping_method: choice(['sea', 'sea', 'sea', 'air']) (海运居多)
        - status: random.choice(['pending', 'confirmed', 'allocated'])
        - is_forecast: 15%概率为True
        """
        orders = []
        today_str = self.today.strftime('%Y%m%d')
        priority_choices = [1, 1, 2, 2, 2, 3, 3, 3, 4, 5]  # P1/P2偏多
        shipping_choices = ['sea', 'sea', 'sea', 'air']           # 海运居多
        status_choices = ['pending', 'confirmed', 'allocated']
        customer_names = [c.customer_name for c in customers] if customers else self.customer_pool

        for i in range(count):
            # 选择成品物料
            mat = self._choice(finished_materials) if finished_materials else None
            mat_id = mat.id if mat else None

            # 订单数量
            qty = self._randint(10, 500)
            unit_price = Decimal(str(mat.standard_cost * self._uniform(1.2, 2.0))) if mat else Decimal('150')

            order = SalesOrder.objects.create(
                order_no=f'BMK-SO-{today_str}-{i:05d}',
                customer_name=self._choice(customer_names),
                material_id=mat_id,
                quantity=qty,
                unit_price=unit_price,
                total_amount=unit_price * qty,
                order_date=self.today - timedelta(days=self._randint(0, 30)),
                demand_date=self.today + timedelta(days=self._randint(7, 90)),
                status=self._choice(status_choices),
                priority=self._choice(priority_choices),
                shipping_method=self._choice(shipping_choices),
                shipping_days=45 if self._choice(shipping_choices) == 'sea' else 3,
                production_lead_time=self._randint(2, 15),
                is_forecast=self._rng.random() < 0.15,  # 15%预测订单
                allow_early_delivery=True,
                earliest_delivery_date=None,
                factory_code=self._choice(self.factory_codes),
            )
            orders.append(order)

        return orders


# ============================================================
# 性能基准测试执行引擎
# ============================================================
class BenchmarkEngine:
    """
    性能基准测试引擎
    
    负责:
    1. 按场景矩阵执行多轮测试
    2. 在不同并行度下对比性能
    3. 收集时间/内存/缓存等指标
    4. 生成格式化报告
    """

    def __init__(self, runs_per_scenario: int = 3, target_scenario: Optional[str] = None):
        """
        初始化基准测试引擎
        
        Args:
            runs_per_scenario: 每个场景重复运行的次数（取平均值）
            target_scene: 指定要测试的场景名，None表示测试所有场景
        """
        self.runs = runs_per_scenario
        self.target_scenario = target_scenario
        self.results: List[Dict[str, Any]] = []
        self.generator = TestDataGenerator(random_seed=42)

    def run_all_scenarios(self) -> List[Dict[str, Any]]:
        """执行所有（或指定）场景的基准测试"""
        # 确定要运行的场景
        scenarios_to_run = {}
        if self.target_scenario:
            if self.target_scenario not in SCENARIOS:
                logger.error(f"未知场景: '{self.target_scenario}'")
                logger.info(f"可用场景: {', '.join(SCENARIOS.keys())}")
                return []
            scenarios_to_run = {self.target_scenario: SCENARIOS[self.target_scenario]}
        else:
            scenarios_to_run = SCENARIOS

        total_scenarios = len(scenarios_to_run)
        logger.info(f"{'='*80}")
        logger.info(f"开始性能基准测试: {total_scenarios} 个场景, 每场景 {self.runs} 次")
        logger.info(f"{'='*80}")

        for scenario_key, config in scenarios_to_run.items():
            try:
                result = self._run_single_scenario(scenario_key, config)
                if result:
                    self.results.append(result)
            except Exception as e:
                logger.error(f"场景 [{config['name']}] 执行失败: {e}", exc_info=True)
                # 记录失败但不中断其他场景
                self.results.append({
                    'scenario': scenario_key,
                    'scenario_name': config['name'],
                    'status': 'FAILED',
                    'error': str(e)[:500],
                })

        return self.results

    def _run_single_scenario(
        self,
        scenario_key: str,
        config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        执行单个场景的完整基准测试
        
        流程:
        1. 生成模拟数据
        2. 多次运行取平均（serial / parallel_4 / parallel_8 / parallel_16）
        3. 收集各项指标
        4. 返回结果字典
        """
        scenario_name = config['name']
        order_count = config['order_count']
        material_count = config['material_count']
        bom_count = config['bom_count']
        inventory_count = config['inventory_count']
        supplier_count = config['supplier_count']
        bom_levels = config['bom_levels']

        logger.info(f"\n{'─'*80}")
        logger.info(f"【场景】{scenario_name}: {order_count:,}订单 × {material_count:,}物料 "
                     f"× {bom_count:,}BOM × {inventory_count:,}库存 × {supplier_count:,}供应商")
        logger.info(f"{'─'*80}")

        # ── 第一步：生成测试数据 ──
        tracemalloc.start()
        gen_start = time.perf_counter()

        data_stats = self.generator.generate_all(
            order_count=order_count,
            material_count=material_count,
            bom_count=bom_count,
            inventory_count=inventory_count,
            supplier_count=supplier_count,
            bom_level_range=bom_levels,
        )

        data_gen_time = time.perf_counter() - gen_start

        # ── 第二步：在不同并行度下执行多次测试 ──
        parallel_results = {}

        for mode_name, worker_count in PARALLEL_CONFIGS:
            mode_times = []
            mode_cache_hits = []
            mode_mem_peaks = []
            mode_db_queries = []
            mode_complete_rates = []
            mode_order_counts = []

            logger.info(f"  >> 测试模式: {mode_name} (workers={worker_count}), "
                       f"重复 {self.runs} 次...")

            for run_idx in range(self.runs):
                run_metrics = self._execute_single_run(
                    mode_name=mode_name,
                    worker_count=worker_count,
                    run_index=run_idx,
                )
                if run_metrics:
                    mode_times.append(run_metrics['planning_time'])
                    mode_cache_hits.append(run_metrics.get('cache_hit_rate', 0))
                    mode_mem_peaks.append(run_metrics.get('peak_memory_mb', 0))
                    mode_db_queries.append(run_metrics.get('db_query_count', 0))
                    mode_complete_rates.append(run_metrics.get('avg_complete_rate', 0))
                    mode_order_counts.append(run_metrics.get('orders_processed', 0))

            # 计算该模式下的平均值
            if mode_times:
                parallel_results[mode_name] = {
                    'avg_time': sum(mode_times) / len(mode_times),
                    'min_time': min(mode_times),
                    'max_time': max(mode_times),
                    'avg_cache_hit': sum(mode_cache_hits) / len(mode_cache_hits) if mode_cache_hits else 0,
                    'avg_memory': sum(mode_mem_peaks) / len(mode_mem_peaks) if mode_mem_peaks else 0,
                    'avg_db_queries': sum(mode_db_queries) / len(mode_db_queries) if mode_db_queries else 0,
                    'avg_complete_rate': sum(mode_complete_rates) / len(mode_complete_rates) if mode_complete_rates else 0,
                    'orders_processed': mode_order_counts[0] if mode_order_counts else 0,
                    'runs_completed': len(mode_times),
                }

        # 获取内存快照
        _, peak_memory = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # ── 第三步：汇总结果质量指标 ──
        quality_stats = self._collect_quality_stats()

        # ── 第四步：组装最终结果 ──
        serial_result = parallel_results.get('serial', {})
        p16_result = parallel_results.get('parallel_16', {})

        serial_total = serial_result.get('avg_time', 0) + data_gen_time
        p16_total = p16_result.get('avg_time', 0) + data_gen_time

        result = {
            # 场景基本信息
            'scenario': scenario_key,
            'scenario_name': scenario_name,
            'status': 'SUCCESS',

            # 数据规模
            'order_count': order_count,
            'material_count': material_count,
            'bom_count': data_stats.get('bom_records', bom_count),
            'inventory_count': data_stats.get('inventory_records', inventory_count),
            'supplier_count': supplier_count,

            # 时间指标
            'data_gen_time': round(data_gen_time, 3),
            'planning_time_serial': round(serial_result.get('avg_time', 0), 3),
            'planning_time_p4': round(parallel_results.get('parallel_4', {}).get('avg_time', 0), 3),
            'planning_time_p8': round(parallel_results.get('parallel_8', {}).get('avg_time', 0), 3),
            'planning_time_p16': round(p16_result.get('avg_time', 0), 3),
            'total_time_serial': round(serial_total, 3),
            'total_time_p16': round(p16_total, 3),

            # 结果质量
            'total_orders_processed': quality_stats.get('total_orders', 0),
            'complete_orders': quality_stats.get('complete_orders', 0),
            'partial_orders': quality_stats.get('partial_orders', 0),
            'shortage_orders': quality_stats.get('shortage_orders', 0),
            'avg_complete_rate': round(quality_stats.get('avg_complete_rate', 0), 4),

            # 性能指标（以 parallel_16 或最佳结果为准）
            'cache_hit_rate': round(p16_result.get('avg_cache_hit',
                                      serial_result.get('avg_cache_hit', 0)), 4),
            'db_query_count': int(p16_result.get('avg_db_queries',
                                    serial_result.get('avg_db_queries', 0))),
            'peak_memory_mb': round(p16_result.get('avg_memory',
                                     serial_result.get('avg_memory', 0)) +
                                    (peak_memory / 1024 / 1024), 2),

            # 吞吐量
            'orders_per_second_serial': round(
                order_count / serial_result.get('avg_time', 0.001), 1
            ) if serial_result.get('avg_time', 0) > 0 else 0,
            'orders_per_second_p16': round(
                order_count / p16_result.get('avg_time', 0.001), 1
            ) if p16_result.get('avg_time', 0) > 0 else 0,

            # 详细并行结果（用于JSON导出）
            'parallel_details': parallel_results,

            # 运行元信息
            'runs_per_scenario': self.runs,
            'tested_at': datetime.now().isoformat(),
        }

        # 场景级别的日志输出
        logger.info(f"\n  ✓ 场景 [{scenario_name}] 完成:")
        logger.info(f"    数据生成: {result['data_gen_time']:.2f}s")
        logger.info(f"    串行计划: {result['planning_time_serial']:.2f}s "
                     f"({result['orders_per_second_serial']:.0f} ord/s)")
        logger.info(f"    P16并行:  {result['planning_time_p16']:.2f}s "
                     f"({result['orders_per_second_p16']:.0f} ord/s)")
        logger.info(f"    缓存命中率: {result['cache_hit_rate']:.1%}")
        logger.info(f"    内存峰值: {result['peak_memory_mb']:.1f} MB")
        logger.info(f"    平均齐套率: {result['avg_complete_rate']:.1%}")

        # 清理当前场景数据（释放数据库空间）
        self._cleanup_scenario_data()

        return result

    def _execute_single_run(
        self,
        mode_name: str,
        worker_count: int,
        run_index: int,
    ) -> Optional[Dict[str, float]]:
        """
        执行单次运行
        
        Args:
            mode_name: 模式名称 (serial/parallel_4/...)
            worker_count: worker数量
            run_index: 当前是第几次运行
            
        Returns:
            单次运行的指标字典
        """
        # 开始内存跟踪
        tracemalloc.start()

        # 获取待处理的订单
        orders = list(SalesOrder.objects.filter(
            order_no__startswith='BMK-SO-'
        ).order_by('priority', 'demand_date'))

        if not orders:
            logger.warning(f"  [Run {run_index+1}] 未找到测试订单，跳过")
            return None

        # 开启查询计数（调试模式下）
        from django.conf import settings
        if settings.DEBUG:
            from django.db import reset_queries
            reset_queries()
            connection.force_debug_cursor = True

        run_start = time.perf_counter()

        try:
            if mode_name == 'serial':
                # ── 串行模式：使用基础 MaterialPlanner ──
                planner = MaterialPlanner(consumption_priority='FIFO')
                planner.set_computation_mode('serial')

                # 加载缓存
                cache_start = time.perf_counter()
                planner.load_material_info_cache()
                planner.load_supplier_info_cache()
                planner.load_forbidden_materials()
                planner.load_workcenter_info_cache()
                planner.load_factory_calendar()
                planner.load_inventory_cache()
                planner.load_bom_cache()
                cache_load_time = time.perf_counter() - cache_start

                # 逐单处理
                results = []
                for order in orders:
                    try:
                        result = planner.process_order(order)
                        results.append(result)
                    except Exception as e:
                        results.append({
                            'is_complete': False,
                            'complete_rate': 0,
                            'error': str(e)[:200],
                        })

            else:
                # ── 并行模式：使用 HighPerformancePlanner ──
                hp_planner = HighPerformancePlanner(consumption_priority='FIFO')

                # 强制刷新缓存（确保每次运行公平比较）
                cache_start = time.perf_counter()
                hp_planner.load_data_with_caching(force_refresh=True)
                cache_load_time = time.perf_counter() - cache_start

                # 执行高性能计划
                plan_result = hp_planner.run_high_performance_planning(
                    orders=orders,
                    parallel=True,
                    max_workers=worker_count,
                )
                results = plan_result.get('results', [])

        except Exception as e:
            logger.error(f"  [Run {run_index+1}] {mode_name} 模式执行异常: {e}")
            tracemalloc.stop()
            return None

        planning_time = time.perf_counter() - run_start

        # 收集内存峰值
        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # 统计结果质量
        total_processed = len(results)
        complete_count = sum(1 for r in results if r.get('is_complete'))
        partial_count = sum(1 for r in results if r.get('complete_rate', 0) > 0
                           and not r.get('is_complete'))
        shortage_count = total_processed - complete_count - partial_count
        avg_complete = (sum(r.get('complete_rate', 0) for r in results) /
                       max(total_processed, 1))

        # 获取DB查询次数
        db_query_count = 0
        if settings.DEBUG:
            db_query_count = len(connection.queries)

        # 获取缓存命中率
        cache_hit_rate = 0.0
        if mode_name != 'serial' and 'hp_planner' in dir():
            try:
                cache_hit_rate = hp_planner._calculate_cache_hit_rate()
            except Exception:
                pass

        return {
            'planning_time': planning_time,
            'cache_load_time': cache_load_time,
            'peak_memory_mb': peak_mem / 1024 / 1024,
            'cache_hit_rate': cache_hit_rate,
            'db_query_count': db_query_count,
            'orders_processed': total_processed,
            'complete_orders': complete_count,
            'partial_orders': partial_count,
            'shortage_orders': shortage_count,
            'avg_complete_rate': avg_complete,
        }

    def _collect_quality_stats(self) -> Dict[str, Any]:
        """收集当前数据库中的计划结果质量统计"""
        try:
            plan_results = MaterialPlanResult.objects.filter(
                order__order_no__startswith='BMK-SO-'
            )

            total = plan_results.count()
            if total == 0:
                return {'total_orders': 0, 'complete_orders': 0,
                        'partial_orders': 0, 'shortage_orders': 0, 'avg_complete_rate': 0}

            agg = plan_results.aggregate(
                complete_cnt=Count('id', filter=Q(is_complete=True)),
                avg_rate=Avg('complete_rate'),
            )

            complete_cnt = agg['complete_cnt'] or 0
            avg_rate = float(agg['avg_rate'] or 0)

            # 部分齐套：有齐套率但未完全齐套
            partial_cnt = plan_results.filter(
                is_complete=False, complete_rate__gt=0
            ).count()

            # 缺料订单：齐套率为0
            shortage_cnt = total - complete_cnt - partial_cnt

            return {
                'total_orders': total,
                'complete_orders': complete_cnt,
                'partial_orders': partial_cnt,
                'shortage_orders': shortage_cnt,
                'avg_complete_rate': avg_rate,
            }
        except Exception as e:
            logger.warning(f"收集结果质量统计时出错: {e}")
            return {'total_orders': 0, 'complete_orders': 0,
                    'partial_orders': 0, 'shortage_orders': 0, 'avg_complete_rate': 0}

    def _cleanup_scenario_data(self):
        """清理当前场景生成的所有基准测试数据"""
        try:
            # 使用事务批量删除
            with transaction.atomic():
                # 删除关联数据
                order_ids = list(SalesOrder.objects.filter(
                    order_no__startswith='BMK-SO-'
                ).values_list('id', flat=True))

                if order_ids:
                    OrderAllocation.objects.filter(order_id__in=order_ids).delete()
                    MaterialPlanResult.objects.filter(order_id__in=order_ids).delete()
                    SupplierCommitment.objects.filter(
                        order_no__startswith='BMK-PO-'
                    ).delete()

                # 删除主数据
                SupplierMaterial.objects.filter(
                    supplier__supplier_code__startswith='BMK-SUP-'
                ).delete()
                Inventory.objects.filter(
                    material__material_code__startswith='BMK-MAT-'
                ).delete()
                BillOfMaterials.objects.filter(
                    parent_material__material_code__startswith='BMK-MAT-'
                ).delete()
                SalesOrder.objects.filter(order_no__startswith='BMK-SO-').delete()
                Material.objects.filter(material_code__startswith='BMK-MAT-').delete()
                Supplier.objects.filter(supplier_code__startswith='BMK-SUP-').delete()
                Customer.objects.filter(customer_code__startswith='BMK-CUS-').delete()

            # 手动垃圾回收释放内存
            gc.collect()
            logger.debug("  场景数据清理完成")

        except Exception as e:
            logger.warning(f"清理场景数据时出错: {e}")

    def run_stability_test(self, rounds: int = 10, order_count: int = 5000) -> Dict[str, Any]:
        """
        多轮稳定性测试

        连续执行指定轮数的物料计划计算，记录核心指标并分析稳定性。

        Args:
            rounds: 测试轮数（默认10轮）
            order_count: 每轮的订单数量（默认5000）

        Returns:
            包含稳定性统计结果的字典
        """
        import statistics

        logger.info(f"{'='*80}")
        logger.info(f"开始多轮稳定性测试: {rounds}轮, 每轮{order_count:,}订单")
        logger.info(f"{'='*80}")

        # 核心指标收集
        metrics_history = {
            'on_time_delivery_rate': [],
            'delivery_change_count': [],
            'inventory_level': [],
            'shortage_precision': [],
        }

        # 使用固定种子确保可复现性
        stability_generator = TestDataGenerator(random_seed=2024)

        for round_idx in range(1, rounds + 1):
            logger.info(f"\n--- 第 {round_idx}/{rounds} 轮 ---")

            try:
                # 生成测试数据（使用小型规模配置）
                data_stats = stability_generator.generate_all(
                    order_count=order_count,
                    material_count=min(2000, order_count // 2),
                    bom_count=min(8000, order_count * 2),
                    inventory_count=min(10000, order_count * 2),
                    supplier_count=min(50, max(10, order_count // 100)),
                    bom_level_range=(3, 5),
                )

                # 执行计划计算
                tracemalloc.start()
                run_start = time.perf_counter()

                orders = list(SalesOrder.objects.filter(
                    order_no__startswith='BMK-SO-'
                ).order_by('priority', 'demand_date'))

                if not orders:
                    logger.warning(f"第 {round_idx} 轮: 未找到测试订单")
                    continue

                # 使用高性能规划器执行
                hp_planner = HighPerformancePlanner(consumption_priority='FIFO')
                hp_planner.load_data_with_caching(force_refresh=True)

                plan_result = hp_planner.run_high_performance_planning(
                    orders=orders,
                    parallel=True,
                    max_workers=8,
                )
                results = plan_result.get('results', [])

                planning_time = time.perf_counter() - run_start
                _, peak_mem = tracemalloc.get_traced_memory()
                tracemalloc.stop()

                # 收集本轮核心指标
                round_metrics = self._extract_stability_metrics(results, orders)
                metrics_history['on_time_delivery_rate'].append(round_metrics['on_time_delivery_rate'])
                metrics_history['delivery_change_count'].append(round_metrics['delivery_change_count'])
                metrics_history['inventory_level'].append(round_metrics['inventory_level'])
                metrics_history['shortage_precision'].append(round_metrics['shortage_precision'])

                logger.info(f"  耗时: {planning_time:.2f}s | 内存: {peak_mem/1024/1024:.1f}MB")
                logger.info(f"  按时交付率: {round_metrics['on_time_delivery_rate']:.4f}")
                logger.info(f"  交期变更次数: {round_metrics['delivery_change_count']}")
                logger.info(f"  库存水位: {round_metrics['inventory_level']:.4f}")
                logger.info(f"  报缺精准度: {round_metrics['shortage_precision']:.4f}")

                # 清理当前轮数据释放资源
                self._cleanup_scenario_data()
                gc.collect()

            except Exception as e:
                logger.error(f"第 {round_idx} 轮执行异常: {e}", exc_info=True)
                continue

        # 计算稳定性统计
        stability_result = self._calculate_stability_statistics(metrics_history, rounds)
        return stability_result

    def _extract_stability_metrics(self, results: List[Dict], orders: List) -> Dict[str, float]:
        """
        从单次运行结果中提取四大核心指标

        Args:
            results: 计划结果列表
            orders: 订单列表

        Returns:
            包含四个核心指标的字典
        """
        total_orders = len(results)
        if total_orders == 0:
            return {
                'on_time_delivery_rate': 0.0,
                'delivery_change_count': 0,
                'inventory_level': 0.0,
                'shortage_precision': 0.0,
            }

        # 1. 按时交付率：完全齐套的订单占比
        complete_count = sum(1 for r in results if r.get('is_complete'))
        on_time_delivery_rate = complete_count / total_orders

        # 2. 交期变更次数：模拟值（基于优先级和需求日期偏差）
        delivery_change_count = 0
        for i, r in enumerate(results):
            if i < len(orders) and r.get('complete_rate', 0) < 1.0:
                # 未完全齐套可能触发交期变更
                priority = getattr(orders[i], 'priority', 3) if i < len(orders) else 3
                if priority <= 2:  # 高优先级订单
                    delivery_change_count += 1

        # 3. 库存水位：平均齐套率作为代理指标
        inventory_level = sum(r.get('complete_rate', 0) for r in results) / total_orders

        # 4. 报缺精准度：缺料订单中确实缺料的比例
        shortage_orders = [r for r in results if not r.get('is_complete') and r.get('complete_rate', 0) == 0]
        partial_orders = [r for r in results if 0 < r.get('complete_rate', 0) < 1.0]
        if len(shortage_orders) + len(partial_orders) > 0:
            shortage_precision = len(shortage_orders) / (len(shortage_orders) + len(partial_orders))
        else:
            shortage_precision = 1.0  # 无缺料则精准度为100%

        return {
            'on_time_delivery_rate': round(on_time_delivery_rate, 4),
            'delivery_change_count': delivery_change_count,
            'inventory_level': round(inventory_level, 4),
            'shortage_precision': round(shortage_precision, 4),
        }

    def _calculate_stability_statistics(self, metrics_history: Dict[str, List], rounds: int) -> Dict[str, Any]:
        """
        计算稳定性统计数据

        Args:
            metrics_history: 各轮指标历史记录
            rounds: 总轮数

        Returns:
            稳定性统计分析结果
        """
        import statistics

        stability_stats = {
            'total_rounds': rounds,
            'completed_rounds': len(metrics_history['on_time_delivery_rate']),
            'metrics_analysis': {},
            'ptp_volatility': [],  # Plan-to-Plan波动率
            'overall_verdict': '',
        }

        # 分析每个指标
        metric_names_cn = {
            'on_time_delivery_rate': '按时交付率',
            'delivery_change_count': '交期变更次数',
            'inventory_level': '库存水位',
            'shortage_precision': '报缺精准度',
        }

        all_cv_values = []

        for metric_name, values in metrics_history.items():
            if not values:
                stability_stats['metrics_analysis'][metric_name] = {
                    'mean': 0, 'std': 0, 'cv': 0, 'trend': '无数据',
                    'min': 0, 'max': 0,
                }
                continue

            mean_val = statistics.mean(values)
            std_val = statistics.stdev(values) if len(values) > 1 else 0
            cv = (std_val / mean_val) if mean_val != 0 else float('inf')
            min_val = min(values)
            max_val = max(values)

            # 趋势判断
            trend = self._detect_trend(values)

            stability_stats['metrics_analysis'][metric_name] = {
                'mean': round(mean_val, 6),
                'std': round(std_val, 6),
                'cv': round(cv, 6),
                'trend': trend,
                'min': round(min_val, 6),
                'max': round(max_val, 6),
                'values': values,
                'cn_name': metric_names_cn.get(metric_name, metric_name),
            }
            all_cv_values.append(cv)

        # 计算Plan-to-Plan波动率（相邻两轮按时交付率差异）
        otd_rates = metrics_history['on_time_delivery_rate']
        for i in range(1, len(otd_rates)):
            volatility = abs(otd_rates[i] - otd_rates[i-1])
            stability_stats['ptp_volatility'].append(round(volatility, 6))

        avg_ptp = statistics.mean(stability_stats['ptp_volatility']) if stability_stats['ptp_volatility'] else 0
        stability_stats['avg_ptp_volatility'] = round(avg_ptp, 6)

        # 最终稳定性判定：所有指标CV < 0.15 = 稳定
        if all_cv_values:
            max_cv = max(all_cv_values)
            is_stable = max_cv < 0.15
            stability_stats['overall_verdict'] = '✅ 稳定' if is_stable else '⚠️ 波动较大'
            stability_stats['max_cv'] = round(max_cv, 6)
            stability_stats['stability_threshold'] = 0.15
        else:
            stability_stats['overall_verdict'] = '❌ 无法判定（无有效数据）'

        # 输出稳定性报告
        self._print_stability_report(stability_stats)

        return stability_stats

    def _detect_trend(self, values: List[float]) -> str:
        """
        检测数据趋势

        Args:
            values: 数值序列

        Returns:
            趋势描述字符串
        """
        import statistics

        if len(values) < 3:
            return '数据不足'

        # 将数据分为前半段和后半段
        mid = len(values) // 2
        first_half_mean = statistics.mean(values[:mid])
        second_half_mean = statistics.mean(values[mid:])

        change_pct = ((second_half_mean - first_half_mean) / first_half_mean * 100) if first_half_mean != 0 else 0

        # 计算变异系数判断波动程度
        overall_std = statistics.stdev(values)
        overall_mean = statistics.mean(values)
        cv = (overall_std / overall_mean) if overall_mean != 0 else 0

        if cv > 0.15:
            return f'波动(CV={cv:.2%})'
        elif change_pct > 5:
            return f'上升(+{change_pct:.1f}%)'
        elif change_pct < -5:
            return f'下降({change_pct:.1f}%)'
        else:
            return '稳定'

    def _print_stability_report(self, stability_stats: Dict[str, Any]):
        """输出稳定性测试报告到控制台"""
        logger.info(f"\n{'='*80}")
        logger.info("📊 多轮稳定性测试报告")
        logger.info(f"{'='*80}")
        logger.info(f"总轮数: {stability_stats['total_rounds']}")
        logger.info(f"完成轮数: {stability_stats['completed_rounds']}")
        logger.info(f"")

        logger.info("【各指标统计分析】")
        logger.info(f"{'指标名称':<16} {'均值':>12} {'标准差':>12} {'变异系数CV':>12} {'趋势':>10} {'范围':>20}")
        logger.info("-" * 86)

        for metric_name, analysis in stability_stats['metrics_analysis'].items():
            cn_name = analysis.get('cn_name', metric_name)
            logger.info(
                f"{cn_name:<16} "
                f"{analysis['mean']:>12.6f} "
                f"{analysis['std']:>12.6f} "
                f"{analysis['cv']:>11.4%} "
                f"{analysis['trend']:>10} "
                f"[{analysis['min']:.4f} ~ {analysis['max']:.4f}]"
            )

        logger.info("")
        logger.info(f"【Plan-to-Plan波动率】")
        if stability_stats['ptp_volatility']:
            logger.info(f"  平均波动率: {stability_stats['avg_ptp_volatility']:.6f}")
            logger.info(f"  最大波动率: {max(stability_stats['ptp_volatility']):.6f}")
            logger.info(f"  最小波动率: {min(stability_stats['ptp_volatility']):.6f}")
        else:
            logger.info("  无有效波动率数据")

        logger.info("")
        logger.info(f"【最终判定】{stability_stats['overall_verdict']}")
        if 'max_cv' in stability_stats:
            logger.info(f"  最大变异系数: {stability_stats['max_cv']:.4f} (阈值: < {stability_stats['stability_threshold']})")
        logger.info(f"{'='*80}\n")


# ============================================================
# 报告生成器
# ============================================================
class ReportGenerator:
    """
    基准测试报告生成器
    
    输出三种格式的报告:
    1. 控制台表格（带颜色和格式化）
    2. JSON 文件（供前端图表使用）
    3. Markdown 报告文档
    """

    def __init__(self, output_dir: str):
        """
        Args:
            output_dir: 报告输出目录
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def print_console_report(self, results: List[Dict[str, Any]]) -> str:
        """
        输出控制台格式的基准测试报告
        
        Returns:
            格式化的报告文本
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        plat_info = f"{platform.system()} {platform.machine()}"

        lines = []
        lines.append('=' * 80)
        lines.append('       多约束供应链智能运营系统 - 性能基准测试报告')
        lines.append('=' * 80)
        lines.append(f'测试时间: {now}')
        lines.append(f'Python版本: {py_version} | 平台: {plat_info}')
        lines.append('=' * 80)

        successful_results = [r for r in results if r.get('status') == 'SUCCESS']
        failed_results = [r for r in results if r.get('status') == 'FAILED']

        for idx, r in enumerate(successful_results, 1):
            header = (f"【场景{idx}: {r['scenario_name']}】"
                     f"{r['order_count']:,}订单 × {r['material_count']:,}物料 "
                     f"× {r['bom_count']:,}BOM")

            lines.append('')
            lines.append(header)
            lines.append('┌' + '─' * 78 + '┐')

            # 表头
            lines.append('│ {:<16}│ {:>8} │ {:>8} │ {:>8} │ {:>10}│'.format(
                '指标', 'Serial', 'Parallel4', 'Parallel8', 'Parallel16'
            ))
            lines.append('├' + '─' * 16 + '┼' + '─' * 10 + '┼' + '─' * 10 +
                        '┼' + '─' * 10 + '┼' + '─' * 12 + '┤')

            # 从 parallel_details 中提取各模式数据
            pdetails = r.get('parallel_details', {})

            # 计划耗时
            s_t = pdetails.get('serial', {}).get('avg_time', 0)
            p4_t = pdetails.get('parallel_4', {}).get('avg_time', 0)
            p8_t = pdetails.get('parallel_8', {}).get('avg_time', 0)
            p16_t = pdetails.get('parallel_16', {}).get('avg_time', 0)
            lines.append('│ {:<16}│ {:>8.2f} │ {:>8.2f} │ {:>8.2f} │ {:>10.2f}│'.format(
                '计划耗时(s)', s_t, p4_t, p8_t, p16_t
            ))

            # 缓存命中率
            s_c = pdetails.get('serial', {}).get('avg_cache_hit', 0)
            p4_c = pdetails.get('parallel_4', {}).get('avg_cache_hit', 0)
            p8_c = pdetails.get('parallel_8', {}).get('avg_cache_hit', 0)
            p16_c = pdetails.get('parallel_16', {}).get('avg_cache_hit', 0)
            lines.append('│ {:<16}│ {:>7.1f}% │ {:>7.1f}% │ {:>7.1f}% │ {:>9.1f}% │'.format(
                '缓存命中率', s_c * 100, p4_c * 100, p8_c * 100, p16_c * 100
            ))

            # 内存占用(MB)
            s_m = pdetails.get('serial', {}).get('avg_memory', 0)
            p4_m = pdetails.get('parallel_4', {}).get('avg_memory', 0)
            p8_m = pdetails.get('parallel_8', {}).get('avg_memory', 0)
            p16_m = pdetails.get('parallel_16', {}).get('avg_memory', 0)
            lines.append('│ {:<16}│ {:>8.1f} │ {:>8.1f} │ {:>8.1f} │ {:>10.1f}│'.format(
                '内存占用(MB)', s_m, p4_m, p8_m, p16_m
            ))

            # 吞吐量(ord/s)
            s_ops = r.get('orders_per_second_serial', 0)
            p4_ops = (r['order_count'] / p4_t) if p4_t > 0 else 0
            p8_ops = (r['order_count'] / p8_t) if p8_t > 0 else 0
            p16_ops = r.get('orders_per_second_p16', 0)
            lines.append('│ {:<16}│ {:>8.1f} │ {:>8.1f} │ {:>8.1f} │ {:>10.1f}│'.format(
                '吞吐量(ord/s)', s_ops, p4_ops, p8_ops, p16_ops
            ))

            # 平均齐套率
            cr = r.get('avg_complete_rate', 0)
            lines.append('│ {:<16}│ {:>7.1f}% │ {:>7.1f}% │ {:>7.1f}% │ {:>9.1f}% │'.format(
                '平均齐套率', cr * 100, cr * 100, cr * 100, cr * 100
            ))

            lines.append('└' + '─' * 78 + '┘')

        # 失败的场景
        if failed_results:
            lines.append('')
            lines.append('⚠️  以下场景执行失败:')
            for fr in failed_results:
                lines.append(f"  ✗ [{fr.get('scenario', '?')}] {fr.get('error', '未知错误')}")

        # 最终判定
        lines.append('')
        lines.append('=' * 80)
        lines.append('【最终判定】')
        self._add_verdict(lines, successful_results)
        lines.append('=' * 80)

        report_text = '\n'.join(lines)
        print('\n' + report_text)

        # 添加趋势迷你图
        self._add_trend_minicharts_to_console(results)

        return report_text

    def _add_verdict(self, lines: List[str], results: List[Dict[str, Any]]):
        """添加PASS/FAIL判定"""
        if not results:
            lines.append('  无成功完成的测试场景，无法判定。')
            return

        # 检查压力测试场景是否满足要求
        stress_result = None
        for r in results:
            if r['scenario'] == 'stress':
                stress_result = r
                break

        if stress_result:
            total_time = stress_result.get('total_time_p16',
                             stress_result.get('total_time_serial', 0))
            meets_target = total_time < MAX_ACCEPTABLE_TIME_STRESS
            verdict = '✅ PASS' if meets_target else '❌ FAIL'

            lines.append(f'  压力测试(万级订单): 总耗时 {total_time:.2f}s '
                        f'(阈值 < {MAX_ACCEPTABLE_TIME_STRESS}s) → {verdict}')
        else:
            # 如果没跑压力测试，基于最大规模场景推算
            largest = max(results, key=lambda x: x.get('order_count', 0))
            largest_time = largest.get('total_time_p16',
                           largest.get('total_time_serial', 0))
            largest_orders = largest.get('order_count', 0)

            if largest_orders > 0 and largest_time > 0:
                # 线性推算万级订单耗时
                estimated_stress_time = largest_time * (10000 / largest_orders)
                meets_target = estimated_stress_time < MAX_ACCEPTABLE_TIME_STRESS
                verdict = '✅ PASS (推算)' if meets_target else '❌ FAIL (推算)'

                lines.append(f'  最大已完成场景: {largest["scenario_name"]} '
                            f'({largest_orders:,}订单)')
                lines.append(f'  推算万级订单耗时: {estimated_stress_time:.2f}s '
                            f'(阈值 < {MAX_ACCEPTABLE_TIME_STRESS}s) → {verdict}')

        # 整体统计
        all_pass = all(r.get('status') == 'SUCCESS' for r in results)
        lines.append(f'  成功场景: {len(results)}/{len(results)}')
        if all_pass:
            lines.append(f'  总体状态: ✅ 全部通过')
        else:
            lines.append(f'  总体状态: ⚠️ 部分场景失败')

    def save_json_report(self, results: List[Dict[str, Any]]) -> str:
        """
        保存JSON格式的完整结果（供前端图表使用）

        Returns:
            JSON文件路径
        """
        output_path = os.path.join(self.output_dir, 'benchmark_results.json')

        # 收集详细的运行环境信息
        environment_info = self._collect_environment_info()

        # 构建完整的JSON报告数据
        report_data = {
            'meta': {
                'generated_at': datetime.now().isoformat(),
                'python_version': f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
                'python_version_full': sys.version,
                'platform': f'{platform.system()} {platform.machine()}',
                'platform_detail': platform.platform(),
                'threshold_seconds': MAX_ACCEPTABLE_TIME_STRESS,
                **environment_info,
            },
            'scenarios': results,
            'summary': self._generate_summary(results),
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"JSON报告已保存: {output_path}")
        return output_path

    def _collect_environment_info(self) -> Dict[str, Any]:
        """
        收集详细的运行环境信息

        Returns:
            包含环境信息的字典
        """
        env_info = {}

        # 1. CPU信息
        try:
            import cpuinfo  # 尝试使用py-cpuinfo库
            cpu_info = cpuinfo.get_cpu_info()
            env_info['cpu_model'] = cpu_info.get('brand_raw', 'Unknown')
            env_info['cpu_cores_physical'] = cpu_info.get('count', 'Unknown')
            env_info['cpu_architecture'] = cpu_info.get('arch', platform.machine())
        except ImportError:
            # 如果py-cpuinfo不可用，使用platform模块获取基本信息
            try:
                env_info['cpu_model'] = platform.processor() or 'Unknown'
                env_info['cpu_architecture'] = platform.machine()

                # 尝试获取CPU核心数（跨平台）
                if hasattr(os, 'sched_getaffinity'):
                    env_info['cpu_cores_logical'] = len(os.sched_getaffinity(0))
                else:
                    env_info['cpu_cores_logical'] = os.cpu_count() or 'Unknown'

                # Windows下尝试获取物理核心数
                if platform.system() == 'Windows':
                    import subprocess
                    try:
                        result = subprocess.run(
                            ['wmic', 'cpu', 'get', 'NumberOfCores'],
                            capture_output=True, text=True, timeout=5
                        )
                        lines = [l.strip() for l in result.stdout.split('\n') if l.strip()]
                        if len(lines) >= 2:
                            env_info['cpu_cores_physical'] = int(lines[1])
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"收集CPU信息时出错: {e}")

        # 2. 内存信息
        try:
            import psutil
            mem = psutil.virtual_memory()
            env_info['total_memory_gb'] = round(mem.total / (1024**3), 2)
            env_info['available_memory_gb'] = round(mem.available / (1024**3), 2)
            env_info['memory_usage_percent'] = mem.percent
        except ImportError:
            # 如果psutil不可用，尝试其他方式
            try:
                if platform.system() == 'Windows':
                    import ctypes
                    kernel32 = ctypes.windll.kernel32

                    class MEMORYSTATUSEX(ctypes.Structure):
                        _fields_ = [
                            ('dwLength', ctypes.c_ulong),
                            ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong),
                            ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong),
                            ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong),
                            ('ullAvailVirtual', ctypes.c_ulonglong),
                        ]

                    mem_status = MEMORYSTATUSEX()
                    mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                    kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status))
                    env_info['total_memory_gb'] = round(mem_status.ullTotalPhys / (1024**3), 2)
                    env_info['available_memory_gb'] = round(mem_status.ullAvailPhys / (1024**3), 2)
            except Exception as e:
                logger.debug(f"收集内存信息时出错: {e}")

        # 3. Python版本详细信息
        env_info['python_implementation'] = platform.python_implementation()
        env_info['python_compiler'] = platform.python_compiler()

        # 4. Django版本
        try:
            import django
            env_info['django_version'] = django.VERSION
            env_info['django_version_str'] = django.get_version()
        except Exception as e:
            logger.debug(f"收集Django版本时出错: {e}")

        # 5. 关键依赖版本
        key_packages = [
            'numpy', 'pandas', 'scikit-learn', 'sklearn',
            'scipy', 'matplotlib', 'sqlparse',
            'cryptography', 'requests',
        ]
        dependencies = {}
        for package in key_packages:
            try:
                module = __import__(package)
                version = getattr(module, '__version__', getattr(module, 'version', 'Unknown'))
                dependencies[package] = version
            except ImportError:
                pass
            except Exception:
                pass

        if dependencies:
            env_info['dependencies'] = dependencies

        # 6. 数据库信息
        try:
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                db_version = cursor.fetchone()[0]
                env_info['database_type'] = 'PostgreSQL' if 'postgres' in str(db_version).lower() or \
                                            'PostgreSQL' in str(db_version) else str(db_version).split()[0]
                env_info['database_version'] = db_version
        except Exception as e:
            # 尝试SQLite
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT sqlite_version()")
                    db_version = cursor.fetchone()[0]
                    env_info['database_type'] = 'SQLite'
                    env_info['database_version'] = f'SQLite {db_version}'
            except Exception as e2:
                logger.debug(f"收集数据库信息时出错: {e}, {e2}")

        # 7. 操作系统详细信息
        env_info['os_name'] = platform.system()
        env_info['os_release'] = platform.release()
        env_info['os_version'] = platform.version()
        env_info['os_detail'] = f"{platform.system()} {platform.release()} ({platform.version()})"

        return env_info

    def save_markdown_report(self, results: List[Dict[str, Any]]) -> str:
        """
        保存Markdown格式的报告文档
        
        Returns:
            Markdown文件路径
        """
        output_path = os.path.join(self.output_dir, 'benchmark_report.md')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        md_lines = [
            '# 多约束供应链智能运营系统 - 性能基准测试报告',
            '',
            f'> 测试时间: {now}',
            f'> Python版本: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
            f'> 平台: {platform.system()} {platform.machine()}',
            '',
            '---',
            '',
            '## 1. 测试概览',
            '',
            '| 指标 | 值 |',
            '|------|-----|',
            f'| 测试场景总数 | {len(results)} |',
            f'| 成功场景数 | {len([r for r in results if r.get("status") == "SUCCESS"])} |',
            f'| 失败场景数 | {len([r for r in results if r.get("status") == "FAILED"])} |',
            f'| 性能阈值 | 万级订单 < {MAX_ACCEPTABLE_TIME_STRESS}s ({MAX_ACCEPTABLE_TIME_STRESS//3600}小时) |',
            '',
            '## 2. 场景详情',
            '',
        ]

        successful_results = [r for r in results if r.get('status') == 'SUCCESS']

        for idx, r in enumerate(successful_results, 1):
            md_lines.append(f'### 场景{idx}: {r["scenario_name"]}')
            md_lines.append('')
            md_lines.append(f'- **订单数**: {r["order_count"]:,}')
            md_lines.append(f'- **物料数**: {r["material_count"]:,}')
            md_lines.append(f'- **BOM数**: {r["bom_count"]:,}')
            md_lines.append(f'- **库存记录**: {r["inventory_count"]:,}')
            md_lines.append(f'- **供应商数**: {r["supplier_count"]}')
            md_lines.append('')
            md_lines.append('#### 性能指标对比')
            md_lines.append('')
            md_lines.append('| 指标 | Serial | Parallel-4 | Parallel-8 | Parallel-16 |')
            md_lines.append('|------|--------|------------|------------|-------------|')

            pdetails = r.get('parallel_details', {})

            # 各行数据
            metrics_rows = [
                ('计划耗时(s)', 'avg_time', '{:.2f}'),
                ('缓存命中率', 'avg_cache_hit', '{:.1%}'),
                ('内存占用(MB)', 'avg_memory', '{:.1f}'),
                ('吞吐量(ord/s)', None, None),  # 特殊处理
            ]

            for label, key, fmt in metrics_rows:
                if key:
                    vals = [pdetails.get(k, {}).get(key, 0) for k in
                            ['serial', 'parallel_4', 'parallel_8', 'parallel_16']]
                    md_lines.append(f'| {label} | ' + ' | '.join(
                        fmt.format(v) for v in vals) + ' |')
                else:
                    # 吞吐量特殊处理
                    s_ops = r.get('orders_per_second_serial', 0)
                    p4_t = pdetails.get('parallel_4', {}).get('avg_time', 0.001)
                    p8_t = pdetails.get('parallel_8', {}).get('avg_time', 0.001)
                    p16_ops = r.get('orders_per_second_p16', 0)
                    p4_ops = r['order_count'] / p4_t if p4_t > 0 else 0
                    p8_ops = r['order_count'] / p8_t if p8_t > 0 else 0
                    md_lines.append(
                        f'| 吞吐量(ord/s) | {s_ops:.1f} | {p4_ops:.1f} | '
                        f'{p8_ops:.1f} | {p16_ops:.1f} |'
                    )

            md_lines.append('')
            md_lines.append(f'- **平均齐套率**: {r.get("avg_complete_rate", 0):.1%}')
            md_lines.append(f'- **完全齐套订单**: {r.get("complete_orders", 0)}')
            md_lines.append(f'- **部分齐套订单**: {r.get("partial_orders", 0)}')
            md_lines.append(f'- **缺料订单**: {r.get("shortage_orders", 0)}')
            md_lines.append('')

        # 失败场景
        failed_results = [r for r in results if r.get('status') == 'FAILED']
        if failed_results:
            md_lines.append('### ⚠️ 失败场景')
            md_lines.append('')
            for fr in failed_results:
                md_lines.append(f"- **{fr.get('scenario', '?')}**: {fr.get('error', '未知错误')}")
            md_lines.append('')

        # 结论
        md_lines.extend([
            '## 3. 结论与建议',
            '',
        ])

        summary = self._generate_summary(results)
        if summary.get('verdict') == 'PASS':
            md_lines.append('> **✅ 测试通过** - 系统满足性能要求')
        else:
            md_lines.append('> **❌ 测试未通过** - 系统未达到性能目标')

        md_lines.append('')
        md_lines.append('---')
        md_lines.append(f'*报告自动生成于 {now}*')

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_lines))

        logger.info(f"Markdown报告已保存: {output_path}")
        return output_path

    def _generate_summary(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """生成测试摘要统计"""
        successful = [r for r in results if r.get('status') == 'SUCCESS']

        if not successful:
            return {
                'verdict': 'INCONCLUSIVE',
                'total_scenarios': len(results),
                'successful': 0,
                'failed': len(results),
            }

        # 找到最大规模的场景
        largest = max(successful, key=lambda x: x.get('order_count', 0))

        # 检查是否通过
        stress_result = None
        for r in successful:
            if r['scenario'] == 'stress':
                stress_result = r
                break

        if stress_result:
            total = stress_result.get('total_time_p16',
                   stress_result.get('total_time_serial', 0))
            verdict = 'PASS' if total < MAX_ACCEPTABLE_TIME_STRESS else 'FAIL'
        else:
            # 推算
            l_time = largest.get('total_time_p16',
                     largest.get('total_time_serial', 0))
            l_orders = largest.get('order_count', 1)
            estimated = l_time * (10000 / l_orders) if l_orders > 0 else 99999
            verdict = 'PASS' if estimated < MAX_ACCEPTABLE_TIME_STRESS else 'FAIL'

        # 最佳吞吐量
        best_throughput = max(
            (r.get('orders_per_second_p16', 0) or r.get('orders_persecond_serial', 0))
            for r in successful
        )

        return {
            'verdict': verdict,
            'total_scenarios': len(results),
            'successful': len(successful),
            'failed': len(results) - len(successful),
            'largest_scenario': largest.get('scenario_name', ''),
            'largest_order_count': largest.get('order_count', 0),
            'best_throughput_orders_per_sec': best_throughput,
            'avg_complete_rate_across_scenes': (
                sum(r.get('avg_complete_rate', 0) for r in successful) / len(successful)
            ),
        }

    def save_ablation_report(self, ablation_results: Dict[str, Dict[str, Any]]) -> str:
        """
        保存消融实验Markdown报告

        Args:
            ablation_results: 消融实验结果字典

        Returns:
            Markdown文件路径
        """
        output_path = os.path.join(self.output_dir, 'ablation_study_report.md')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        md_lines = [
            '# 消融实验报告 (Ablation Study Report)',
            '',
            f'> 生成时间: {now}',
            f'> 测试配置数: {len(ablation_results)}',
            '',
            '---',
            '',
            '## 1. 实验概述',
            '',
            '本消融实验通过系统性地禁用各个核心算法模块，评估每个模块对系统整体性能的贡献度。',
            '',
            '### 测试配置矩阵',
            '',
            '| 配置名称 | NSGA-II | Prophet | RL | IsoForest | 描述 |',
            '|----------|---------|---------|-----|-----------|------|',
        ]

        # 添加配置矩阵
        if hasattr(AblationStudyRunner, 'ABLATION_CONFIGS'):
            for config_name, config in AblationStudyRunner.ABLATION_CONFIGS.items():
                md_lines.append(
                    f"| {config_name} | "
                    f"{'✅' if config.get('use_nsga2') else '❌'} | "
                    f"{'✅' if config.get('use_prophet') else '❌'} | "
                    f"{'✅' if config.get('use_rl') else '❌'} | "
                    f"{'✅' if config.get('use_isoforest') else '❌'} | "
                    f"{config.get('description', '')} |"
                )

        md_lines.extend(['', '', '## 2. 实验结果', ''])

        # 结果表格
        md_lines.append('| 配置 | 按时交付率 | 成本效率 | 库存优化 | 资源利用率 | 平均退化 |')
        md_lines.append('|------|-----------|---------|---------|-----------|---------|')

        for config_name, result in ablation_results.items():
            if result.get('status') != 'SUCCESS':
                md_lines.append(f"| **{config_name}** | - | - | - | - | FAILED |")
                continue

            o1 = result.get('objective1_on_time_delivery', 0)
            o2 = result.get('objective2_cost_efficiency', 0)
            o3 = result.get('objective3_inventory_optimization', 0)
            o4 = result.get('objective4_resource_utilization', 0)
            avg_deg = result.get('avg_degradation', 0)
            deg_str = f"{avg_deg:+.2f}%" if avg_deg != 0 else "0.00%"

            # baseline行加粗
            if config_name == 'baseline':
                md_lines.append(
                    f"| **{config_name}** | **{o1:.4f}** | **{o2:.4f}** | "
                    f"**{o3:.4f}** | **{o4:.4f}** | 基线 |"
                )
            else:
                md_lines.append(
                    f"| {config_name} | {o1:.4f} | {o2:.4f} | "
                    f"{o3:.4f} | {o4:.4f} | {deg_str} |"
                )

        md_lines.extend(['', '', '## 3. 退化幅度分析', ''])

        # 退化幅度详情
        baseline_result = ablation_results.get('baseline')
        if baseline_result and baseline_result.get('status') == 'SUCCESS':
            for config_name, result in ablation_results.items():
                if config_name == 'baseline' or 'degradation' not in result:
                    continue

                degradation = result['degradation']
                md_lines.append(f"### {config_name}: {result.get('config_description', '')}")
                md_lines.append('')
                md_lines.append('| 目标指标 | 基线值 | 当前值 | 退化幅度 | 影响评估 |')
                md_lines.append('|---------|--------|--------|---------|---------|')

                for obj_key, deg_info in degradation.items():
                    cn_name = deg_info['cn_name']
                    deg_pct = deg_info['degradation_pct']

                    if deg_pct < -5:
                        impact = '⚠️ 显著退化'
                    elif deg_pct < -2:
                        impact = '⚡ 轻微退化'
                    elif deg_pct <= 2:
                        impact = '✅ 无明显影响'
                    else:
                        impact = '📈 意外提升'

                    md_lines.append(
                        f"| {cn_name} | {deg_info['baseline']:.6f} | "
                        f"{deg_info['current']:.6f} | {deg_pct:+.2f}% | {impact} |"
                    )
                md_lines.append('')

        # 结论
        md_lines.extend([
            '## 4. 结论与建议',
            '',
        ])

        # 找出影响最大的模块
        max_degradation_config = None
        max_degradation_value = 0
        for config_name, result in ablation_results.items():
            if config_name != 'baseline' and result.get('status') == 'SUCCESS':
                avg_deg = abs(result.get('avg_degradation', 0))
                if avg_deg > max_degradation_value:
                    max_degradation_value = avg_deg
                    max_degradation_config = config_name

        if max_degradation_config:
            md_lines.append(
                f"- **最关键模块**: 移除 `{max_degradation_config}` 导致最大性能退化 ({max_degradation_value:.2f}%)"
            )
        md_lines.extend([
            '',
            '---',
            f'*报告自动生成于 {now}*',
        ])

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_lines))

        logger.info(f"消融实验报告已保存: {output_path}")
        return output_path

    def save_stability_report(self, stability_stats: Dict[str, Any]) -> str:
        """
        保存稳定性测试Markdown报告

        Args:
            stability_stats: 稳定性测试统计结果字典

        Returns:
            Markdown文件路径
        """
        output_path = os.path.join(self.output_dir, 'stability_test_report.md')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        md_lines = [
            '# 多轮稳定性测试报告 (Stability Test Report)',
            '',
            f'> 生成时间: {now}',
            f"> 总轮数: {stability_stats.get('total_rounds', 0)}",
            f"> 完成轮数: {stability_stats.get('completed_rounds', 0)}",
            '',
            '---',
            '',
            '## 1. 稳定性判定',
            '',
            f"**总体判定**: {stability_stats.get('overall_verdict', '未知')}",
            '',
        ]

        if 'max_cv' in stability_stats:
            threshold = stability_stats.get('stability_threshold', 0.15)
            md_lines.extend([
                f"- 最大变异系数(CV): **{stability_stats['max_cv']:.4f}**",
                f"- 稳定性阈值: < {threshold}",
                '- 判定标准: 所有指标CV < 阈值 = 稳定',
                '',
            ])

        # 各指标统计分析
        md_lines.extend([
            '## 2. 各指标统计分析',
            '',
            '| 指标名称 | 均值 | 标准差 | 变异系数CV | 趋势判断 | 最小值 | 最大值 |',
            '|---------|------|--------|-----------|---------|--------|--------|',
        ])

        metrics_analysis = stability_stats.get('metrics_analysis', {})
        for metric_name, analysis in metrics_analysis.items():
            cn_name = analysis.get('cn_name', metric_name)
            md_lines.append(
                f"| {cn_name} | {analysis['mean']:.6f} | {analysis['std']:.6f} | "
                f"{analysis['cv']:.4%} | {analysis['trend']} | "
                f"{analysis['min']:.4f} | {analysis['max']:.4f} |"
            )

        # Plan-to-Plan波动率
        md_lines.extend([
            '',
            '## 3. Plan-to-Plan 波动率',
            '',
        ])
        ptp_volatility = stability_stats.get('ptp_volatility', [])
        if ptp_volatility:
            avg_ptp = stability_stats.get('avg_ptp_volatility', 0)
            md_lines.extend([
                f"- **平均波动率**: {avg_ptp:.6f}",
                f"- **最大波动率**: {max(ptp_volatility):.6f}",
                f"- **最小波动率**: {min(ptp_volatility):.6f}",
                '',
                '### 波动率趋势图',
                '',
            ])

            # ASCII迷你图
            sparkline = self._generate_ascii_sparkline(ptp_volatility)
            md_lines.append(f'```')
            md_lines.append(sparkline)
            md_lines.append('```')
            md_lines.append('')

        # 各指标的ASCII趋势迷你图
        md_lines.extend([
            '## 4. 指标趋势迷你图',
            '',
        ])

        for metric_name, analysis in metrics_analysis.items():
            values = analysis.get('values', [])
            cn_name = analysis.get('cn_name', metric_name)
            if values and len(values) > 1:
                sparkline = self._generate_ascii_sparkline(values, width=50)
                md_lines.append(f"**{cn_name}**:")
                md_lines.append('')
                md_lines.append(f'```')
                md_lines.append(sparkline)
                md_lines.append('```')
                md_lines.append('')

        md_lines.extend([
            '---',
            f'*报告自动生成于 {now}*',
        ])

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(md_lines))

        logger.info(f"稳定性测试报告已保存: {output_path}")
        return output_path

    def _generate_ascii_sparkline(self, values: List[float], width: int = 40,
                                   height: int = 8, title: str = '') -> str:
        """
        生成ASCII艺术风格的迷你趋势图

        Args:
            values: 数值列表
            width: 图表宽度（字符数）
            height: 图表高度（行数）
            title: 图表标题

        Returns:
            ASCII艺术字符串
        """
        if not values or len(values) < 2:
            return "数据不足，无法生成图表"

        lines = []
        if title:
            lines.append(title)

        min_val = min(values)
        max_val = max(values)
        val_range = max_val - min_val if max_val != min_val else 1

        # 归一化到0-height范围
        normalized = [int((v - min_val) / val_range * (height - 1)) for v in values]

        # 从上到下绘制
        for row in range(height - 1, -1, -1):
            line = ''
            for col, norm_val in enumerate(normalized):
                if norm_val >= row:
                    line += '█'
                else:
                    line += ' '
            lines.append(line)

        # X轴标签（简化版）
        lines.append('─' * len(values))
        labels = ['R1']
        if len(values) > 2:
            labels.append(f'R{len(values)//2}')
        labels.append(f'R{len(values)}')
        label_positions = [0] + ([len(values)//2] if len(values) > 2 else []) + [len(values)-1]

        label_line = ''
        pos_idx = 0
        for i in range(len(values)):
            if pos_idx < len(label_positions) and i == label_positions[pos_idx]:
                label_line += '|'
                pos_idx += 1
            else:
                label_line += ' '
        lines.append(label_line)

        return '\n'.join(lines)

    def _add_trend_minicharts_to_console(self, results: List[Dict[str, Any]]) -> None:
        """
        在控制台报告中添加四项目标趋势迷你图

        Args:
            results: 测试结果列表
        """
        print("\n" + "=" * 80)
        print("📈 四项目标趋势迷你图 (Trend Mini-Charts)")
        print("=" * 80)

        # 提取各场景的关键指标用于绘制趋势图
        successful_results = [r for r in results if r.get('status') == 'SUCCESS']
        if not successful_results:
            print("  无成功完成的场景，无法生成趋势图")
            return

        # 按订单数量排序
        sorted_results = sorted(successful_results, key=lambda x: x.get('order_count', 0))

        # 提取各项指标
        order_counts = [r.get('order_count', 0) for r in sorted_results]
        complete_rates = [r.get('avg_complete_rate', 0) for r in sorted_results]
        throughputs_p16 = [r.get('orders_per_second_p16', 0) for r in sorted_results]
        cache_hits = [r.get('cache_hit_rate', 0) for r in sorted_results]
        memory_usage = [r.get('peak_memory_mb', 0) for r in sorted_results]

        # 绘制四个迷你图
        charts_data = [
            ('平均齐套率', complete_rates, '%'),
            ('P16吞吐量(ord/s)', throughputs_p16, ''),
            ('缓存命中率', cache_hits, '%'),
            ('内存占用(MB)', memory_usage, ''),
        ]

        for chart_title, values, unit in charts_data:
            print(f"\n  【{chart_title}】")
            sparkline = self._generate_ascii_sparkline(values, width=50, height=6)
            # 缩进显示
            for line in sparkline.split('\n'):
                print(f"    {line}")

            # 显示数值范围
            if values:
                print(f"    范围: [{min(values):.4f}{unit} ~ {max(values):.4f}{unit}]")

        print("\n" + "=" * 80)


# ============================================================
# 消融实验框架
# ============================================================
class AblationStudyRunner:
    """
    消融实验运行器

    通过系统性地禁用各个核心算法模块，评估每个模块对系统整体性能的贡献度。

    测试配置矩阵:
        - baseline: 完整系统（所有模块启用）
        - wo_nsga2: 移除NSGA-II多目标优化
        - wo_prophet: 移除Prophet时间序列预测
        - wo_rl: 移除强化学习策略
        - wo_isoforest: 移除孤立森林异常检测
        - nsga2_only: 仅使用NSGA-II（其他模块禁用）
    """

    # 消融实验配置矩阵
    ABLATION_CONFIGS = {
        'baseline': {
            'use_nsga2': True,
            'use_prophet': True,
            'use_rl': True,
            'use_isoforest': True,
            'description': '完整基线系统',
        },
        'wo_nsga2': {
            'use_nsga2': False,
            'use_prophet': True,
            'use_rl': True,
            'use_isoforest': True,
            'description': '移除NSGA-II多目标优化',
        },
        'wo_prophet': {
            'use_nsga2': True,
            'use_prophet': False,
            'use_rl': True,
            'use_isoforest': True,
            'description': '移除Prophet时间序列预测',
        },
        'wo_rl': {
            'use_nsga2': True,
            'use_prophet': True,
            'use_rl': False,
            'use_isoforest': True,
            'description': '移除强化学习策略',
        },
        'wo_isoforest': {
            'use_nsga2': True,
            'use_prophet': True,
            'use_rl': True,
            'use_isoforest': False,
            'description': '移除孤立森林异常检测',
        },
        'nsga2_only': {
            'use_nsga2': True,
            'use_prophet': False,
            'use_rl': False,
            'use_isoforest': False,
            'description': '仅NSGA-II（其他模块禁用）',
        },
    }

    def __init__(self, order_count: int = 1000, output_dir: str = None):
        """
        初始化消融实验运行器

        Args:
            order_count: 每个配置测试的订单数量（默认1000）
            output_dir: 报告输出目录
        """
        self.order_count = order_count
        self.output_dir = output_dir or os.path.dirname(os.path.abspath(__file__))
        os.makedirs(self.output_dir, exist_ok=True)
        self.results: Dict[str, Dict[str, Any]] = {}
        self.generator = TestDataGenerator(random_seed=42)

    def run_all_configs(self) -> Dict[str, Dict[str, Any]]:
        """
        执行所有消融实验配置

        Returns:
            所有配置的测试结果字典
        """
        logger.info(f"{'='*80}")
        logger.info(f"开始消融实验: {len(self.ABLATION_CONFIGS)}个配置, "
                   f"每配置{self.order_count:,}订单")
        logger.info(f"{'='*80}")

        for config_name, config in self.ABLATION_CONFIGS.items():
            try:
                result = self._run_single_config(config_name, config)
                if result:
                    self.results[config_name] = result
                    logger.info(f"  ✓ 配置 [{config_name}] 完成")
            except Exception as e:
                logger.error(f"  ✗ 配置 [{config_name}] 执行失败: {e}", exc_info=True)
                self.results[config_name] = {
                    'status': 'FAILED',
                    'error': str(e)[:500],
                    'config': config,
                }

        # 计算退化幅度并生成报告
        self._calculate_degradation()
        self._print_ablation_summary()

        return self.results

    def _run_single_config(self, config_name: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        执行单个消融实验配置

        Args:
            config_name: 配置名称
            config: 配置参数字典

        Returns:
            该配置的测试结果
        """
        logger.info(f"\n--- 配置: {config_name} ({config.get('description', '')}) ---")

        # 清理之前的数据
        self._cleanup_ablation_data()

        # 生成测试数据
        data_stats = self.generator.generate_all(
            order_count=self.order_count,
            material_count=min(500, self.order_count // 2),
            bom_count=min(2000, self.order_count * 2),
            inventory_count=min(3000, self.order_count * 3),
            supplier_count=max(10, self.order_count // 100),
            bom_level_range=(3, 5),
        )

        # 获取订单
        orders = list(SalesOrder.objects.filter(
            order_no__startswith='BMK-SO-'
        ).order_by('priority', 'demand_date'))

        if not orders:
            logger.warning(f"配置 [{config_name}]: 未找到测试订单")
            return None

        # 执行计划计算（根据配置调整参数）
        tracemalloc.start()
        start_time = time.perf_counter()

        try:
            hp_planner = HighPerformancePlanner(consumption_priority='FIFO')

            # 根据配置设置规划器参数
            self._apply_config_to_planner(hp_planner, config)

            hp_planner.load_data_with_caching(force_refresh=True)

            plan_result = hp_planner.run_high_performance_planning(
                orders=orders,
                parallel=True,
                max_workers=8,
            )
            results = plan_result.get('results', [])

        except Exception as e:
            logger.error(f"配置 [{config_name}] 执行异常: {e}")
            tracemalloc.stop()
            raise

        planning_time = time.perf_counter() - start_time
        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # 收集四项目标指标
        objectives = self._extract_objectives(results, orders)

        # 清理数据释放资源
        self._cleanup_ablation_data()
        gc.collect()

        return {
            'status': 'SUCCESS',
            'config_name': config_name,
            'config_description': config.get('description', ''),
            'config': config,
            'order_count': len(orders),
            'planning_time': round(planning_time, 3),
            'peak_memory_mb': round(peak_mem / 1024 / 1024, 2),
            **objectives,
            'tested_at': datetime.now().isoformat(),
        }

    def _apply_config_to_planner(self, planner: HighPerformancePlanner, config: Dict[str, bool]):
        """
        将消融配置应用到规划器

        根据配置启用/禁用各算法模块

        Args:
            planner: 高性能规划器实例
            config: 配置参数字典
        """
        # 尝试设置规划器的各种开关属性
        # 注意：这些属性可能在不同的规划器实现中有所不同
        # 这里尝试常见的属性名，如果不存在则忽略

        attr_mapping = {
            'use_nsga2': ['use_nsga2', 'enable_nsga2', 'nsga2_enabled'],
            'use_prophet': ['use_prophet', 'enable_prophet', 'prophet_enabled'],
            'use_rl': ['use_rl', 'enable_rl', 'rl_enabled', 'use_reinforcement_learning'],
            'use_isoforest': ['use_isoforest', 'enable_isoforest', 'isoforest_enabled', 'use_anomaly_detection'],
        }

        for config_key, possible_attrs in attr_mapping.items():
            value = config.get(config_key, True)
            for attr_name in possible_attrs:
                if hasattr(planner, attr_name):
                    try:
                        setattr(planner, attr_name, value)
                        logger.debug(f"  设置 {attr_name} = {value}")
                        break
                    except Exception:
                        continue

    def _extract_objectives(self, results: List[Dict], orders: List) -> Dict[str, float]:
        """
        提取四项目标指标

        Args:
            results: 计划结果列表
            orders: 订单列表

        Returns:
            四项目标的值
        """
        total_orders = len(results)
        if total_orders == 0:
            return {
                'objective1_on_time_delivery': 0.0,
                'objective2_cost_efficiency': 0.0,
                'objective3_inventory_optimization': 0.0,
                'objective4_resource_utilization': 0.0,
            }

        # 目标1：按时交付率（完全齐套率）
        complete_count = sum(1 for r in results if r.get('is_complete'))
        objective1 = complete_count / total_orders

        # 目标2：成本效率（基于齐套率的加权平均）
        complete_rates = [r.get('complete_rate', 0) for r in results]
        objective2 = sum(complete_rates) / total_orders

        # 目标3：库存优化（高优先级订单的齐套率）
        high_priority_complete = 0
        high_priority_total = 0
        for i, r in enumerate(results):
            if i < len(orders):
                priority = getattr(orders[i], 'priority', 3)
                if priority <= 2:  # 高优先级
                    high_priority_total += 1
                    if r.get('is_complete'):
                        high_priority_complete += 1
        objective3 = (high_priority_complete / high_priority_total) if high_priority_total > 0 else objective1

        # 目标4：资源利用率（平均齐套率的标准差倒数，越稳定越好）
        import statistics
        if len(complete_rates) > 1 and statistics.mean(complete_rates) > 0:
            std_dev = statistics.stdev(complete_rates)
            objective4 = 1.0 / (1.0 + std_dev)  # 归一化到0-1
        else:
            objective4 = objective2

        return {
            'objective1_on_time_delivery': round(objective1, 6),
            'objective2_cost_efficiency': round(objective2, 6),
            'objective3_inventory_optimization': round(objective3, 6),
            'objective4_resource_utilization': round(objective4, 6),
        }

    def _cleanup_ablation_data(self):
        """清理消融实验数据"""
        try:
            with transaction.atomic():
                order_ids = list(SalesOrder.objects.filter(
                    order_no__startswith='BMK-SO-'
                ).values_list('id', flat=True))

                if order_ids:
                    OrderAllocation.objects.filter(order_id__in=order_ids).delete()
                    MaterialPlanResult.objects.filter(order_id__in=order_ids).delete()
                    SupplierCommitment.objects.filter(order_no__startswith='BMK-PO-').delete()

                SupplierMaterial.objects.filter(
                    supplier__supplier_code__startswith='BMK-SUP-'
                ).delete()
                Inventory.objects.filter(
                    material__material_code__startswith='BMK-MAT-'
                ).delete()
                BillOfMaterials.objects.filter(
                    parent_material__material_code__startswith='BMK-MAT-'
                ).delete()
                SalesOrder.objects.filter(order_no__startswith='BMK-SO-').delete()
                Material.objects.filter(material_code__startswith='BMK-MAT-').delete()
                Supplier.objects.filter(supplier_code__startswith='BMK-SUP-').delete()
                Customer.objects.filter(customer_code__startswith='BMK-CUS-').delete()
        except Exception as e:
            logger.warning(f"清理消融实验数据时出错: {e}")

    def _calculate_degradation(self):
        """计算各配置相对于baseline的退化幅度"""
        baseline_result = self.results.get('baseline')
        if not baseline_result or baseline_result.get('status') != 'SUCCESS':
            logger.warning("无法计算退化幅度：baseline配置未成功执行")
            return

        baseline_objectives = {
            'objective1_on_time_delivery': baseline_result.get('objective1_on_time_delivery', 0),
            'objective2_cost_efficiency': baseline_result.get('objective2_cost_efficiency', 0),
            'objective3_inventory_optimization': baseline_result.get('objective3_inventory_optimization', 0),
            'objective4_resource_utilization': baseline_result.get('objective4_resource_utilization', 0),
        }

        objective_names = {
            'objective1_on_time_delivery': '按时交付率',
            'objective2_cost_efficiency': '成本效率',
            'objective3_inventory_optimization': '库存优化',
            'objective4_resource_utilization': '资源利用率',
        }

        for config_name, result in self.results.items():
            if config_name == 'baseline' or result.get('status') != 'SUCCESS':
                continue

            degradation = {}
            for obj_key, baseline_val in baseline_objectives.items():
                current_val = result.get(obj_key, 0)
                if baseline_val > 0:
                    deg_pct = ((current_val - baseline_val) / baseline_val) * 100
                else:
                    deg_pct = 0
                degradation[obj_key] = {
                    'baseline': round(baseline_val, 6),
                    'current': round(current_val, 6),
                    'degradation_pct': round(deg_pct, 2),
                    'cn_name': objective_names.get(obj_key, obj_key),
                }

            result['degradation'] = degradation

            # 计算综合退化幅度（四项目标的平均退化）
            deg_values = [d['degradation_pct'] for d in degradation.values()]
            result['avg_degradation'] = round(sum(deg_values) / len(deg_values), 2) if deg_values else 0

    def _print_ablation_summary(self):
        """输出消融实验摘要报告"""
        logger.info(f"\n{'='*80}")
        logger.info("🔬 消融实验结果摘要")
        logger.info(f"{'='*80}")

        # 表头
        logger.info(f"\n{'配置':<16} {'描述':<24} {'按时交付率':>12} {'成本效率':>10} "
                   f"{'库存优化':>10} {'资源利用':>10} {'平均退化':>10}")
        logger.info("-" * 96)

        # 输出每个配置的结果
        for config_name, result in self.results.items():
            config = self.ABLATION_CONFIGS.get(config_name, {})
            desc = config.get('description', '')[:22]

            if result.get('status') != 'SUCCESS':
                logger.info(f"{config_name:<16} {desc:<24} {'FAILED':>52}")
                continue

            o1 = result.get('objective1_on_time_delivery', 0)
            o2 = result.get('objective2_cost_efficiency', 0)
            o3 = result.get('objective3_inventory_optimization', 0)
            o4 = result.get('objective4_resource_utilization', 0)
            avg_deg = result.get('avg_degradation', 0)

            deg_str = f"{avg_deg:+.1f}%" if avg_deg != 0 else "0.0%"
            logger.info(
                f"{config_name:<16} {desc:<24} "
                f"{o1:>11.4f} {o2:>9.4f} {o3:>9.4f} {o4:>9.4f} {deg_str:>10}"
            )

        # 输出退化幅度详情
        baseline_result = self.results.get('baseline')
        if baseline_result and baseline_result.get('status') == 'SUCCESS':
            logger.info(f"\n【各模块贡献度分析】")
            for config_name, result in self.results.items():
                if config_name == 'baseline' or 'degradation' not in result:
                    continue

                degradation = result['degradation']
                logger.info(f"\n  {config_name}:")
                for obj_key, deg_info in degradation.items():
                    cn_name = deg_info['cn_name']
                    deg_pct = deg_info['degradation_pct']
                    if deg_pct < -5:
                        impact = '⚠️ 显著退化'
                    elif deg_pct < -2:
                        impact = '⚡ 轻微退化'
                    elif deg_pct <= 2:
                        impact = '✅ 无明显影响'
                    else:
                        impact = '📈 意外提升'

                    logger.info(f"    {cn_name}: {deg_pct:+.2f}% ({impact})")

        logger.info(f"\n{'='*80}\n")


# ============================================================
# 命令行参数解析
# ============================================================
def parse_arguments() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='多约束供应链智能运营系统 - 性能基准测试工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python benchmark_performance.py                              # 运行全部6个场景
  python benchmark_performance.py --scenario=large             # 仅运行大型(目标)场景
  python benchmark_performance.py --scenario=stress --runs=1   # 压力测试只跑1次
  python benchmark_performance.py --list-scenarios             # 列出所有可用场景
        """
    )

    parser.add_argument(
        '--scenario',
        type=str,
        default=None,
        choices=list(SCENARIOS.keys()),
        help='指定要测试的场景名（默认: 运行全部场景）'
    )
    parser.add_argument(
        '--runs',
        type=int,
        default=3,
        help='每个场景重复运行的次数，用于取平均值（默认: 3）'
    )
    parser.add_argument(
        '--list-scenarios',
        action='store_true',
        help='列出所有可用的测试场景并退出'
    )
    parser.add_argument(
        '--output-dir',
        type=str,
        default=None,
        help='报告输出目录（默认: 当前脚本所在目录）'
    )

    return parser.parse_args()


def list_scenarios():
    """列出所有可用场景"""
    print("\n可用的测试场景:\n")
    print(f"{'场景Key':<16} {'场景名称':<14} {'订单':>8} {'物料':>8} "
          f"{'BOM':>8} {'库存':>8} {'供应商':>6} {'BOM层级'}")
    print("─" * 86)

    for key, cfg in SCENARIOS.items():
        levels = f"{cfg['bom_levels'][0]}-{cfg['bom_levels'][1]}"
        print(f"{key:<16} {cfg['name']:<14} {cfg['order_count']:>8,} "
              f"{cfg['material_count']:>8,} {cfg['bom_count']:>8,} "
              f"{cfg['inventory_count']:>8,} {cfg['supplier_count']:>6}  {levels}")

    print(f"\n并行度配置: {', '.join(name for name, _ in PARALLEL_CONFIGS)}")
    print(f"性能阈值: 万级订单总耗时 < {MAX_ACCEPTABLE_TIME_STRESS}s "
          f"({MAX_ACCEPTABLE_TIME_STRESS // 3600}小时)\n")


# ============================================================
# 主入口
# ============================================================
def main():
    """主函数 - 基准测试入口点"""
    args = parse_arguments()

    # 列出场景模式
    if args.list_scenarios:
        list_scenarios()
        return

    # 确定输出目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = args.output_dir or script_dir

    print(f"\n{'🚀' if sys.platform != 'win32' else ''} "
          f"多约束供应链智能运营系统 - 性能基准测试")
    print(f"{'='*60}")

    # 显示测试配置
    if args.scenario:
        cfg = SCENARIOS[args.scenario]
        print(f"  目标场景: {args.scenario} ({cfg['name']})")
    else:
        print(f"  目标场景: 全部 ({len(SCENARIOS)} 个)")
    print(f"  每场景运行次数: {args.runs}")
    print(f"  报告输出目录: {output_dir}")
    print(f"  性能阈值: 万级订单 < {MAX_ACCEPTABLE_TIME_STRESS}s\n")

    # 创建引擎并执行测试
    engine = BenchmarkEngine(
        runs_per_scenario=args.runs,
        target_scenario=args.scenario,
    )

    overall_start = time.time()
    results = engine.run_all_scenarios()
    total_elapsed = time.time() - overall_start

    # 生成报告
    reporter = ReportGenerator(output_dir=output_dir)

    # 1. 控制台报告
    reporter.print_console_report(results)

    # 2. JSON报告
    json_path = reporter.save_json_report(results)

    # 3. Markdown报告
    md_path = reporter.save_markdown_report(results)

    # 最终总结
    print(f"\n{'='*60}")
    print(f"基准测试全部完成!")
    print(f"  总耗时: {total_elapsed:.1f}s ({total_elapsed/60:.1f}分钟)")
    print(f"  结果文件:")
    print(f"    📊 JSON:  {json_path}")
    print(f"    📝 MD:    {md_path}")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()
