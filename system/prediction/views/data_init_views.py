from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from datetime import date, timedelta, datetime
import os
import csv
import logging
from ..models import (
    Material, Supplier, Customer, WorkCenter, FactoryCalendar,
    BillOfMaterials, Inventory, SalesOrder, PurchaseOrder, SupplierCommitment
)

logger = logging.getLogger(__name__)

# 数据文件根目录（指向数据集目录）
from django.conf import settings as _settings
DATA_ROOT = getattr(_settings, 'CSV_IMPORT_BASE_PATH', None) or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    '数据集'
)

@login_required
def data_init(request):
    """数据初始化视图"""
    if request.method == 'POST':
        init_type = request.POST.get('init_type')

        try:
            if init_type == 'calendar':
                init_factory_calendar()
                messages.success(request, '工厂日历初始化完成！')
            elif init_type == 'workcenter':
                init_work_center()
                messages.success(request, '工作中心初始化完成！')
            elif init_type == 'demo_data':
                init_demo_data()
                messages.success(request, '演示数据初始化完成！')
            elif init_type == 'all':
                init_factory_calendar()
                init_work_center()
                init_demo_data()
                messages.success(request, '所有数据初始化完成！')
            elif init_type == 'all_csv':
                import_all_from_csv()
                messages.success(request, 'CSV数据集全量导入完成！')
        except Exception as e:
            logger.error(f'数据初始化失败 [{init_type}]: {e}', exc_info=True)
            messages.error(request, f'初始化失败: {str(e)}')

        return redirect('data_init')

    return render(request, 'data_init.html', {
        'stats': get_system_stats()
    })

def get_system_stats():
    """获取系统数据统计"""
    return {
        'materials': Material.objects.count(),
        'suppliers': Supplier.objects.count(),
        'customers': Customer.objects.count(),
        'workcenters': WorkCenter.objects.count(),
        'calendars': FactoryCalendar.objects.count(),
        'inventories': Inventory.objects.count(),
        'sales_orders': SalesOrder.objects.count(),
        'purchase_orders': PurchaseOrder.objects.count(),
        'boms': BillOfMaterials.objects.count()
    }

def init_factory_calendar():
    """初始化工厂日历（生成未来一年的工作日）"""
    today = date.today()
    end_date = today + timedelta(days=365)
    
    current_date = today
    while current_date <= end_date:
        # 判断是否为周末
        is_workday = current_date.weekday() < 5  # Monday=0, Sunday=6
        
        # 检查是否已存在（使用DEFAULT工厂代码）
        if not FactoryCalendar.objects.filter(factory_code='DEFAULT', date=current_date).exists():
            FactoryCalendar.objects.create(
                factory_code='DEFAULT',
                date=current_date,
                is_workday=is_workday,
                shift_count=2 if is_workday else 0,
                remarks=''
            )
        
        current_date += timedelta(days=1)
    
    logger.info(f'工厂日历初始化完成，共创建 {365} 天')

def init_work_center():
    """初始化工作中心数据 - 使用真实产能数据"""
    # 先获取所有成品物料代码
    finished_codes = list(Material.objects.filter(material_type='finished').values_list('material_code', flat=True))
    # 如果没有成品则用默认列表
    if not finished_codes:
        finished_codes = ['P0001', 'P0002', 'P0045', 'P0047', 'P0048']
    all_products = ','.join(finished_codes)

    work_centers = [
        {
            'work_center_code': 'WC001',
            'work_center_name': 'A生产线',
            'available_products': all_products,
            'daily_available_hours': 16,
            'shift_count': 2,
            'hours_per_shift': 8,
            'production_days_per_week': 6,
            'planned_headcount': 20,
            'actual_headcount': 18,
            'daily_capacity_limit': 800,   # 真实日产能：约800台/天
            'changeover_time': 2,
            'planned_maintenance_hours': 4
        },
        {
            'work_center_code': 'WC002',
            'work_center_name': 'B生产线',
            'available_products': all_products,
            'daily_available_hours': 16,
            'shift_count': 2,
            'hours_per_shift': 8,
            'production_days_per_week': 6,
            'planned_headcount': 25,
            'actual_headcount': 23,
            'daily_capacity_limit': 600,   # 真实日产能：约600台/天
            'changeover_time': 1.5,
            'planned_maintenance_hours': 4
        },
        {
            'work_center_code': 'WC003',
            'work_center_name': 'C生产线',
            'available_products': all_products,
            'daily_available_hours': 24,
            'shift_count': 3,
            'hours_per_shift': 8,
            'production_days_per_week': 7,
            'planned_headcount': 30,
            'actual_headcount': 28,
            'daily_capacity_limit': 1000,  # 真实日产能：约1000台/天（三班倒）
            'changeover_time': 2,
            'planned_maintenance_hours': 3
        },
        {
            'work_center_code': 'WC004',
            'work_center_name': '包装车间',
            'available_products': all_products,
            'daily_available_hours': 16,
            'shift_count': 2,
            'hours_per_shift': 8,
            'production_days_per_week': 6,
            'planned_headcount': 15,
            'actual_headcount': 14,
            'daily_capacity_limit': 1200,  # 包装线产能较高
            'changeover_time': 0.5,
            'planned_maintenance_hours': 2
        }
    ]

    for wc_data in work_centers:
        WorkCenter.objects.update_or_create(
            work_center_code=wc_data['work_center_code'],
            defaults=wc_data
        )

    logger.info(f'工作中心初始化完成，共 {len(work_centers)} 个，支持产品: {all_products[:50]}...')

