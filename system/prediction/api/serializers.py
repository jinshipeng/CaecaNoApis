from rest_framework import serializers
from rest_framework.validators import UniqueValidator
from django.db.models import Sum
from ..models import (
    Material, Supplier, Customer, SalesOrder, Inventory, BillOfMaterials,
    SupplierCommitment, MaterialPlanResult, OrderAllocation, PurchaseOrder, Capacity,
    PriorityRule, HoldAuditLog, BOMChangeHistory, WorkCenter, DeliveryChange,
    EngineeringChange, FactoryCalendar, FactoryTransfer, ImportHistory
)


class MaterialSerializer(serializers.ModelSerializer):
    material_code = serializers.CharField(
        validators=[
            UniqueValidator(
                queryset=Material.objects.all(),
                message='该数据已存在：物料代码已存在'
            )
        ]
    )
    actual_stock = serializers.SerializerMethodField()

    class Meta:
        model = Material
        fields = '__all__'

    def get_actual_stock(self, obj):
        result = Inventory.objects.filter(material=obj).aggregate(
            total=Sum('quantity')
        )
        return int(result['total'] or 0)


class MaterialBriefSerializer(serializers.ModelSerializer):
    """物料简要信息（用于嵌套展示，避免N+1查询）"""

    class Meta:
        model = Material
        fields = ['id', 'material_code', 'material_name', 'material_type', 'unit',
                  'standard_cost', 'sales_price', 'safety_stock', 'lead_time',
                  'shelf_life', 'min_order_qty']


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = '__all__'


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = '__all__'


class SalesOrderSerializer(serializers.ModelSerializer):
    material_code = serializers.CharField(source='material.material_code', read_only=True)
    material_name = serializers.CharField(source='material.material_name', read_only=True)
    material_id = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all(), source='material', write_only=True, required=False, allow_null=True)
    total_amount = serializers.SerializerMethodField()
    customer = serializers.SerializerMethodField()

    class Meta:
        model = SalesOrder
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']
    
    def get_total_amount(self, obj):
        return round(float(obj.total_amount or obj.quantity * obj.unit_price), 2)
    
    def get_customer(self, obj):
        return {
            'customer_name': obj.customer_name
        }


