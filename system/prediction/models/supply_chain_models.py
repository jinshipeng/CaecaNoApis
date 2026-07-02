from django.db import models
from datetime import datetime, date

# 物料类型
MATERIAL_TYPE_CHOICES = [
    ('raw', '原材料'),
    ('semi', '半成品'),
    ('finished', '成品'),
]

# 库存类型
INVENTORY_TYPE_CHOICES = [
    ('local', '本地库存'),
    ('transit', '在途库存'),
    ('supplier', '供应商承诺'),
    ('finished', '成品库存'),
    ('semi', '半成品库存'),
]

# 订单状态
ORDER_STATUS_CHOICES = [
    ('pending', '待处理'),
    ('confirmed', '已确认'),       # 新增：客户已确认订单
    ('in_production', '生产中'),   # 生产中
    ('allocated', '已占料'),        # 已分配物料/生产中
    ('partial', '部分齐套'),
    ('complete', '完全齐套'),
    ('processing', '进行中'),
    ('shipped', '已发货'),
    ('delivered', '已交付'),
    ('cancelled', '已取消'),
]

# 订单类型
ORDER_TYPE_CHOICES = [
    ('standard', '标准订单'),
    ('custom', '客制化订单'),
    ('sample', '样品订单'),
    ('repair', '返修订单'),
    ('framework', '框架协议'),
    ('spare', '备品备件'),
]

# 物流方式
SHIPPING_METHOD_CHOICES = [
    ('sea', '海运'),
    ('air', '空运'),
]

# 采购订单状态
PURCHASE_STATUS_CHOICES = [
    ('draft', '草稿'),
    ('pending', '待处理'),
    ('issued', '已下达'),
    ('confirmed', '已确认'),
    ('in_production', '生产中'),
    ('partial', '部分到货'),
    ('partial_shipped', '部分发货'),
    ('shipped', '已发货'),
    ('processing', '进行中'),
    ('completed', '已完成'),
    ('cancelled', '已取消'),
]


class Material(models.Model):
    """物料模型"""
    material_code = models.CharField(max_length=50, unique=True, db_index=True, verbose_name='物料代码')
    material_name = models.CharField(max_length=200, verbose_name='物料名称')
    material_type = models.CharField(max_length=20, choices=MATERIAL_TYPE_CHOICES, db_index=True, verbose_name='物料类型')
    unit = models.CharField(max_length=20, default='件', verbose_name='单位')
    shelf_life = models.IntegerField(default=0, verbose_name='保质期(天)')
    min_order_qty = models.IntegerField(default=1, verbose_name='最小起订量')
    lead_time = models.IntegerField(default=7, verbose_name='采购/生产提前期(天)')
    standard_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='标准成本(元)')
    sales_price = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='销售价格(元)')
    safety_stock = models.IntegerField(default=0, verbose_name='安全库存')
    min_production_qty = models.IntegerField(default=1, verbose_name='最小生产批量')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '物料'
        verbose_name_plural = '物料'
        db_table = 'material'

    def __str__(self):
        return f'{self.material_code} - {self.material_name}'


SUPPLIER_RATING_CHOICES = [
    ('A', 'A级'),
    ('B', 'B级'),
    ('C', 'C级'),
    ('D', 'D级'),
]

class Supplier(models.Model):
    """供应商模型"""
    supplier_code = models.CharField(max_length=50, unique=True, db_index=True, verbose_name='供应商代码')
    supplier_name = models.CharField(max_length=200, verbose_name='供应商名称')
    contact_person = models.CharField(max_length=100, blank=True, null=True, verbose_name='联系人')
    phone = models.CharField(max_length=50, blank=True, null=True, verbose_name='联系电话')
    email = models.EmailField(blank=True, null=True, verbose_name='邮箱')
    address = models.TextField(blank=True, null=True, verbose_name='地址')
    rating = models.CharField(max_length=10, choices=SUPPLIER_RATING_CHOICES, default='B', verbose_name='供应商评级')
    delivery_reliability = models.FloatField(default=0.9, verbose_name='交付可靠率')
    normal_lead_time = models.IntegerField(default=7, verbose_name='正常交期(天)')
    payment_terms = models.CharField(max_length=50, blank=True, null=True, default='月结30天', verbose_name='结算方式')
    min_order_qty = models.IntegerField(blank=True, null=True, default=100, verbose_name='最小起订量(件)')
    capacity_level = models.CharField(max_length=20, blank=True, null=True, default='B', verbose_name='产能等级')
    cooperation_years = models.IntegerField(blank=True, null=True, default=3, verbose_name='合作年限(年)')
    warranty_months = models.IntegerField(blank=True, null=True, default=12, verbose_name='质保期(月)')
    on_time_delivery_rate = models.FloatField(blank=True, null=True, default=0.95, verbose_name='准时交付率')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '供应商'
        verbose_name_plural = '供应商'
        db_table = 'supplier'

    def __str__(self):
        return f'{self.supplier_code} - {self.supplier_name}'