def init_demo_data():
    """初始化演示数据 - 优先使用已导入的真实数据（CSV/xlsx），不再生成假数据"""
    from django.core.cache import cache
    from ..utils.safe_cache import safe_delete

    with transaction.atomic():
        # 仅初始化基础结构数据（物料/供应商/客户/BOM），且只在数据库为空时执行
        # 已通过 CSV/xlsx 导入的数据不会被覆盖
        if Material.objects.count() == 0:
            init_materials()
        else:
            logger.info(f'数据库中已有 {Material.objects.count()} 条物料记录，跳过初始化')

        if Supplier.objects.count() == 0:
            init_suppliers()
        else:
            logger.info(f'数据库中已有 {Supplier.objects.count()} 条供应商记录，跳过初始化')

        if Customer.objects.count() == 0:
            init_customers()
        else:
            logger.info(f'数据库中已有 {Customer.objects.count()} 条客户记录，跳过初始化')

        # BOM：只为没有BOM的成品创建
        init_boms()

        # 库存：只为没有库存记录的物料创建
        if Inventory.objects.count() == 0:
            init_inventories()
        else:
            logger.info(f'数据库中已有 {Inventory.objects.count()} 条库存记录，跳过初始化')

        # 销售订单和采购订单：只在数据库为空时从CSV/xlsx导入
        if SalesOrder.objects.count() == 0:
            import_sales_orders_from_csv()
        else:
            logger.info(f'数据库中已有 {SalesOrder.objects.count()} 条销售订单，跳过初始化')

        if PurchaseOrder.objects.count() == 0:
            import_purchase_orders_from_xlsx()
        else:
            logger.info(f'数据库中已有 {PurchaseOrder.objects.count()} 条采购订单，跳过初始化')

        # 清除所有计划相关缓存（含各策略子缓存），确保下次API调用基于新数据重新计算
        from ..tasks import _clear_all_planning_caches
        _clear_all_planning_caches()

        logger.info('演示数据初始化完成（基于已导入的真实数据）')

def init_materials():
    """初始化物料数据 - 与CSV文件格式保持一致"""
    materials = [
        {'material_code': 'P0001', 'material_name': '智能音箱 A', 'material_type': 'finished', 'unit': '台', 'standard_cost': 288.59, 'sales_price': 975.96, 'safety_stock': 100, 'shelf_life': 730, 'lead_time': 6, 'min_production_qty': 40},
        {'material_code': 'P0002', 'material_name': '智能音箱 B', 'material_type': 'finished', 'unit': '台', 'standard_cost': 224.96, 'sales_price': 432.29, 'safety_stock': 80, 'shelf_life': 3650, 'lead_time': 7, 'min_production_qty': 100},
        {'material_code': 'P0045', 'material_name': '无线耳机 Pro', 'material_type': 'finished', 'unit': '副', 'standard_cost': 156.78, 'sales_price': 599.00, 'safety_stock': 200, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 50},
        {'material_code': 'P0047', 'material_name': '智能手表 Ultra', 'material_type': 'finished', 'unit': '块', 'standard_cost': 428.99, 'sales_price': 1299.00, 'safety_stock': 50, 'shelf_life': 730, 'lead_time': 8, 'min_production_qty': 30},
        {'material_code': 'P0048', 'material_name': '平板电脑 Air', 'material_type': 'finished', 'unit': '台', 'standard_cost': 899.00, 'sales_price': 2499.00, 'safety_stock': 60, 'shelf_life': 1825, 'lead_time': 10, 'min_production_qty': 20},
        {'material_code': 'M0001', 'material_name': 'ABS塑料外壳', 'material_type': 'raw', 'unit': '个', 'standard_cost': 111.50, 'sales_price': 150.00, 'safety_stock': 500, 'shelf_life': 3650, 'lead_time': 12, 'min_order_qty': 100},
        {'material_code': 'M0002', 'material_name': 'PC透明罩', 'material_type': 'raw', 'unit': '个', 'standard_cost': 58.34, 'sales_price': 80.00, 'safety_stock': 400, 'shelf_life': 3650, 'lead_time': 5, 'min_order_qty': 2000},
        {'material_code': 'M0007', 'material_name': 'ARM Cortex-A53芯片', 'material_type': 'raw', 'unit': '片', 'standard_cost': 64.73, 'sales_price': 95.00, 'safety_stock': 200, 'shelf_life': 1095, 'lead_time': 21, 'min_order_qty': 200},
        {'material_code': 'M0021', 'material_name': '电解电容 100μF', 'material_type': 'raw', 'unit': '个', 'standard_cost': 0.50, 'sales_price': 0.80, 'safety_stock': 2000, 'shelf_life': 1825, 'lead_time': 7, 'min_order_qty': 1000},
        {'material_code': 'M0051', 'material_name': '继电器 12V', 'material_type': 'raw', 'unit': '个', 'standard_cost': 5.20, 'sales_price': 8.50, 'safety_stock': 300, 'shelf_life': 3650, 'lead_time': 10, 'min_order_qty': 100},
        {'material_code': 'M0142', 'material_name': 'LoRa模块 v3', 'material_type': 'raw', 'unit': '个', 'standard_cost': 45.80, 'sales_price': 68.00, 'safety_stock': 100, 'shelf_life': 730, 'lead_time': 15, 'min_order_qty': 50},
        {'material_code': 'M0163', 'material_name': '包装彩盒(小) v3', 'material_type': 'raw', 'unit': '个', 'standard_cost': 2.80, 'sales_price': 4.50, 'safety_stock': 1000, 'shelf_life': 365, 'lead_time': 3, 'min_order_qty': 500},
        {'material_code': 'SM001', 'material_name': '主板组件', 'material_type': 'semi', 'unit': '件', 'standard_cost': 125.00, 'sales_price': 168.00, 'safety_stock': 150, 'shelf_life': 365, 'lead_time': 4, 'min_production_qty': 50},
        {'material_code': 'SM002', 'material_name': '电源模块', 'material_type': 'semi', 'unit': '件', 'standard_cost': 45.00, 'sales_price': 65.00, 'safety_stock': 200, 'shelf_life': 730, 'lead_time': 3, 'min_production_qty': 100},
    ]
    
    for mat_data in materials:
        Material.objects.get_or_create(
            material_code=mat_data['material_code'],
            defaults=mat_data
        )