class InventorySerializer(serializers.ModelSerializer):
    material_code = serializers.CharField(source='material.material_code', read_only=True)
    material_name = serializers.CharField(source='material.material_name', read_only=True)
    material = MaterialBriefSerializer(read_only=True)
    material_id = serializers.PrimaryKeyRelatedField(
        queryset=Material.objects.all(), source='material', write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = Inventory
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class BillOfMaterialsSerializer(serializers.ModelSerializer):
    parent_code = serializers.SlugRelatedField(
        slug_field='material_code',
        queryset=Material.objects.all(),
        source='parent_material',
    )
    parent_name = serializers.CharField(source='parent_material.material_name', read_only=True)
    child_code = serializers.SlugRelatedField(
        slug_field='material_code',
        queryset=Material.objects.all(),
        source='child_material',
    )
    child_name = serializers.CharField(source='child_material.material_name', read_only=True)

    class Meta:
        model = BillOfMaterials
        fields = ['id', 'parent_code', 'parent_name', 'child_code', 'child_name',
                  'quantity', 'unit', 'bom_level', 'usage_ratio', 'scrap_rate',
                  'alternative_group', 'alternative_priority', 'alternative_ratio',
                  'factory_code', 'ecn_no', 'ecn_date', 'ecn_reason', 'version',
                  'is_configurable', 'config_group', 'config_options', 'is_active',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        parent = attrs.get('parent_material') or getattr(self.instance, 'parent_material', None)
        child = attrs.get('child_material') or getattr(self.instance, 'child_material', None)
        version = attrs.get('version', getattr(self.instance, 'version', 1))

        if parent and child and parent.pk == child.pk:
            raise serializers.ValidationError({'detail': '父物料和子物料不能相同'})

        if parent and child:
            exists = BillOfMaterials.objects.filter(
                parent_material=parent,
                child_material=child,
                version=version or 1
            )
            if self.instance:
                exists = exists.exclude(pk=self.instance.pk)
            if exists.exists():
                raise serializers.ValidationError({
                    'detail': '该数据已存在：相同父物料、子物料和版本的 BOM 已存在'
                })

        return attrs


class SupplierCommitmentSerializer(serializers.ModelSerializer):
    supplier_code = serializers.CharField(source='supplier.supplier_code', read_only=True)
    supplier_name = serializers.CharField(source='supplier.supplier_name', read_only=True)
    material_code = serializers.CharField(source='material.material_code', read_only=True)
    material_name = serializers.CharField(source='material.material_name', read_only=True)

    class Meta:
        model = SupplierCommitment
        fields = '__all__'
        read_only_fields = ['created_at']


class MaterialPlanResultSerializer(serializers.ModelSerializer):
    order_no = serializers.CharField(source='order.order_no', read_only=True)

    class Meta:
        model = MaterialPlanResult
        fields = '__all__'
        read_only_fields = ['created_at']


class OrderAllocationSerializer(serializers.ModelSerializer):
    order_no = serializers.CharField(source='order.order_no', read_only=True)
    material_code = serializers.CharField(source='material.material_code', read_only=True)

    class Meta:
        model = OrderAllocation
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class FailureAnalysisSerializer(serializers.Serializer):
    total_failed = serializers.IntegerField()
    by_reason = serializers.DictField(child=serializers.IntegerField())
    details = serializers.DictField()


class PlanningSummarySerializer(serializers.Serializer):
    total_orders = serializers.IntegerField()
    complete_orders = serializers.IntegerField()
    partial_orders = serializers.IntegerField()
    pending_orders = serializers.IntegerField()
    avg_complete_rate = serializers.FloatField()
    complete_rate = serializers.FloatField()
    total_shortage_orders = serializers.IntegerField()
    total_promise_changes = serializers.IntegerField()
    stable_orders = serializers.IntegerField()
    avg_supplier_reliability = serializers.FloatField()
    total_safety_stock_usage = serializers.IntegerField()
    failure_analysis = FailureAnalysisSerializer()
    total_critical_shortages = serializers.IntegerField(default=0)
    total_urgent_shortages = serializers.IntegerField(default=0)
    jit_optimization = serializers.DictField(default={})
    ai_analysis = serializers.DictField(default=None)
    procurement_plan = serializers.DictField(default=None)
    release_records = serializers.ListField(default=list)
    delivery_violations = serializers.ListField(default=list)
    material_stats = serializers.DictField(required=False, allow_null=True, default=None)


class ShortageReportSerializer(serializers.Serializer):
    order_no = serializers.CharField()
    material_code = serializers.CharField()
    material_name = serializers.CharField()
    required = serializers.FloatField()
    allocated = serializers.FloatField()
    shortage = serializers.FloatField()


class PurchaseOrderSerializer(serializers.ModelSerializer):
    supplier_code = serializers.CharField(source='supplier.supplier_code', read_only=True)
    supplier_name = serializers.CharField(source='supplier.supplier_name', read_only=True)
    material_code = serializers.CharField(source='material.material_code', read_only=True)
    material_name = serializers.CharField(source='material.material_name', read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class CapacitySerializer(serializers.ModelSerializer):
    material_code = serializers.CharField(source='material.material_code', read_only=True)
    material_name = serializers.CharField(source='material.material_name', read_only=True)

    class Meta:
        model = Capacity
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class WorkCenterCapacitySerializer(serializers.ModelSerializer):
    """将 WorkCenter 数据映射为前端 Capacity 页面期望的格式"""
    work_center = serializers.CharField(source='work_center_name')
    daily_capacity = serializers.IntegerField(source='daily_capacity_limit')
    weekly_capacity = serializers.SerializerMethodField()
    material_code = serializers.CharField(source='work_center_code')  # 用产线ID填充物料代码列
    material_name = serializers.CharField(default='')  # WorkCenter无物料名称，留空

    def get_weekly_capacity(self, obj):
        return int(obj.daily_capacity_limit or 0) * 5  # 周产能 ≈ 日产能 × 5天

    class Meta:
        model = WorkCenter
        fields = ['id', 'work_center', 'material_code', 'material_name',
                  'daily_capacity', 'weekly_capacity', 'is_active']


class WorkCenterSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkCenter
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class PriorityRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PriorityRule
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class AuditLogSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source='user.username', default='匿名')
    time = serializers.DateTimeField(source='created_at', format='%Y-%m-%d %H:%M:%S', read_only=True)
    ip = serializers.CharField(source='ip_address', read_only=True, default='')
    status = serializers.SerializerMethodField()
    action_display = serializers.SerializerMethodField()

    ACTION_DISPLAY_MAP = {
        'create': '创建', 'update': '更新', 'delete': '删除',
        'login': '登录', 'logout': '登出', 'export': '导出',
        'import': '导入', 'run': '执行', 'other': '其他',
    }

    def get_status(self, obj):
        return 'success'

    def get_action_display(self, obj):
        return self.ACTION_DISPLAY_MAP.get(obj.action, obj.action)

    class Meta:
        from ..models.notification_models import AuditLog
        model = AuditLog
        fields = ['id', 'user', 'action', 'action_display', 'module', 'target', 'detail',
                  'created_at', 'time', 'ip', 'status']
        read_only_fields = ['id', 'created_at', 'time', 'ip', 'status', 'action_display']


class HoldAuditLogSerializer(serializers.ModelSerializer):
    """Hold操作审计日志序列化器"""
    material_code = serializers.CharField(source='inventory.material.material_code', read_only=True)
    material_name = serializers.CharField(source='inventory.material.material_name', read_only=True)

    class Meta:
        model = HoldAuditLog
        fields = ['id', 'inventory', 'operation', 'quantity', 'reason',
                  'hold_until', 'operated_by', 'created_at', 'material_code', 'material_name']
        read_only_fields = ['id', 'created_at']


class BOMChangeHistorySerializer(serializers.ModelSerializer):
    """BOM变更历史序列化器"""
    parent_material_code = serializers.CharField(source='bom.parent_material.material_code', read_only=True)
    child_material_code = serializers.CharField(source='bom.child_material.material_code', read_only=True)

    class Meta:
        model = BOMChangeHistory
        fields = ['id', 'bom', 'ecn_no', 'from_version', 'to_version', 'change_type',
                  'old_child_material_code', 'old_quantity', 'old_alternative_ratio',
                  'new_child_material_code', 'new_quantity', 'new_alternative_ratio',
                  'reason', 'effective_date', 'created_at',
                  'parent_material_code', 'child_material_code']
        read_only_fields = ['id', 'created_at']


class DeliveryChangeSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryChange
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class EngineeringChangeSerializer(serializers.ModelSerializer):
    class Meta:
        model = EngineeringChange
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class FactoryTransferSerializer(serializers.ModelSerializer):
    class Meta:
        model = FactoryTransfer
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']


class ImportHistorySerializer(serializers.ModelSerializer):
    time = serializers.DateTimeField(format='%Y-%m-%d %H:%M:%S', read_only=True)

    class Meta:
        model = ImportHistory
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']