class Customer(models.Model):
    """客户模型"""
    customer_code = models.CharField(max_length=50, unique=True, db_index=True, verbose_name='客户代码')
    customer_name = models.CharField(max_length=200, verbose_name='客户名称')
    contact_person = models.CharField(max_length=100, blank=True, null=True, verbose_name='联系人')
    phone = models.CharField(max_length=50, blank=True, null=True, verbose_name='联系电话')
    email = models.EmailField(blank=True, null=True, verbose_name='邮箱')
    address = models.TextField(blank=True, null=True, verbose_name='地址')
    credit_limit = models.DecimalField(max_digits=20, decimal_places=2, default=0, verbose_name='信用额度')
    customer_type = models.CharField(max_length=20, default='其他', verbose_name='客户类型')
    payment_terms = models.CharField(max_length=50, default='月结30天', verbose_name='付款条件')
    customer_level = models.CharField(max_length=20, default='normal', verbose_name='客户等级')
    delivery_priority = models.IntegerField(default=5, verbose_name='交付优先级')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '客户'
        verbose_name_plural = '客户'
        db_table = 'customer'

    def __str__(self):
        return f'{self.customer_code} - {self.customer_name}'


class PurchaseOrder(models.Model):
    """采购订单模型"""
    po_no = models.CharField(max_length=100, unique=True, db_index=True, verbose_name='采购订单号')
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, verbose_name='供应商')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, verbose_name='物料')
    quantity = models.IntegerField(default=0, verbose_name='订单数量')
    unit_price = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='单价')
    total_amount = models.DecimalField(max_digits=20, decimal_places=2, default=0, verbose_name='总金额')
    order_date = models.DateField(verbose_name='下单日期')
    delivery_date = models.DateField(db_index=True, verbose_name='预计交付日期')
    actual_delivery_date = models.DateField(blank=True, null=True, verbose_name='实际交付日期')
    status = models.CharField(max_length=20, choices=PURCHASE_STATUS_CHOICES, default='draft', db_index=True, verbose_name='订单状态')
    remarks = models.TextField(blank=True, null=True, verbose_name='备注')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '采购订单'
        verbose_name_plural = '采购订单'
        db_table = 'purchase_order'

    def __str__(self):
        return f'{self.po_no} - {self.supplier.supplier_name}'

    def save(self, *args, **kwargs):
        self.total_amount = self.quantity * self.unit_price
        super().save(*args, **kwargs)


class SupplierMaterial(models.Model):
    """供应商物料关系模型"""
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, verbose_name='供应商')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, verbose_name='物料')
    lead_time = models.IntegerField(default=7, verbose_name='交货周期(天)')
    unit_price = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='单价')
    min_order_qty = models.IntegerField(default=1, verbose_name='最小起订量')
    is_forbidden = models.BooleanField(default=False, verbose_name='是否禁用')
    forbidden_reason = models.CharField(max_length=500, blank=True, null=True, verbose_name='禁用原因')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '供应商物料'
        verbose_name_plural = '供应商物料'
        db_table = 'supplier_material'
        unique_together = ('supplier', 'material')

    def __str__(self):
        return f'{self.supplier.supplier_name} - {self.material.material_code}'


class BillOfMaterials(models.Model):
    """BOM模型"""
    parent_material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name='bom_parent', verbose_name='父物料')
    child_material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name='bom_child', verbose_name='子物料')
    quantity = models.DecimalField(max_digits=15, decimal_places=4, default=1, verbose_name='用量')
    unit = models.CharField(max_length=20, default='件', verbose_name='单位')
    bom_level = models.IntegerField(default=1, verbose_name='BOM层级')
    usage_ratio = models.FloatField(default=0.0, verbose_name='用量占比(%)')
    scrap_rate = models.FloatField(default=0.0, verbose_name='报废率')
    alternative_group = models.CharField(max_length=200, blank=True, null=True, verbose_name='替代料组')
    alternative_priority = models.IntegerField(default=1, verbose_name='替代优先级')
    alternative_ratio = models.FloatField(default=1.0, verbose_name='替代比例')
    factory_code = models.CharField(max_length=50, blank=True, null=True, verbose_name='工厂代码')
    ecn_no = models.CharField(max_length=100, blank=True, null=True, verbose_name='ECN编号')
    ecn_date = models.DateField(blank=True, null=True, verbose_name='ECN变更日期')
    ecn_reason = models.TextField(blank=True, null=True, verbose_name='ECN变更原因')
    version = models.IntegerField(default=1, verbose_name='BOM版本号')
    is_configurable = models.BooleanField(default=False, verbose_name='是否可配置(CTO)')
    config_group = models.CharField(max_length=50, blank=True, null=True, verbose_name='配置组')
    config_options = models.JSONField(blank=True, null=True, verbose_name='配置选项')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = 'BOM'
        verbose_name_plural = 'BOM'
        db_table = 'bill_of_materials'
        indexes = [
            # 优化：按父物料查询子物料（用于齐套率计算）
            models.Index(fields=['parent_material_id', 'is_active'], name='bom_parent_active_idx'),
            # 优化：按子物料查询父物料（反向查询）
            models.Index(fields=['child_material_id'], name='bom_child_idx'),
        ]

    def __str__(self):
        return f'{self.parent_material.material_code} -> {self.child_material.material_code} x {self.quantity}'