def init_suppliers():
    """初始化供应商数据 - 与CSV文件格式保持一致"""
    suppliers = [
        {'supplier_code': 'SUP001', 'supplier_name': '深圳华腾电子', 'contact_person': '张伟', 'phone': '13800138001', 'email': 'zhangwei@huateng.com', 'address': '深圳市南山区科技园', 'rating': 'A', 'delivery_reliability': 0.98, 'normal_lead_time': 7},
        {'supplier_code': 'SUP002', 'supplier_name': '东莞鑫盛塑胶', 'contact_person': '李明', 'phone': '13900139002', 'email': 'liming@xinsheng.com', 'address': '东莞市长安镇', 'rating': 'B', 'delivery_reliability': 0.95, 'normal_lead_time': 10},
        {'supplier_code': 'SUP003', 'supplier_name': '上海精密机械', 'contact_person': '王芳', 'phone': '13700137003', 'email': 'wangfang@shprecision.com', 'address': '上海市浦东新区', 'rating': 'A', 'delivery_reliability': 0.99, 'normal_lead_time': 5},
        {'supplier_code': 'SUP004', 'supplier_name': '广州恒达金属', 'contact_person': '陈强', 'phone': '13600136004', 'email': 'chenqiang@hengda.com', 'address': '广州市天河区', 'rating': 'C', 'delivery_reliability': 0.92, 'normal_lead_time': 15},
        {'supplier_code': 'SUP005', 'supplier_name': '苏州佳美电子', 'contact_person': '刘洋', 'phone': '13500135005', 'email': 'liuyang@jiamei.com', 'address': '苏州市工业园区', 'rating': 'A', 'delivery_reliability': 0.97, 'normal_lead_time': 8},
        {'supplier_code': 'SUP006', 'supplier_name': '杭州明远科技', 'contact_person': '赵敏', 'phone': '13400134006', 'email': 'zhaomin@mingyuan.com', 'address': '杭州市滨江区', 'rating': 'B', 'delivery_reliability': 0.94, 'normal_lead_time': 12},
        {'supplier_code': 'SUP007', 'supplier_name': '南京盛达光电', 'contact_person': '孙磊', 'phone': '13300133007', 'email': 'sunlei@shengda.com', 'address': '南京市江宁区', 'rating': 'A', 'delivery_reliability': 0.98, 'normal_lead_time': 6},
        {'supplier_code': 'SUP008', 'supplier_name': '武汉华工科技', 'contact_person': '周杰', 'phone': '13200132008', 'email': 'zhoujie@huagong.com', 'address': '武汉市东湖高新区', 'rating': 'B', 'delivery_reliability': 0.95, 'normal_lead_time': 10},
    ]
    
    for sup_data in suppliers:
        Supplier.objects.get_or_create(
            supplier_code=sup_data['supplier_code'],
            defaults=sup_data
        )

def init_customers():
    """初始化客户数据"""
    # 根据客户名称推断客户类型、付款条件、等级
    def infer_customer_info(name):
        """从客户名称推断客户属性"""
        name_lower = name.lower()
        # 推断客户类型
        if '海外' in name or 'eBay' in name or 'amazon' in name_lower or 'lazada' in name_lower or 'shopee' in name_lower:
            customer_type = '海外渠道'
        elif '工程' in name:
            customer_type = '工程渠道'
        elif '企业集采' in name or 'corp' in name_lower:
            customer_type = '企业集采'
        elif '电商' in name or '抖音' in name or '京东' in name or '天猫' in name or '拼多多' in name or '唯品会' in name or '苏宁' in name or '快手' in name:
            customer_type = '电商平台'
        elif '线下零售' in name or '沃尔玛' in name or '家乐福' in name or '大润发' in name or '物美' in name or '华润' in name or '联华' in name or '永辉' in name or '国美' in name:
            customer_type = '线下零售'
        elif '运营商' in name or '电信' in name or '移动' in name or '联通' in name or '广电' in name:
            customer_type = '运营商'
        else:
            customer_type = '其他'
        # 推断客户等级（S级客户通常是重要合作伙伴）
        if any(k in name for k in ['中国移动', '中国电信', '华为', '小米', '比亚迪', '格力', '联想', 'OPPO', 'vivo']):
            customer_level = 'vip'
        elif any(k in name for k in ['中兴', '万科', '碧桂园', '龙湖', '绿地']):
            customer_level = 'important'
        else:
            customer_level = 'normal'
        # 推断付款条件
        if '运营商' in name:
            payment_terms = '月结90天'
        elif '企业集采' in name or '工程' in name:
            payment_terms = '月结45天'
        elif '线下零售' in name:
            payment_terms = '月结30天'
        else:
            payment_terms = '月结30天'
        return customer_type, payment_terms, customer_level

    customers = [
        {'customer_code': 'C001', 'customer_name': '海外-欧洲', 'contact_person': '赵总', 'phone': '13811110001', 'email': 'zhaozong@europe.com', 'address': '欧洲', 'credit_limit': 1000000},
        {'customer_code': 'C002', 'customer_name': '工程渠道-万科', 'contact_person': '钱经理', 'phone': '13911110002', 'email': 'qianjingli@vanke.com', 'address': '深圳', 'credit_limit': 800000},
        {'customer_code': 'C003', 'customer_name': '企业集采-中兴', 'contact_person': '孙总', 'phone': '13711110003', 'email': 'sunzong@zte.com', 'address': '深圳', 'credit_limit': 1500000},
        {'customer_code': 'C004', 'customer_name': '快手电商', 'contact_person': '李总', 'phone': '13611110004', 'email': 'lizong@kuaishou.com', 'address': '北京', 'credit_limit': 2000000},
        {'customer_code': 'C005', 'customer_name': '唯品会', 'contact_person': '周经理', 'phone': '13511110005', 'email': 'zhoujingli@vip.com', 'address': '广州', 'credit_limit': 500000},
        {'customer_code': 'C006', 'customer_name': '运营商-中国联通', 'contact_person': '吴总', 'phone': '13411110006', 'email': 'wuzong@chinaunicom.com', 'address': '北京', 'credit_limit': 3000000},
        {'customer_code': 'C007', 'customer_name': '线下零售-沃尔玛', 'contact_person': '郑经理', 'phone': '13311110007', 'email': 'zhengjingli@walmart.com', 'address': '全国', 'credit_limit': 2500000},
        {'customer_code': 'C008', 'customer_name': 'Lazada东南亚', 'contact_person': '王经理', 'phone': '13211110008', 'email': 'wangjingli@lazada.com', 'address': '东南亚', 'credit_limit': 1200000},
    ]

    for cust_data in customers:
        # 推断缺失的字段
        ctype, pterms, clevel = infer_customer_info(cust_data['customer_name'])
        cust_data['customer_type'] = ctype
        cust_data['payment_terms'] = pterms
        cust_data['customer_level'] = clevel
        cust_data['delivery_priority'] = 5 if clevel == 'normal' else (3 if clevel == 'important' else 1)

        Customer.objects.get_or_create(
            customer_code=cust_data['customer_code'],
            defaults=cust_data
        )

def init_boms():
    """初始化BOM数据 - 为所有成品物料创建完整的BOM清单"""
    # 获取所有原材料和半成品（用于构建BOM）
    raw_materials = {m.material_code: m for m in Material.objects.filter(material_type__in=['raw', 'semi'])}
    
    # 获取所有成品
    finished_products = list(Material.objects.filter(material_type='finished'))
    
    # 常用原材料引用
    M0001 = raw_materials.get('M0001')   # ABS塑料外壳
    M0002 = raw_materials.get('M0002')   # PC透明罩
    M0007 = raw_materials.get('M0007')   # ARM Cortex-A53芯片
    M0021 = raw_materials.get('M0021')   # 电解电容 100μF
    M0051 = raw_materials.get('M0051')   # 继电器 12V
    M0142 = raw_materials.get('M0142')   # LoRa模块 v3
    M0163 = raw_materials.get('M0163')   # 包装彩盒(小) v3
    SM001 = raw_materials.get('SM001')   # 主板组件
    SM002 = raw_materials.get('SM002')   # 电源模块
    
    bom_definitions = {
        # 成品代码: [(子物料, 用量, 单位, 优先级), ...]
        'P0001': [  # 智能音箱 A
            (M0021, 8, '个', 1), (M0007, 1, '片', 2), (M0001, 1, '个', 3),
            (M0051, 10, '个', 4), (M0163, 1, '个', 5), (M0142, 1, '个', 6),
        ],
        'P0002': [  # 智能音箱 B
            (M0021, 5, '个', 1), (M0007, 1, '片', 2), (M0163, 1, '个', 3),
        ],
        'P0045': [  # 无线耳机 Pro
            (M0007, 1, '片', 1), (M0021, 3, '个', 2), (M0001, 1, '个', 3),
            (M0002, 1, '个', 4), (M0163, 1, '个', 5), (M0051, 2, '个', 6),
        ],
        'P0047': [  # 智能手表 Ultra
            (M0007, 1, '片', 1), (M0021, 5, '个', 2), (M0001, 1, '个', 3),
            (M0142, 1, '个', 4), (M0163, 1, '个', 5),
        ],
        'P0048': [  # 平板电脑 Air
            (M0007, 1, '片', 1), (M0021, 15, '个', 2), (M0001, 1, '个', 3),
            (M0002, 1, '个', 4), (SM001, 1, '件', 5), (SM002, 1, '件', 6),
            (M0163, 1, '个', 7),
        ],
    }
    
    created_count = 0
    for product in finished_products:
        code = product.material_code
        children = bom_definitions.get(code, None)
        
        # 预定义BOM
        if children:
            for child_mat, qty, unit, priority in children:
                if child_mat:
                    BillOfMaterials.objects.get_or_create(
                        parent_material=product,
                        child_material=child_mat,
                        defaults={
                            'quantity': qty, 'unit': unit, 'bom_level': 1,
                            'alternative_group': code, 'alternative_priority': priority,
                            'alternative_ratio': 1.0, 'scrap_rate': 0.01
                        }
                    )
                    created_count += 1
        else:
            # 动态生成BOM：为没有预定义的成品自动创建基础BOM
            # 使用可用的原材料按合理比例组合
            auto_children = []
            # 芯片是核心 → 必选
            if M0007:
                auto_children.append((M0007, 1, '片', 1))
            # 电容通用 → 必选
            if M0021:
                auto_children.append((M0021, 5, '个', 2))
            # 外壳 → 必选
            if M0001:
                auto_children.append((M0001, 1, '个', 3))
            # 包装 → 必选
            if M0163:
                auto_children.append((M0163, 1, '个', 4))
            
            for child_mat, qty, unit, priority in auto_children:
                BillOfMaterials.objects.get_or_create(
                    parent_material=product,
                    child_material=child_mat,
                    defaults={
                        'quantity': qty, 'unit': unit, 'bom_level': 1,
                        'alternative_group': code, 'alternative_priority': priority,
                        'alternative_ratio': 1.0, 'scrap_rate': 0.01
                    }
                )
                created_count += 1
    
    logger.info(f'BOM初始化完成，共创建/更新 {created_count} 条BOM记录，覆盖 {len(finished_products)} 个成品')