class Inventory(models.Model):
    """库存模型"""
    material = models.ForeignKey(Material, on_delete=models.CASCADE, verbose_name='物料')
    inventory_type = models.CharField(max_length=20, choices=INVENTORY_TYPE_CHOICES, db_index=True, verbose_name='库存类型')
    quantity = models.IntegerField(default=0, verbose_name='在库数量')
    hold_quantity = models.IntegerField(default=0, verbose_name='Hold数量')
    locked_quantity = models.IntegerField(default=0, verbose_name='锁定数量(已分配未出库)')
    available_quantity = models.IntegerField(default=0, verbose_name='可用数量')
    warehouse = models.CharField(max_length=100, blank=True, null=True, verbose_name='仓库')
    location = models.CharField(max_length=100, blank=True, null=True, verbose_name='库位')
    batch_no = models.CharField(max_length=50, blank=True, null=True, verbose_name='批次号')
    expiry_date = models.DateField(blank=True, null=True, verbose_name='有效期')
    is_hold = models.BooleanField(default=False, db_index=True, verbose_name='是否冻结')
    hold_reason = models.CharField(max_length=500, blank=True, null=True, verbose_name='冻结原因')
    hold_until = models.DateField(blank=True, null=True, verbose_name='冻结截止日期')
    data_date = models.DateField(blank=True, null=True, verbose_name='数据日期')
    factory_code = models.CharField(max_length=50, blank=True, null=True, db_index=True, verbose_name='所属工厂代码')
    safety_stock_lower = models.IntegerField(default=0, verbose_name='安全库存下限')
    target_level = models.IntegerField(default=0, verbose_name='目标水位')
    max_stock_upper = models.IntegerField(default=0, verbose_name='库存上限')
    is_restricted = models.BooleanField(default=False, verbose_name='是否禁用(供应商禁用料)')
    restricted_reason = models.CharField(max_length=500, blank=True, null=True, verbose_name='禁用原因')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '库存'
        verbose_name_plural = '库存'
        db_table = 'inventory'
        unique_together = ('material', 'warehouse')
        indexes = [
            # 优化：按物料查询库存的复合索引（用于齐套率计算等场景）
            models.Index(fields=['material', 'inventory_type'], name='inv_material_type_idx'),
            # 优化：按物料+仓库查询（同时作为唯一约束的支撑索引）
            models.Index(fields=['material', 'warehouse'], name='inv_material_warehouse_idx'),
            # 优化：按工厂+物料查询
            models.Index(fields=['factory_code', 'material_id'], name='inv_factory_material_idx'),
        ]

    def __str__(self):
        return f'{self.material.material_code} - {self.inventory_type} - {self.quantity}'

    def save(self, *args, **kwargs):
        # 自动计算可用数量: 可用 = 在库 - Hold - 锁定
        self.available_quantity = max(0, self.quantity - self.hold_quantity - self.locked_quantity)
        super().save(*args, **kwargs)

    def lock(self, qty):
        """锁定指定数量（已分配给订单但未出库）"""
        if qty > self.available_quantity:
            raise ValueError(f'锁定数量{qty}超过可用数量{self.available_quantity}')
        self.locked_quantity += qty
        self.save(update_fields=['locked_quantity', 'available_quantity', 'updated_at'])

    def unlock(self, qty):
        """释放锁定数量"""
        self.locked_quantity = max(0, self.locked_quantity - qty)
        self.save(update_fields=['locked_quantity', 'available_quantity', 'updated_at'])


class SalesOrder(models.Model):
    """销售订单模型"""
    order_no = models.CharField(max_length=100, unique=True, db_index=True, verbose_name='销售订单号')
    customer_name = models.CharField(max_length=200, verbose_name='客户名称')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, verbose_name='成品物料')
    quantity = models.IntegerField(default=0, verbose_name='订单数量')
    unit_price = models.DecimalField(max_digits=20, decimal_places=2, default=0, verbose_name='单价')
    total_amount = models.DecimalField(max_digits=20, decimal_places=2, default=0, verbose_name='总金额')
    order_date = models.DateField(null=True, blank=True, verbose_name='下单日期')
    demand_date = models.DateField(db_index=True, verbose_name='需求交付日期')
    status = models.CharField(max_length=20, choices=ORDER_STATUS_CHOICES, default='pending', db_index=True, verbose_name='订单状态')
    order_type = models.CharField(max_length=20, choices=ORDER_TYPE_CHOICES, default='standard', db_index=True,
                                   verbose_name='订单类型',
                                   help_text='区分标准/客制化/样品/返修/框架协议/备品备件等业务场景')
    priority = models.IntegerField(default=1, db_index=True, verbose_name='优先级')
    shipping_method = models.CharField(max_length=20, choices=SHIPPING_METHOD_CHOICES, default='sea', verbose_name='物流方式')
    shipping_days = models.IntegerField(default=45, verbose_name='运输天数')
    production_lead_time = models.IntegerField(default=2, verbose_name='生产周期(天)')
    is_forecast = models.BooleanField(default=False, verbose_name='是否预测订单')
    allow_early_delivery = models.BooleanField(default=True, verbose_name='允许提前交货')
    earliest_delivery_date = models.DateField(blank=True, null=True, verbose_name='最早可交货日期')
    factory_code = models.CharField(max_length=50, blank=True, null=True, db_index=True, verbose_name='需求工厂代码')
    actual_delivery_date = models.DateField(blank=True, null=True, db_index=True, verbose_name='实际交付日期')
    delivery_priority = models.IntegerField(default=5, verbose_name='交付优先级顺序')
    remarks = models.TextField(blank=True, null=True, verbose_name='备注')
    config_options = models.JSONField(blank=True, null=True, verbose_name='CTO配置选项')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '销售订单'
        verbose_name_plural = '销售订单'
        db_table = 'sales_order'
        indexes = [
            # 优化：按状态+优先级查询（用于待办事项、齐套率计算）
            models.Index(fields=['status', 'priority'], name='order_status_priority_idx'),
            # 优化：按需求日期+状态查询（用于逾期订单检测）
            models.Index(fields=['demand_date', 'status'], name='order_demand_status_idx'),
            # 优化：按物料+状态查询（用于物料级别的统计分析）
            models.Index(fields=['material_id', 'status'], name='order_material_status_idx'),
            # 优化：按创建时间降序查询（用于最近活动列表）
            models.Index(fields=['-created_at'], name='order_created_desc_idx'),
        ]

    def __str__(self):
        return f'{self.order_no} - {self.customer_name}'

    def save(self, *args, **kwargs):
        if self.quantity and self.unit_price:
            self.total_amount = self.quantity * self.unit_price
        super().save(*args, **kwargs)