def init_inventories():
    """从CSV文件导入库存数据（03_库存.csv）- 按物料聚合多行数据"""
    csv_path = os.path.join(DATA_ROOT, '03_库存.csv')
    if not os.path.exists(csv_path):
        # 兼容旧文件名
        csv_path = os.path.join(DATA_ROOT, '03_库存 (1).csv')
    if not os.path.exists(csv_path):
        logger.warning(f'库存文件不存在: {csv_path}，跳过库存初始化')
        return

    # 第一步：按物料ID聚合所有行的数据
    aggregated = {}  # {material_code: {total_qty, total_hold, total_available, is_hold, hold_reason, warehouse}}
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            material_code = row.get('物料ID', '').strip()
            if not material_code:
                continue

            quantity_str = row.get('在库数量', row.get('库存数量', row.get('可用数量', '0'))).strip().replace(',', '')
            quantity = float(quantity_str) if quantity_str else 0

            available_str = row.get('可用数量', '').strip().replace(',', '')
            available_qty = float(available_str) if available_str else quantity

            hold_qty_str = row.get('Hold数量', '').strip().replace(',', '')
            hold_qty = float(hold_qty_str) if hold_qty_str else 0

            hold_reason = row.get('Hold原因', '').strip()
            warehouse = row.get('库位', row.get('仓库代码', '')).strip() or '主仓库'

            if material_code not in aggregated:
                aggregated[material_code] = {
                    'total_qty': 0, 'total_hold': 0, 'total_available': 0,
                    'is_hold': False, 'hold_reason': '', 'warehouse': warehouse
                }
            agg = aggregated[material_code]
            agg['total_qty'] += quantity
            agg['total_hold'] += hold_qty
            agg['total_available'] += available_qty
            # 修复：不再因hold_qty>0就标记整条记录为is_hold=True
            # is_hold仅当全部库存都被冻结时才为True（available_qty <= 0）
            # 正常情况只记录hold_quantity和hold_reason即可

    # 第二步：写入数据库
    imported = 0
    for material_code, agg in aggregated.items():
        try:
            material = Material.objects.filter(material_code=material_code).first()
            if not material:
                continue

            # 按(material, warehouse)查找（与模型unique_together约束对齐）
            wh = agg.get('warehouse', '主仓库') or '主仓库'
            existing = Inventory.objects.filter(material=material, warehouse=wh).first()
            if existing:
                existing.quantity = agg['total_qty']
                existing.available_quantity = agg['total_available']
                existing.hold_quantity = agg['total_hold']
                existing.batch_no = f'B{datetime.now().strftime("%y%m")}001'
                existing.warehouse = wh
                existing.is_hold = agg['is_hold']
                if agg['hold_reason']:
                    existing.hold_reason = agg['hold_reason']
                existing.save()
            else:
                Inventory.objects.create(
                    material=material,
                    quantity=agg['total_qty'],
                    available_quantity=agg['total_available'],
                    hold_quantity=agg['total_hold'],
                    inventory_type='local',
                    warehouse=wh,
                    location='A区',
                    batch_no=f'B{datetime.now().strftime("%y%m")}001',
                    is_hold=agg['is_hold'],
                    hold_reason=agg['hold_reason']
                )
            imported += 1
        except Exception as e:
            logger.warning(f'导入库存 {material_code} 失败: {e}')
            continue

    logger.info(f'库存数据从CSV导入完成，共 {imported} 条物料（聚合自 {sum(len(v) for v in []) if False else len(aggregated)} 条记录）')

def import_sales_orders_from_csv():
    """从CSV文件导入销售订单数据（04_订单.csv）"""
    csv_path = os.path.join(DATA_ROOT, '04_订单.csv')
    if not os.path.exists(csv_path):
        csv_path = os.path.join(DATA_ROOT, '04_订单 (1).csv')
    if not os.path.exists(csv_path):
        logger.warning(f'订单文件不存在: {csv_path}，跳过销售订单导入')
        return

    imported = 0
    # 状态映射：CSV中的中文状态 → 数据库状态值
    status_map = {
        # 中文输入
        '待处理': 'pending', '待确认': 'pending', '待排产': 'pending',
        '已确认': 'confirmed',
        '生产中': 'in_production', '进行中': 'processing',
        '已占料': 'allocated',
        '部分齐套': 'partial', '部分完成': 'partial',
        '完全齐套': 'complete', '已完成': 'complete',
        '已发货': 'shipped', '已交付': 'delivered',
        '已取消': 'cancelled',
        # 英文输入（兼容）
        'pending': 'pending', 'confirmed': 'confirmed',
        'in_production': 'in_production', 'processing': 'processing',
        'allocated': 'allocated', 'partial': 'partial', 'complete': 'complete',
        'shipped': 'shipped', 'delivered': 'delivered', 'cancelled': 'cancelled',
    }
    shipping_map = {'空运': 'air', '海运': 'sea', '陆运': 'land', '快递': 'express',
                    'sea': 'sea', 'air': 'air', 'express': 'express'}
    priority_map = {'紧急': 1, '加急': 2, '高': 3, '普通': 4, '低': 5,
                    'critical': 1, 'urgent': 2, 'high': 3, 'normal': 4, 'low': 5}

    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            order_no = row.get('订单ID', '').strip()
            if not order_no:
                continue
            try:
                product_code = row.get('产品ID', row.get('成品ID', '')).strip()
                material = Material.objects.filter(material_code=product_code).first()

                quantity_str = row.get('订单数量', '0').strip().replace(',', '')
                quantity = int(float(quantity_str)) if quantity_str else 0
                unit_price_str = row.get('单价', '0').strip().replace(',', '')
                unit_price = float(unit_price_str) if unit_price_str else 0
                total_amount = quantity * unit_price

                order_date_str = row.get('订单日期', row.get('下单日期', '')).strip()
                # 【修复】CSV实际列名为'需求交付日 期'(中间含空格)，需精确匹配
                demand_date_str = row.get('需求交付日 期', row.get('需求交付日期', row.get('要求交期', ''))).strip()
                order_date = parse_date(order_date_str)
                demand_date = parse_date(demand_date_str)

                status_raw = row.get('状态', 'pending').strip()
                status = status_map.get(status_raw, 'pending')

                shipping_raw = row.get('运输方式', row.get('备注(空运/海运)', '')).strip()
                shipping_method = shipping_map.get(shipping_raw, 'sea')
                shipping_days = {'air': 3, 'sea': 45, 'express': 1, 'land': 10}.get(shipping_method, 45)

                priority_raw = row.get('优先级', row.get('优先级(1最高-5最低)', 'normal')).strip()
                try:
                    priority = int(float(priority_raw))
                except ValueError:
                    priority = priority_map.get(priority_raw.lower() if priority_raw.isascii() else priority_raw, 4)

                customer_name = row.get('客户名称', '未命名客户').strip()

                SalesOrder.objects.update_or_create(
                    order_no=order_no,
                    defaults={
                        'customer_name': customer_name,
                        'material': material,
                        'quantity': quantity,
                        'unit_price': unit_price,
                        'total_amount': total_amount,
                        'order_date': order_date,
                        'demand_date': demand_date,
                        'priority': priority,
                        'shipping_method': shipping_method,
                        'shipping_days': shipping_days,
                        'status': status,
                        'is_forecast': '预测' in row.get('备注', ''),
                    }
                )
                imported += 1
            except Exception as e:
                logger.warning(f'导入销售订单 {order_no} 失败: {e}')
                continue

    logger.info(f'销售订单从CSV导入完成，共 {imported} 条记录')