class Capacity(models.Model):
    """产能模型"""
    work_center = models.CharField(max_length=100, db_index=True, verbose_name='工作中心')
    material = models.ForeignKey(Material, on_delete=models.SET_NULL, blank=True, null=True, verbose_name='物料')
    daily_capacity = models.IntegerField(default=0, verbose_name='日产能')
    weekly_capacity = models.IntegerField(default=0, verbose_name='周产能')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '产能'
        verbose_name_plural = '产能'
        db_table = 'capacity'

    def __str__(self):
        return f'{self.work_center} - {self.daily_capacity}/天'


class WorkCenter(models.Model):
    """工作中心/产线模型"""
    work_center_code = models.CharField(max_length=50, unique=True, db_index=True, verbose_name='产线ID')
    work_center_name = models.CharField(max_length=200, verbose_name='产线名称')
    available_products = models.TextField(blank=True, null=True, verbose_name='可生产产品(逗号分隔)')
    daily_available_hours = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name='每日可用工时')
    shift_count = models.IntegerField(default=1, verbose_name='班次数')
    hours_per_shift = models.DecimalField(max_digits=10, decimal_places=2, default=8, verbose_name='每班工时')
    production_days_per_week = models.IntegerField(default=5, verbose_name='每周生产天数')
    planned_headcount = models.IntegerField(default=0, verbose_name='定编人数')
    actual_headcount = models.IntegerField(default=0, verbose_name='在岗人数')
    daily_capacity_limit = models.IntegerField(default=0, verbose_name='日产能上限')
    changeover_time = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name='换线时间(小时/次)')
    planned_maintenance_hours = models.DecimalField(max_digits=10, decimal_places=2, default=0, verbose_name='计划维护停机时长(小时)')
    maintenance_start_date = models.DateField(blank=True, null=True, verbose_name='维护生效日期')
    maintenance_end_date = models.DateField(blank=True, null=True, verbose_name='维护失效日期')
    is_active = models.BooleanField(default=True, db_index=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '工作中心'
        verbose_name_plural = '工作中心'
        db_table = 'work_center'

    def __str__(self):
        return f'{self.work_center_code} - {self.work_center_name}'


class SupplierCommitment(models.Model):
    """供应商承诺模型"""
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, verbose_name='供应商')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, verbose_name='物料')
    quantity = models.IntegerField(default=0, verbose_name='承诺数量')
    delivery_date = models.DateField(verbose_name='预计交付日期')
    order_no = models.CharField(max_length=100, blank=True, null=True, verbose_name='关联采购订单号')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '供应商承诺'
        verbose_name_plural = '供应商承诺'
        db_table = 'supplier_commitment'
        indexes = [
            # 优化：按供应商+交付日期查询（用于绩效报表）
            models.Index(fields=['supplier_id', 'delivery_date'], name='commit_supplier_date_idx'),
            # 优化：按物料+交付日期查询
            models.Index(fields=['material_id', 'delivery_date'], name='commit_material_date_idx'),
        ]

    def __str__(self):
        return f'{self.supplier.supplier_name} - {self.material.material_code} - {self.delivery_date}'


class OrderAllocation(models.Model):
    """订单物料分配模型"""
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, verbose_name='销售订单')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, verbose_name='物料')
    inventory_id = models.IntegerField(blank=True, null=True, verbose_name='库存ID')
    allocated_quantity = models.IntegerField(default=0, verbose_name='已分配数量')
    required_quantity = models.IntegerField(default=0, verbose_name='需求数量')
    shortage_quantity = models.IntegerField(default=0, verbose_name='短缺数量')
    is_alternative = models.BooleanField(default=False, verbose_name='是否替代料')
    reliability_factor = models.FloatField(default=1.0, verbose_name='可靠率因子')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '订单物料分配'
        verbose_name_plural = '订单物料分配'
        db_table = 'order_allocation'

    def __str__(self):
        return f'{self.order.order_no} - {self.material.material_code}'


class FactoryCalendar(models.Model):
    """工厂日历模型 - 支持多工厂差异化日历配置"""
    factory_code = models.CharField(max_length=50, default='DEFAULT', db_index=True, verbose_name='工厂代码')
    date = models.DateField(verbose_name='日期')
    is_workday = models.BooleanField(default=True, verbose_name='是否工作日')
    shift_count = models.IntegerField(default=1, verbose_name='班次数量')
    remarks = models.CharField(max_length=200, blank=True, null=True, verbose_name='备注')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '工厂日历'
        verbose_name_plural = '工厂日历'
        db_table = 'factory_calendar'
        unique_together = [['factory_code', 'date']]

    def __str__(self):
        return f'[{self.factory_code}] {self.date} - {"工作日" if self.is_workday else "休息日"}'


class PlanLog(models.Model):
    """计划日志模型"""
    LOG_TYPE_CHOICES = [
        ('INFO', '信息'),
        ('WARNING', '警告'),
        ('ERROR', '错误'),
        ('PLANNING', '计划'),
    ]
    
    log_type = models.CharField(max_length=20, choices=LOG_TYPE_CHOICES, default='INFO', verbose_name='日志类型')
    message = models.TextField(verbose_name='日志内容')
    order_id = models.IntegerField(blank=True, null=True, verbose_name='订单ID')
    material_id = models.IntegerField(blank=True, null=True, verbose_name='物料ID')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '计划日志'
        verbose_name_plural = '计划日志'
        db_table = 'plan_log'

    def __str__(self):
        return f'[{self.log_type}] {self.message[:50]}'


class MaterialPlanResult(models.Model):
    """物料计划结果模型"""
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, verbose_name='销售订单')
    is_complete = models.BooleanField(default=False, verbose_name='是否完全齐套')
    complete_rate = models.FloatField(default=0.0, verbose_name='齐套率')
    shortage_details = models.JSONField(blank=True, null=True, verbose_name='缺料详情')
    allocation_details = models.JSONField(blank=True, null=True, verbose_name='分配详情')
    previous_complete_rate = models.FloatField(blank=True, null=True, verbose_name='上次齐套率')
    stability_score = models.FloatField(blank=True, null=True, verbose_name='稳定性评分')
    delivery_change_count = models.IntegerField(default=0, verbose_name='交期变更次数')
    is_early_delivery = models.BooleanField(default=False, verbose_name='是否提前交货')
    transfer_details = models.JSONField(blank=True, null=True, verbose_name='调拨详情')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '物料计划结果'
        verbose_name_plural = '物料计划结果'
        db_table = 'material_plan_result'

    def __str__(self):
        return f'{self.order.order_no} - {"齐套" if self.is_complete else "缺料"}'


class PriorityRule(models.Model):
    """动态优先级规则模型"""
    STRATEGY_CHOICES = [
        ('delivery_first', '交付优先'),
        ('inventory_first', '库存优先'),
        ('supplier_first', '供应商优先'),
        ('stability_first', '稳定性优先'),
        ('cost_first', '成本优先'),
        ('expiry_first', '临期优先'),
    ]

    name = models.CharField(max_length=100, verbose_name='规则名称')
    strategy = models.CharField(max_length=20, choices=STRATEGY_CHOICES, default='delivery_first', verbose_name='策略类型')
    urgency_weight = models.FloatField(default=0.3, verbose_name='紧急度权重')
    customer_weight = models.FloatField(default=0.2, verbose_name='客户等级权重')
    delivery_weight = models.FloatField(default=0.3, verbose_name='交期紧迫度权重')
    value_weight = models.FloatField(default=0.1, verbose_name='订单价值权重')
    product_weight = models.FloatField(default=0.1, verbose_name='产品组权重')
    inventory_status_weight = models.FloatField(default=0.0, verbose_name='库存状态权重')
    capacity_utilization_weight = models.FloatField(default=0.0, verbose_name='产能利用率权重')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '优先级规则'
        verbose_name_plural = '优先级规则'
        db_table = 'priority_rule'

    def __str__(self):
        return f'{self.name} - {self.get_strategy_display()}'


# 工厂调拨状态
TRANSFER_STATUS_CHOICES = [
    ('pending', '待审批'),
    ('approved', '已审批'),
    ('in_transit', '调拨中'),
    ('completed', '已完成'),
    ('cancelled', '已取消'),
]


class FactoryTransfer(models.Model):
    """工厂间物料调拨模型"""
    transfer_no = models.CharField(max_length=100, unique=True, db_index=True, verbose_name='调拨单号')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, verbose_name='物料')
    from_factory = models.CharField(max_length=50, verbose_name='调出工厂代码')
    to_factory = models.CharField(max_length=50, verbose_name='调入工厂代码')
    quantity = models.IntegerField(default=0, verbose_name='调拨数量')
    transfer_days = models.IntegerField(default=1, verbose_name='调拨耗时(天)')
    transfer_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0, verbose_name='调拨成本(元)')
    status = models.CharField(max_length=20, choices=TRANSFER_STATUS_CHOICES, default='pending', db_index=True, verbose_name='调拨状态')
    related_order = models.ForeignKey(SalesOrder, blank=True, null=True, on_delete=models.SET_NULL, verbose_name='关联销售订单')
    expected_arrival_date = models.DateField(blank=True, null=True, verbose_name='预计到达日期')
    actual_arrival_date = models.DateField(blank=True, null=True, verbose_name='实际到达日期')
    reason = models.CharField(max_length=500, blank=True, null=True, verbose_name='调拨原因')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '工厂调拨'
        verbose_name_plural = '工厂调拨'
        db_table = 'factory_transfer'

    def __str__(self):
        return f'{self.transfer_no} - {self.from_factory} -> {self.to_factory}'