def parse_date(date_str):
    """解析日期字符串，支持多种格式"""
    if not date_str:
        return date.today()
    date_str = date_str.strip().split(' ')[0]
    # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
    if '/' in date_str:
        parts = date_str.split('/')
        if len(parts) == 3:
            date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
    # 尝试常见格式
    for fmt in ('%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return date.today()

def import_purchase_orders_from_xlsx():
    """从CSV文件导入采购订单数据（08_采购订单.csv）"""
    csv_path = os.path.join(DATA_ROOT, '08_采购订单.csv')
    if not os.path.exists(csv_path):
        logger.warning(f'采购订单文件不存在: {csv_path}，跳过采购订单导入')
        return

    imported = 0
    po_status_map = {
        # 中文输入
        '待确认': 'draft', '已下达': 'issued', '已确认': 'confirmed',
        '部分到货': 'partial', '已完成': 'completed', '已取消': 'cancelled',
        '待处理': 'pending', '生产中': 'in_production', '已发货': 'shipped',
        '部分发货': 'partial_shipped', '进行中': 'processing',
        # 英文输入（兼容）
        'draft': 'draft', 'issued': 'issued', 'confirmed': 'confirmed',
        'partial': 'partial', 'completed': 'completed', 'cancelled': 'cancelled',
        'pending': 'pending', 'in_production': 'in_production', 'shipped': 'shipped',
        'partial_shipped': 'partial_shipped', 'processing': 'processing'
    }

    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            po_no = row.get('采购订单号', '').strip()
            if not po_no:
                continue
            try:
                supplier_code = row.get('供应商代码', '').strip()
                material_code = row.get('物料代码', '').strip()

                supplier = Supplier.objects.filter(supplier_code=supplier_code).first()
                material = Material.objects.filter(material_code=material_code).first()

                quantity_str = row.get('订单数量', '0').strip().replace(',', '')
                quantity = int(float(quantity_str)) if quantity_str else 0
                unit_price_str = row.get('单价', '0').strip().replace(',', '')
                unit_price = float(unit_price_str) if unit_price_str else 0
                total_amount_str = row.get('总金额', '0').strip().replace(',', '')
                total_amount = float(total_amount_str) if total_amount_str else quantity * unit_price

                order_date = parse_date(row.get('下单日期', '').strip())
                delivery_date = parse_date(row.get('预计交付日期', '').strip())
                actual_delivery = parse_date(row.get('实际交付日期', '').strip()) if row.get('实际交付日期', '').strip() else None

                status_raw = row.get('状态', 'draft').strip()
                status = po_status_map.get(status_raw, 'draft')

                defaults = {
                    'supplier': supplier,
                    'material': material,
                    'quantity': quantity,
                    'unit_price': round(unit_price, 2),
                    'total_amount': round(total_amount, 2),
                    'order_date': order_date,
                    'delivery_date': delivery_date,
                    'status': status
                }
                if actual_delivery:
                    defaults['actual_delivery_date'] = actual_delivery

                PurchaseOrder.objects.update_or_create(
                    po_no=po_no,
                    defaults=defaults
                )
                imported += 1
            except Exception as e:
                logger.warning(f'导入采购订单 {po_no} 失败: {e}')
                continue

    logger.info(f'采购订单从CSV导入完成，共 {imported} 条记录')


def init_work_centers_from_csv():
    """从CSV文件导入工作中心/产线数据（07_产线.csv）"""
    csv_path = os.path.join(DATA_ROOT, '07_产线.csv')
    if not os.path.exists(csv_path):
        logger.warning(f'产线文件不存在: {csv_path}，跳过工作中心导入')
        return

    imported = 0
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get('产线ID', '').strip()
            if not code:
                continue
            try:
                name = row.get('产线名称', '').strip()
                products = row.get('可生产产品', '').strip()
                daily_hours = float(row.get('每日可用工时', '0').strip() or 0)
                shift_count = int(float(row.get('班次数', '1').strip() or 1))
                hours_per_shift = float(row.get('每班工时', '8').strip() or 8)
                prod_days = int(float(row.get('每周生产天数', '5').strip() or 5))
                planned_hc = int(float(row.get('定编人数', '0').strip() or 0))
                actual_hc = int(float(row.get('在岗人数', '0').strip() or 0))
                capacity_limit = int(float(row.get('日产能上限', '0').strip() or 0))
                changeover = float(row.get('换线时间(小时/次)', '0').strip() or 0)
                maint_hours = float(row.get('计划维护停机时长(小时)', '0').strip() or 0)

                # 解析维护日期
                maint_start = None
                maint_end = None
                start_str = row.get('维护生效日期', '').strip()
                end_str = row.get('维护失效日期', '').strip()
                if start_str:
                    maint_start = parse_date(start_str.split(' ')[0])
                if end_str:
                    maint_end = parse_date(end_str.split(' ')[0])

                WorkCenter.objects.update_or_create(
                    work_center_code=code,
                    defaults={
                        'work_center_name': name,
                        'available_products': products,
                        'daily_available_hours': daily_hours,
                        'shift_count': shift_count,
                        'hours_per_shift': hours_per_shift,
                        'production_days_per_week': prod_days,
                        'planned_headcount': planned_hc,
                        'actual_headcount': actual_hc,
                        'daily_capacity_limit': capacity_limit,
                        'changeover_time': changeover,
                        'planned_maintenance_hours': maint_hours,
                        'maintenance_start_date': maint_start,
                        'maintenance_end_date': maint_end,
                        'is_active': True,
                    }
                )
                imported += 1
            except Exception as e:
                logger.warning(f'导入产线 {code} 失败: {e}')
                continue

    logger.info(f'工作中心从CSV导入完成，共 {imported} 条记录')


def import_supplier_commitments_from_csv():
    """从CSV文件导入供应商承诺数据（05_供应商.csv的供应商承诺部分）"""
    csv_path = os.path.join(DATA_ROOT, '05_供应商.csv')
    if not os.path.exists(csv_path):
        logger.warning(f'供应商文件不存在: {csv_path}，跳过供应商承诺导入')
        return

    imported = 0
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            data_type = row.get('数据类型', '').strip()
            if data_type != '供应商承诺':
                continue

            try:
                supplier_code = row.get('供应商ID', '').strip()
                commit_no = row.get('承诺编号', '').strip()
                material_code = row.get('物料ID', '').strip()
                if not commit_no or not material_code:
                    continue

                supplier = Supplier.objects.filter(supplier_code=supplier_code).first()
                material = Material.objects.filter(material_code=material_code).first()
                if not supplier or not material:
                    continue

                qty_str = row.get('承诺数量', '0').strip().replace(',', '')
                quantity = int(float(qty_str)) if qty_str else 0
                delivery_date = parse_date(row.get('承诺交期', '').strip())
                reply_date = parse_date(row.get('回复日期', '').strip()) if row.get('回复日期', '').strip() else None
                remark = row.get('备注', '').strip()

                SupplierCommitment.objects.create(
                    supplier=supplier,
                    material=material,
                    quantity=quantity,
                    delivery_date=delivery_date,
                    order_no=commit_no,
                )
                imported += 1
            except Exception as e:
                logger.warning(f'导入供应商承诺失败: {e}')
                continue

    logger.info(f'供应商承诺从CSV导入完成，共 {imported} 条记录')


def import_suppliers_from_csv():
    """从CSV文件导入供应商数据（05_供应商.csv的供应商信息部分）"""
    csv_path = os.path.join(DATA_ROOT, '05_供应商.csv')
    if not os.path.exists(csv_path):
        logger.warning(f'供应商文件不存在: {csv_path}，跳过供应商导入')
        return

    imported = 0
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            data_type = row.get('数据类型', '').strip()
            if data_type != '供应商信息':
                continue

            supplier_code = row.get('供应商ID', '').strip()
            if not supplier_code:
                continue
            try:
                rating_raw = row.get('供应商评级', 'B').strip()
                rating_map = {'A级': 'A', 'B级': 'B', 'C级': 'C', 'D级': 'D',
                              'A': 'A', 'B': 'B', 'C': 'C', 'D': 'D'}
                rating = rating_map.get(rating_raw, 'B')

                reliability_str = row.get('交付可靠率', '0.9').strip()
                reliability = float(reliability_str) if reliability_str else 0.9
                if reliability > 1:
                    reliability = reliability / 100

                lead_time_str = row.get('正常交期(天)', '7').strip()
                lead_time = int(float(lead_time_str)) if lead_time_str else 7

                Supplier.objects.update_or_create(
                    supplier_code=supplier_code,
                    defaults={
                        'supplier_name': row.get('供应商名称', '').strip(),
                        'contact_person': row.get('联系人', '').strip(),
                        'phone': row.get('联系电话', '').strip(),
                        'email': row.get('邮箱', '').strip(),
                        'address': row.get('地址', '').strip(),
                        'rating': rating,
                        'delivery_reliability': reliability,
                        'normal_lead_time': lead_time,
                        'is_active': True,
                    }
                )
                imported += 1
            except Exception as e:
                logger.warning(f'导入供应商 {supplier_code} 失败: {e}')
                continue

    logger.info(f'供应商从CSV导入完成，共 {imported} 条记录')


def import_customers_from_csv():
    """从CSV文件导入客户数据（06_客户.csv）"""
    csv_path = os.path.join(DATA_ROOT, '06_客户.csv')
    if not os.path.exists(csv_path):
        logger.warning(f'客户文件不存在: {csv_path}，跳过客户导入')
        return

    imported = 0
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            customer_code = row.get('客户ID', '').strip()
            if not customer_code:
                continue
            try:
                credit_str = row.get('信用额度', '0').strip().replace(',', '')
                credit_limit = float(credit_str) if credit_str else 0

                priority_str = row.get('交付优先级', '5').strip()
                try:
                    delivery_priority = int(float(priority_str))
                except ValueError:
                    delivery_priority = 5

                is_active_str = row.get('是否启用', '是').strip()
                is_active = is_active_str in ['是', 'Y', 'yes', 'True', '1']

                Customer.objects.update_or_create(
                    customer_code=customer_code,
                    defaults={
                        'customer_name': row.get('客户名称', '').strip(),
                        'contact_person': row.get('联系人', '').strip(),
                        'phone': row.get('联系电话', '').strip(),
                        'email': row.get('邮箱', '').strip(),
                        'address': row.get('地址', '').strip(),
                        'credit_limit': credit_limit,
                        'customer_type': row.get('客户类型', '其他').strip(),
                        'payment_terms': row.get('付款条件', '月结30天').strip(),
                        'customer_level': row.get('客户等级', 'normal').strip(),
                        'delivery_priority': delivery_priority,
                        'is_active': is_active,
                    }
                )
                imported += 1
            except Exception as e:
                logger.warning(f'导入客户 {customer_code} 失败: {e}')
                continue

    logger.info(f'客户从CSV导入完成，共 {imported} 条记录')


def _fix_invalid_order_statuses():
    """修复数据库中无效的订单状态值（兼容旧数据）"""
    from ..models import SalesOrder, PurchaseOrder

    # 销售订单：'completed' → 'complete'（模型只接受 'complete'）
    bad_so = SalesOrder.objects.filter(status='completed')
    cnt = bad_so.update(status='complete')
    if cnt:
        logger.info(f'修复销售订单状态: completed → complete, 共 {cnt} 条')

    # 修复其他可能的无效状态
    valid_so_statuses = ['pending', 'confirmed', 'in_production', 'allocated',
                         'partial', 'complete', 'processing', 'shipped', 'delivered', 'cancelled']
    invalid_so = SalesOrder.objects.exclude(status__in=valid_so_statuses)
    invalid_cnt = invalid_so.count()
    if invalid_cnt > 0:
        invalid_so.update(status='pending')
        logger.info(f'修复销售订单无效状态: 共 {invalid_cnt} 条重置为 pending')

    # 采购订单：确保状态值在有效范围内
    valid_po_statuses = ['draft', 'pending', 'issued', 'confirmed', 'in_production',
                         'partial', 'partial_shipped', 'shipped', 'processing',
                         'completed', 'cancelled']
    invalid_po = PurchaseOrder.objects.exclude(status__in=valid_po_statuses)
    po_cnt = invalid_po.count()
    if po_cnt > 0:
        invalid_po.update(status='draft')
        logger.info(f'修复采购订单无效状态: 共 {po_cnt} 条重置为 draft')


def _import_system_config_from_csv():
    """
    从09_系统配置.csv导入系统配置数据（工厂日历/工厂调拨/优先级规则）
    
    【修复】原import_all_from_csv()遗漏此文件，现补充调用。
    同时修复了_batch_import_config中factory_code硬编码'DEFAULT'的问题：
    改为从CSV的'工厂代码'列读取F001/F002/F003等多工厂代码。
    """
    from ..views.import_views import _batch_import_config

    csv_path = os.path.join(DATA_ROOT, '09_系统配置.csv')
    if not os.path.exists(csv_path):
        logger.warning(f'系统配置文件不存在: {csv_path}，跳过')
        return

    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if rows:
        result = _batch_import_config(rows, clean_import=True)
        logger.info(f'系统配置(09)导入完成: {result.get("message", "")}, '
                   f'导入{result.get("imported", 0)}条, 更新{result.get("updated", 0)}条')
        if result.get('errors'):
            logger.warning(f'系统配置导入有{len(result["errors"])}个错误: {result["errors"][:5]}')
    else:
        logger.warning('09_系统配置.csv 文件为空')


def import_all_from_csv():
    """从CSV数据集全量导入所有数据"""
    from django.core.cache import cache
    from ..utils.safe_cache import safe_delete

    with transaction.atomic():
        # 1. 物料（01_物料.csv）
        from ..views.import_views import _batch_import_material
        csv_path = os.path.join(DATA_ROOT, '01_物料.csv')
        if os.path.exists(csv_path):
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            if rows:
                _batch_import_material(rows, clean_import=True)
                logger.info(f'物料CSV导入: {len(rows)} 行')

        # 2. 供应商（05_供应商.csv - 供应商信息部分）
        import_suppliers_from_csv()

        # 3. 客户（06_客户.csv）
        import_customers_from_csv()

        # 4. BOM（02_BOM.csv）
        from ..views.import_views import _batch_import_bom
        csv_path = os.path.join(DATA_ROOT, '02_BOM.csv')
        if os.path.exists(csv_path):
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            if rows:
                _batch_import_bom(rows, clean_import=True)
                logger.info(f'BOM CSV导入: {len(rows)} 行')

        # 5. 库存（03_库存.csv）
        init_inventories()

        # 6. 销售订单（04_订单.csv）
        import_sales_orders_from_csv()

        # 6.1 修复数据库中无效的订单状态值
        _fix_invalid_order_statuses()

        # 7. 工作中心（07_产线.csv）
        init_work_centers_from_csv()

        # 8. 采购订单（08_采购订单.csv）
        import_purchase_orders_from_xlsx()

        # 9. 供应商承诺（05_供应商.csv - 供应商承诺部分）
        import_supplier_commitments_from_csv()

        # 10. 系统配置（09_系统配置.csv - 工厂日历/多工厂/优先级规则/调拨）
        #    【修复】原流程遗漏此文件，导致多工厂日历和优先级规则未导入
        _import_system_config_from_csv()

        # 11. 工厂日历兜底（仅当09_系统配置.csv不存在或无工厂日历数据时生成）
        if FactoryCalendar.objects.count() == 0:
            init_factory_calendar()

        # 清除所有缓存
        cache.clear()

    logger.info('CSV数据集全量导入完成')