class EngineeringChange(models.Model):
    """工程变更(ECN)模型 - 管理物料替换、环保合规、用量调整等变更"""
    ECN_STATUS_CHOICES = [
        ('active', '启用'),
        ('inactive', '停用'),
    ]

    ecn_no = models.CharField(max_length=100, unique=True, db_index=True, verbose_name='ECN编号')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, verbose_name='变更物料')
    change_type = models.CharField(max_length=50, default='材料替换', verbose_name='变更类型',
                                    help_text='材料替换/环保合规/用量调整/质量升级/成本优化')
    reason = models.TextField(blank=True, null=True, verbose_name='变更原因')
    related_product = models.CharField(max_length=50, blank=True, null=True, verbose_name='关联产品编码')
    ecn_category = models.CharField(max_length=100, blank=True, null=True, verbose_name='ECN类别名称',
                                     help_text='如: ECN-材料替换-P0051')
    effective_date = models.DateField(verbose_name='生效日期')
    expiry_date = models.DateField(blank=True, null=True, verbose_name='失效日期')
    status = models.CharField(max_length=20, choices=ECN_STATUS_CHOICES, default='active', verbose_name='状态')
    remarks = models.TextField(blank=True, null=True, verbose_name='备注')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '工程变更'
        verbose_name_plural = '工程变更'
        db_table = 'engineering_change'

    def __str__(self):
        return f'{self.ecn_no} - {self.change_type}'


class DeliveryChange(models.Model):
    """交期变更记录模型 — 赛题目标2：追踪供应商承诺交期变化"""
    CHANGE_TYPE_CHOICES = [
        ('supplier_delay', '供应商延期'),
        ('supplier_advance', '供应商提前'),
        ('customer_rush', '客户加急'),
        ('customer_postpone', '客户延后'),
        ('logistics_issue', '物流问题'),
        ('material_shortage', '物料短缺'),
    ]

    order_no = models.CharField(max_length=100, db_index=True, verbose_name='关联订单号')
    po_no = models.CharField(max_length=100, blank=True, null=True, db_index=True, verbose_name='关联采购单号')
    material_code = models.CharField(max_length=50, blank=True, null=True, verbose_name='物料代码')
    supplier_code = models.CharField(max_length=50, blank=True, null=True, verbose_name='供应商代码')
    change_type = models.CharField(max_length=30, choices=CHANGE_TYPE_CHOICES, default='supplier_delay', verbose_name='变更类型')
    original_date = models.DateField(verbose_name='原定交付日期')
    new_date = models.DateField(verbose_name='新交付日期')
    change_days = models.IntegerField(default=0, verbose_name='变更天数(正=延后/负=提前)')
    reason = models.TextField(blank=True, null=True, verbose_name='变更原因')
    change_by = models.CharField(max_length=50, default='system', verbose_name='变更来源')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='变更时间')

    class Meta:
        verbose_name = '交期变更记录'
        verbose_name_plural = '交期变更记录'
        db_table = 'delivery_change'
        indexes = [
            models.Index(fields=['order_no', 'created_at'], name='dc_order_time_idx'),
            models.Index(fields=['supplier_code', 'created_at'], name='dc_supplier_time_idx'),
            models.Index(fields=['material_code'], name='dc_material_idx'),
        ]

    def __str__(self):
        return f'{self.order_no} {self.original_date} -> {self.new_date}'


class SubstituteMaterial(models.Model):
    """替代物料模型：管理可互相替代的物料组"""
    group_id = models.CharField(max_length=50, db_index=True, verbose_name='替代料组ID')
    group_name = models.CharField(max_length=200, verbose_name='替代料组名称')
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name='substitute_groups', verbose_name='物料')
    priority = models.IntegerField(default=1, verbose_name='替代优先级(1最高)')
    ratio = models.FloatField(default=1.0, verbose_name='总体用料比例')
    purchase_ratio = models.FloatField(default=1.0, verbose_name='采购比例',
                                       help_text='采购备料时的比例分配，与用料比例独立。如用料60:40但采购70:30')
    is_default = models.BooleanField(default=False, verbose_name='是否默认')
    remark = models.TextField(blank=True, null=True, verbose_name='备注')
    is_active = models.BooleanField(default=True, verbose_name='是否启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '替代物料'
        verbose_name_plural = '替代物料'
        db_table = 'substitute_material'
        unique_together = [['group_id', 'material']]
        indexes = [
            models.Index(fields=['group_id', 'priority'], name='idx_sub_group_priority'),
            models.Index(fields=['material'], name='idx_sub_material'),
        ]

    def __str__(self):
        return f'{self.group_id} - {self.material.material_code}(P{self.priority})'


class ConsumptionRule(models.Model):
    """
    特殊消耗规则模型
    
    支持细粒度的库存消耗控制，解决赛题中"特殊消耗限制"的痛点：
    - 按客户等级设置消耗限制（如"A类客户优先消耗优质批次库存"）
    - 按订单类型设置消耗限制（如"预测订单只能消耗在途库存"）
    - 按物料批次设置特殊限制（如"某批次物料仅限特定产品线使用"）
    - 订单/BOM/库存间特殊关联占料限制
    
    规则表达式使用简化的JSON条件格式，
    由MaterialPlanner在分配时动态解析和执行。
    
    使用场景示例：
    1. 客户Apple的订单只能消耗批次号以'A'开头的库存
    2. 预测订单(is_forecast=True)优先消耗transit类型库存
    3. 紧急订单(priority<=2)可突破安全库存保护
    4. 物料MAT-RAW-0001只能用于产品FIN-001
    """

    RULE_TYPE_CHOICES = [
        ('customer_restriction', '客户消耗限制'),
        ('order_type_restriction', '订单类型限制'),
        ('batch_restriction', '批次限制'),
        ('material_product_binding', '物料-产品绑定'),
        ('inventory_type_preference', '库存类型偏好'),
        ('safety_stock_override', '安全库存覆盖'),
        ('priority_boost', '优先级加成'),
    ]

    PRIORITY_CHOICES = [
        (1, '最高（必须满足）'),
        (2, '高（强烈推荐）'),
        (3, '中（正常执行）'),
        (4, '低（可选执行）'),
        (5, '最低（仅作参考）'),
    ]

    name = models.CharField('规则名称', max_length=100, unique=True)
    rule_type = models.CharField(
        '规则类型', max_length=30, choices=RULE_TYPE_CHOICES, default='customer_restriction'
    )
    priority = models.IntegerField('规则优先级', default=3, choices=PRIORITY_CHOICES,
                                   help_text='多条规则冲突时，高优规则优先')
    is_active = models.BooleanField('是否启用', default=True)

    # ===== 条件匹配字段 =====
    # 适用客户（空表示所有客户）
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='consumption_rules', verbose_name='适用客户'
    )
    # 适用物料（空表示所有物料）
    material = models.ForeignKey(
        Material, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='consumption_rules', verbose_name='适用物料'
    )
    # 适用工厂
    factory_code = models.CharField('工厂代码', max_length=50, null=True, blank=True)

    # ===== 条件表达式（JSON格式）=====
    # 支持的条件操作符: eq(等于), ne(不等于), in(包含), not_in(不包含),
    #                    gt(大于), lt(小于), gte(>=), lte(<=), contains(包含字符串)
    # 示例: {"order_priority": {"lte": 2}, "is_forecast": {"eq": false}}
    condition_expression = models.JSONField(
        '条件表达式', default=dict, blank=True,
        help_text='JSON格式的条件匹配规则，用于精细控制何时触发此规则'
    )

    # ===== 动作定义（JSON格式）=====
    # 支持的动作:
    #   - inventory_types: 允许消耗的库存类型列表（如["local", "transit"]）
    #   - excluded_batches: 排除的批次号列表
    #   - required_batch_prefix: 要求的批次号前缀
    #   - safety_stock_override: 安全库存覆盖比例(0-1)，0表示不可突破
    #   - allocation_boost: 分配优先级加成系数(>1提升/<1降低)
    #   - max_allocation_pct: 最大分配占比(0-1)
    action_definition = models.JSONField(
        '动作定义', default=dict, blank=True,
        help_text='JSON格式，定义规则命中后执行的消耗控制动作'
    )

    # ===== 元数据 =====
    description = models.TextField('规则说明', blank=True, default='')
    created_by = models.CharField('创建人', max_length=50, default='system')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '特殊消耗规则'
        verbose_name_plural = '特殊消耗规则'
        db_table = 'consumption_rule'
        indexes = [
            models.Index(fields=['rule_type', 'is_active'], name='idx_cr_type_active'),
            models.Index(fields=['customer', 'material', 'factory_code'], name='idx_cr_scope'),
            models.Index(fields=['priority', 'is_active'], name='idx_cr_priority'),
        ]

    def __str__(self):
        return f'{self.name} [{self.get_rule_type_display()}] P{self.priority}'

    def matches(self, context: dict) -> bool:
        """
        检查当前上下文是否匹配此规则
        
        Args:
            context: 包含以下字段的字典
                - order: SalesOrder实例或其属性字典
                - material_id: 当前分配的物料ID
                - factory_code: 工厂代码
                - inventory_type: 库存类型
                - batch_no: 批次号
                
        Returns:
            bool: 是否匹配此规则
        """
        if not self.is_active:
            return False

        expr = self.condition_expression or {}
        if not expr:
            return True  # 无条件表达式视为始终匹配

        # 客户匹配
        if self.customer and context.get('customer_id'):
            if str(context['customer_id']) != str(self.customer.id):
                return False

        # 物料匹配
        if self.material and context.get('material_id'):
            if str(context['material_id']) != str(self.material.id):
                return False

        # 工厂匹配
        if self.factory_code and context.get('factory_code'):
            if context['factory_code'] != self.factory_code:
                return False

        # 条件表达式求值
        for key, condition in expr.items():
            ctx_value = context.get(key)

            if isinstance(condition, dict):
                for op, val in condition.items():
                    if op == 'eq' and ctx_value != val:
                        return False
                    elif op == 'ne' and ctx_value == val:
                        return False
                    elif op == 'in' and ctx_value not in val:
                        return False
                    elif op == 'not_in' and ctx_value in val:
                        return False
                    elif op == 'gt' and not (ctx_value is not None and ctx_value > val):
                        return False
                    elif op == 'lt' and not (ctx_value is not None and ctx_value < val):
                        return False
                    elif op == 'gte' and not (ctx_value is not None and ctx_value >= val):
                        return False
                    elif op == 'lte' and not (ctx_value is not None and ctx_value <= val):
                        return False
                    elif op == 'contains':
                        if ctx_value is None or str(val) not in str(ctx_value):
                            return False
            else:
                # 简单等值比较
                if ctx_value != condition:
                    return False

        return True

    def get_action(self, key: str, default=None):
        """获取动作定义中的指定参数"""
        actions = self.action_definition or {}
        return actions.get(key, default)


class HoldAuditLog(models.Model):
    """Hold操作审计日志 - 记录所有Hold/UnHold操作的完整轨迹"""

    OPERATION_CHOICES = [
        ('hold', '冻结'),
        ('unhold', '解冻'),
        ('auto_release', '自动解冻'),
    ]

    inventory = models.ForeignKey(Inventory, on_delete=models.CASCADE, related_name='hold_audit_logs',
                                   verbose_name='库存记录')
    operation = models.CharField(max_length=20, choices=OPERATION_CHOICES, verbose_name='操作类型')
    quantity = models.IntegerField(default=0, verbose_name='操作数量')
    reason = models.CharField(max_length=500, blank=True, null=True, verbose_name='操作原因')
    hold_until = models.DateField(blank=True, null=True, verbose_name='冻结截止日期')
    operated_by = models.CharField(max_length=100, default='system', verbose_name='操作人')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='操作时间')

    class Meta:
        verbose_name = 'Hold操作审计'
        verbose_name_plural = 'Hold操作审计'
        db_table = 'hold_audit_log'
        indexes = [
            models.Index(fields=['inventory', 'operation'], name='hal_inv_op_idx'),
            models.Index(fields=['created_at'], name='hal_created_idx'),
        ]

    def __str__(self):
        return f'{self.get_operation_display()} - {self.inventory_id} - {self.quantity}'


class BOMChangeHistory(models.Model):
    """BOM变更历史 - 记录ECN工程变更的版本间差异"""

    bom = models.ForeignKey(BillOfMaterials, on_delete=models.CASCADE, related_name='change_history',
                             verbose_name='BOM记录')
    ecn_no = models.CharField(max_length=100, verbose_name='ECN编号')
    from_version = models.IntegerField(verbose_name='变更前版本')
    to_version = models.IntegerField(verbose_name='变更后版本')
    change_type = models.CharField(max_length=20, default='modify', verbose_name='变更类型',
                                    help_text='add/modify/delete/replace')
    old_child_material_code = models.CharField(max_length=50, blank=True, null=True, verbose_name='变更前子物料代码')
    old_quantity = models.DecimalField(max_digits=15, decimal_places=4, blank=True, null=True, verbose_name='变更前用量')
    old_alternative_ratio = models.FloatField(blank=True, null=True, verbose_name='变更前替代比例')
    new_child_material_code = models.CharField(max_length=50, blank=True, null=True, verbose_name='变更后子物料代码')
    new_quantity = models.DecimalField(max_digits=15, decimal_places=4, blank=True, null=True, verbose_name='变更后用量')
    new_alternative_ratio = models.FloatField(blank=True, null=True, verbose_name='变更后替代比例')
    reason = models.TextField(blank=True, null=True, verbose_name='变更原因')
    effective_date = models.DateField(verbose_name='生效日期')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='记录时间')

    class Meta:
        verbose_name = 'BOM变更历史'
        verbose_name_plural = 'BOM变更历史'
        db_table = 'bom_change_history'
        indexes = [
            models.Index(fields=['ecn_no'], name='bch_ecn_idx'),
            models.Index(fields=['bom', 'from_version', 'to_version'], name='bch_bom_ver_idx'),
        ]

    def __str__(self):
        return f'{self.ecn_no} v{self.from_version}->v{self.to_version}'


def auto_release_expired_holds():
    """自动解冻到期的Hold库存 - 可由定时任务调用"""
    from django.utils import timezone
    today = timezone.now().date()
    expired_inventories = Inventory.objects.filter(
        is_hold=True,
        hold_until__isnull=False,
        hold_until__lte=today
    )
    released_count = 0
    for inv in expired_inventories:
        old_reason = inv.hold_reason or ''
        inv.is_hold = False
        inv.hold_quantity = 0
        inv.hold_reason = f'[自动解冻] 原因: {old_reason}'
        inv.hold_until = None
        inv.save(update_fields=['is_hold', 'hold_quantity', 'hold_reason', 'hold_until', 'available_quantity', 'updated_at'])

        # 记录审计日志
        HoldAuditLog.objects.create(
            inventory=inv,
            operation='auto_release',
            quantity=inv.quantity,
            reason=f'Hold到期自动解冻，原冻结截止日: {today}',
            operated_by='system_auto_release'
        )
        released_count += 1
    return released_count