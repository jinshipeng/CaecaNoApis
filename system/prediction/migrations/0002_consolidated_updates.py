# -*- coding: utf-8 -*-
"""
合并迁移: 0002-0012 → 单个文件
包含所有增量变更：字段增删改、新模型创建、索引、choices更新
原始文件: 0002~0012 (11个文件 → 合并为1个)
生成时间: 2026-06-14
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("prediction", "0001_initial_consolidated"),
    ]

    operations = [
        # ============================================================
        # [原0002] OrderAllocation + SalesOrder + Material + PlanLog + PriorityRule 字段/choices 变更
        # ============================================================
        migrations.AddField(
            model_name="orderallocation",
            name="is_alternative",
            field=models.BooleanField(default=False, verbose_name="是否替代料"),
        ),
        migrations.AddField(
            model_name="orderallocation",
            name="reliability_factor",
            field=models.FloatField(default=1.0, verbose_name="可靠率因子"),
        ),
        migrations.AddField(
            model_name="orderallocation",
            name="required_quantity",
            field=models.IntegerField(default=0, verbose_name="需求数量"),
        ),
        migrations.AddField(
            model_name="salesorder",
            name="config_options",
            field=models.JSONField(blank=True, null=True, verbose_name="CTO配置选项"),
        ),
        migrations.AlterField(
            model_name="material",
            name="material_type",
            field=models.CharField(
                choices=[("raw", "原材料"), ("semi", "半成品"), ("finished", "成品")],
                db_index=True,
                max_length=20,
                verbose_name="物料类型",
            ),
        ),
        migrations.AlterField(
            model_name="planlog",
            name="log_type",
            field=models.CharField(
                choices=[
                    ("INFO", "信息"),
                    ("WARNING", "警告"),
                    ("ERROR", "错误"),
                    ("PLANNING", "计划"),
                ],
                default="INFO",
                max_length=20,
                verbose_name="日志类型",
            ),
        ),
        migrations.AlterField(
            model_name="priorityrule",
            name="strategy",
            field=models.CharField(
                choices=[
                    ("balanced", "均衡策略"),
                    ("delivery_first", "交付优先"),
                    ("inventory_first", "库存优先"),
                    ("supplier_first", "供应商优先"),
                    ("stability_first", "稳定性优先"),
                    ("cost_first", "成本优先"),
                    ("expiry_first", "临期优先"),
                ],
                default="balanced",
                max_length=20,
                verbose_name="策略类型",
            ),
        ),
        migrations.AlterField(
            model_name="salesorder",
            name="shipping_method",
            field=models.CharField(
                choices=[("sea", "海运"), ("air", "空运")],
                default="sea",
                max_length=20,
                verbose_name="物流方式",
            ),
        ),

        # ============================================================
        # [原0003] Customer 新增4个字段
        # ============================================================
        migrations.AddField(
            model_name="customer",
            name="customer_level",
            field=models.CharField(default="normal", max_length=20, verbose_name="客户等级"),
        ),
        migrations.AddField(
            model_name="customer",
            name="customer_type",
            field=models.CharField(default="其他", max_length=20, verbose_name="客户类型"),
        ),
        migrations.AddField(
            model_name="customer",
            name="delivery_priority",
            field=models.IntegerField(default=5, verbose_name="交付优先级"),
        ),
        migrations.AddField(
            model_name="customer",
            name="payment_terms",
            field=models.CharField(default="月结30天", max_length=50, verbose_name="付款条件"),
        ),

        # ============================================================
        # [原0004] SubstituteMaterial 模型
        # ============================================================
        migrations.CreateModel(
            name="SubstituteMaterial",
            fields=[
                (
                    "id",
                    models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                ("group_id", models.CharField(db_index=True, max_length=50, verbose_name="替代料组ID")),
                ("group_name", models.CharField(max_length=200, verbose_name="替代料组名称")),
                ("priority", models.IntegerField(default=1, verbose_name="替代优先级(1最高)")),
                ("ratio", models.FloatField(default=1.0, verbose_name="替代比例")),
                ("is_default", models.BooleanField(default=False, verbose_name="是否默认")),
                ("remark", models.TextField(blank=True, null=True, verbose_name="备注")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否启用")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "material",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="substitute_groups", to="prediction.material", verbose_name="物料"),
                ),
            ],
            options={
                "verbose_name": "替代物料",
                "verbose_name_plural": "替代物料",
                "db_table": "substitute_material",
                "indexes": [
                    models.Index(fields=["group_id", "priority"], name="idx_sub_group_priority"),
                    models.Index(fields=["material"], name="idx_sub_material"),
                ],
            },
        ),

        # ============================================================
        # [原0005] BOM alternative_group 长度调整
        # ============================================================
        migrations.AlterField(
            model_name="billofmaterials",
            name="alternative_group",
            field=models.CharField(blank=True, max_length=200, null=True, verbose_name="替代料组"),
        ),

        # ============================================================
        # [原0006] PriorityRule权重 + ImportHistory/SalesOrder/SubstituteMaterial choices + 索引
        # ============================================================
        migrations.AddField(
            model_name="priorityrule",
            name="capacity_utilization_weight",
            field=models.FloatField(default=0.0, verbose_name="产能利用率权重"),
        ),
        migrations.AddField(
            model_name="priorityrule",
            name="inventory_status_weight",
            field=models.FloatField(default=0.0, verbose_name="库存状态权重"),
        ),
        migrations.AlterField(
            model_name="importhistory",
            name="import_type",
            field=models.CharField(
                choices=[
                    ("material", "物料数据"),
                    ("supplier", "供应商数据"),
                    ("customer", "客户数据"),
                    ("bom", "BOM数据"),
                    ("inventory", "库存数据"),
                    ("order", "订单数据"),
                    ("purchase", "采购订单数据"),
                    ("workcenter", "工作中心数据"),
                    ("config", "系统配置数据"),
                ],
                db_index=True,
                max_length=20,
                verbose_name="导入类型",
            ),
        ),
        migrations.AlterField(
            model_name="salesorder",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "待处理"),
                    ("confirmed", "已确认"),
                    ("allocated", "已占料"),
                    ("partial", "部分齐套"),
                    ("complete", "完全齐套"),
                    ("shipped", "已发货"),
                    ("delivered", "已交付"),
                    ("cancelled", "已取消"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
                verbose_name="订单状态",
            ),
        ),
        migrations.AlterField(
            model_name="substitutematerial",
            name="id",
            field=models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        ),
        migrations.AlterUniqueTogether(name="substitutematerial", unique_together={("group_id", "material")}),
        migrations.AddIndex(model_name="billofmaterials", index=models.Index(fields=["parent_material_id", "is_active"], name="bom_parent_active_idx")),
        migrations.AddIndex(model_name="billofmaterials", index=models.Index(fields=["child_material_id"], name="bom_child_idx")),
        migrations.AddIndex(model_name="inventory", index=models.Index(fields=["material", "inventory_type"], name="inv_material_type_idx")),
        migrations.AddIndex(model_name="inventory", index=models.Index(fields=["material", "warehouse"], name="inv_material_warehouse_idx")),
        migrations.AddIndex(model_name="inventory", index=models.Index(fields=["factory_code", "material_id"], name="inv_factory_material_idx")),
        migrations.AddIndex(model_name="salesorder", index=models.Index(fields=["status", "priority"], name="order_status_priority_idx")),
        migrations.AddIndex(model_name="salesorder", index=models.Index(fields=["demand_date", "status"], name="order_demand_status_idx")),
        migrations.AddIndex(model_name="salesorder", index=models.Index(fields=["material_id", "status"], name="order_material_status_idx")),
        migrations.AddIndex(model_name="salesorder", index=models.Index(fields=["-created_at"], name="order_created_desc_idx")),
        migrations.AddIndex(model_name="suppliercommitment", index=models.Index(fields=["supplier_id", "delivery_date"], name="commit_supplier_date_idx")),
        migrations.AddIndex(model_name="suppliercommitment", index=models.Index(fields=["material_id", "delivery_date"], name="commit_material_date_idx")),

        # ============================================================
        # [原0007] PurchaseOrder / SalesOrder status choices 更新
        # ============================================================
        migrations.AlterField(
            model_name="purchaseorder",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "草稿"),
                    ("pending", "待处理"),
                    ("issued", "已下达"),
                    ("confirmed", "已确认"),
                    ("in_production", "生产中"),
                    ("partial", "部分到货"),
                    ("partial_shipped", "部分发货"),
                    ("shipped", "已发货"),
                    ("processing", "进行中"),
                    ("completed", "已完成"),
                    ("cancelled", "已取消"),
                ],
                db_index=True,
                default="draft",
                max_length=20,
                verbose_name="订单状态",
            ),
        ),
        migrations.AlterField(
            model_name="salesorder",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "待处理"),
                    ("confirmed", "已确认"),
                    ("in_production", "生产中"),
                    ("allocated", "已占料"),
                    ("partial", "部分齐套"),
                    ("complete", "完全齐套"),
                    ("processing", "进行中"),
                    ("shipped", "已发货"),
                    ("delivered", "已交付"),
                    ("cancelled", "已取消"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
                verbose_name="订单状态",
            ),
        ),

        # ============================================================
        # [原0008] BOMChangeHistory + ConsumptionRule + HoldAuditLog + 多表字段+索引
        # ============================================================
        migrations.CreateModel(
            name="BOMChangeHistory",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ecn_no", models.CharField(max_length=100, verbose_name="ECN编号")),
                ("from_version", models.IntegerField(verbose_name="变更前版本")),
                ("to_version", models.IntegerField(verbose_name="变更后版本")),
                ("change_type", models.CharField(default="modify", help_text="add/modify/delete/replace", max_length=20, verbose_name="变更类型")),
                ("old_child_material_code", models.CharField(blank=True, max_length=50, null=True, verbose_name="变更前子物料代码")),
                ("old_quantity", models.DecimalField(blank=True, decimal_places=4, max_digits=15, null=True, verbose_name="变更前用量")),
                ("old_alternative_ratio", models.FloatField(blank=True, null=True, verbose_name="变更前替代比例")),
                ("new_child_material_code", models.CharField(blank=True, max_length=50, null=True, verbose_name="变更后子物料代码")),
                ("new_quantity", models.DecimalField(blank=True, decimal_places=4, max_digits=15, null=True, verbose_name="变更后用量")),
                ("new_alternative_ratio", models.FloatField(blank=True, null=True, verbose_name="变更后替代比例")),
                ("reason", models.TextField(blank=True, null=True, verbose_name="变更原因")),
                ("effective_date", models.DateField(verbose_name="生效日期")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="记录时间")),
            ],
            options={"verbose_name": "BOM变更历史", "verbose_name_plural": "BOM变更历史", "db_table": "bom_change_history"},
        ),
        migrations.CreateModel(
            name="ConsumptionRule",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True, verbose_name="规则名称")),
                ("rule_type", models.CharField(choices=[("customer_restriction","客户消耗限制"),("order_type_restriction","订单类型限制"),("batch_restriction","批次限制"),("material_product_binding","物料-产品绑定"),("inventory_type_preference","库存类型偏好"),("safety_stock_override","安全库存覆盖"),("priority_boost","优先级加成")], default="customer_restriction", max_length=30, verbose_name="规则类型")),
                ("priority", models.IntegerField(choices=[(1,"最高（必须满足）"),(2,"高（强烈推荐）"),(3,"中（正常执行）"),(4,"低（可选执行）"),(5,"最低（仅作参考）")], default=3, help_text="多条规则冲突时，高优规则优先", verbose_name="规则优先级")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否启用")),
                ("factory_code", models.CharField(blank=True, max_length=50, null=True, verbose_name="工厂代码")),
                ("condition_expression", models.JSONField(blank=True, default=dict, help_text="JSON格式的条件匹配规则", verbose_name="条件表达式")),
                ("action_definition", models.JSONField(blank=True, default=dict, help_text="JSON格式，定义规则命中后的动作", verbose_name="动作定义")),
                ("description", models.TextField(blank=True, default="", verbose_name="规则说明")),
                ("created_by", models.CharField(default="system", max_length=50, verbose_name="创建人")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={"verbose_name": "特殊消耗规则", "verbose_name_plural": "特殊消耗规则", "db_table": "consumption_rule"},
        ),
        migrations.CreateModel(
            name="HoldAuditLog",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation", models.CharField(choices=[("hold","冻结"),("unhold","解冻"),("auto_release","自动解冻")], max_length=20, verbose_name="操作类型")),
                ("quantity", models.IntegerField(default=0, verbose_name="操作数量")),
                ("reason", models.CharField(blank=True, max_length=500, null=True, verbose_name="操作原因")),
                ("hold_until", models.DateField(blank=True, null=True, verbose_name="冻结截止日期")),
                ("operated_by", models.CharField(default="system", max_length=100, verbose_name="操作人")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="操作时间")),
            ],
            options={"verbose_name": "Hold操作审计", "verbose_name_plural": "Hold操作审计", "db_table": "hold_audit_log"},
        ),
        migrations.AddField(model_name="factorycalendar", name="factory_code", field=models.CharField(db_index=True, default="DEFAULT", max_length=50, verbose_name="工厂代码")),
        migrations.AddField(model_name="inventory", name="locked_quantity", field=models.IntegerField(default=0, verbose_name="锁定数量(已分配未出库)")),
        migrations.AddField(model_name="salesorder", name="order_type", field=models.CharField(choices=[("standard","标准订单"),("custom","客制化订单"),("sample","样品订单"),("repair","返修订单"),("framework","框架协议"),("spare","备品备件")], db_index=True, default="standard", help_text="区分标准/客制化/样品/返修/框架协议/备品备件等业务场景", max_length=20, verbose_name="订单类型")),
        migrations.AddField(model_name="substitutematerial", name="purchase_ratio", field=models.FloatField(default=1.0, help_text="采购备料时的比例分配，与用料比例独立。如用料60:40但采购70:30", verbose_name="采购比例")),
        migrations.AlterField(model_name="factorycalendar", name="date", field=models.DateField(verbose_name="日期")),
        migrations.AlterField(model_name="substitutematerial", name="ratio", field=models.FloatField(default=1.0, verbose_name="总体用料比例")),
        migrations.AlterUniqueTogether(name="factorycalendar", unique_together={("factory_code", "date")}),
        migrations.AddField(model_name="bomchangehistory", name="bom", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="change_history", to="prediction.billofmaterials", verbose_name="BOM记录")),
        migrations.AddField(model_name="consumptionrule", name="customer", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="consumption_rules", to="prediction.customer", verbose_name="适用客户")),
        migrations.AddField(model_name="consumptionrule", name="material", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="consumption_rules", to="prediction.material", verbose_name="适用物料")),
        migrations.AddField(model_name="holdauditlog", name="inventory", field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="hold_audit_logs", to="prediction.inventory", verbose_name="库存记录")),
        migrations.AddIndex(model_name="bomchangehistory", index=models.Index(fields=["ecn_no"], name="bch_ecn_idx")),
        migrations.AddIndex(model_name="bomchangehistory", index=models.Index(fields=["bom", "from_version", "to_version"], name="bch_bom_ver_idx")),
        migrations.AddIndex(model_name="consumptionrule", index=models.Index(fields=["rule_type", "is_active"], name="idx_cr_type_active")),
        migrations.AddIndex(model_name="consumptionrule", index=models.Index(fields=["customer", "material", "factory_code"], name="idx_cr_scope")),
        migrations.AddIndex(model_name="consumptionrule", index=models.Index(fields=["priority", "is_active"], name="idx_cr_priority")),
        migrations.AddIndex(model_name="holdauditlog", index=models.Index(fields=["inventory", "operation"], name="hal_inv_op_idx")),
        migrations.AddIndex(model_name="holdauditlog", index=models.Index(fields=["created_at"], name="hal_created_idx")),

        # ============================================================
        # [原0009] EngineeringChange 模型
        # ============================================================
        migrations.CreateModel(
            name="EngineeringChange",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ecn_no", models.CharField(db_index=True, max_length=100, unique=True, verbose_name="ECN编号")),
                ("change_type", models.CharField(default="材料替换", help_text="材料替换/环保合规/用量调整/质量升级/成本优化", max_length=50, verbose_name="变更类型")),
                ("reason", models.TextField(blank=True, null=True, verbose_name="变更原因")),
                ("related_product", models.CharField(blank=True, max_length=50, null=True, verbose_name="关联产品编码")),
                ("ecn_category", models.CharField(blank=True, help_text="如: ECN-材料替换-P0051", max_length=100, null=True, verbose_name="ECN类别名称")),
                ("effective_date", models.DateField(verbose_name="生效日期")),
                ("expiry_date", models.DateField(blank=True, null=True, verbose_name="失效日期")),
                ("status", models.CharField(choices=[("active","启用"),("inactive","停用")], default="active", max_length=20, verbose_name="状态")),
                ("remarks", models.TextField(blank=True, null=True, verbose_name="备注")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("material", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="prediction.material", verbose_name="变更物料")),
            ],
            options={"verbose_name": "工程变更", "verbose_name_plural": "工程变更", "db_table": "engineering_change"},
        ),

        # ============================================================
        # [原0010] Inventory扩展字段 + SalesOrder扩展字段 + DeliveryChange模型
        # ============================================================
        migrations.AddField(model_name="inventory", name="is_restricted", field=models.BooleanField(default=False, verbose_name="是否禁用(供应商禁用料)")),
        migrations.AddField(model_name="inventory", name="max_stock_upper", field=models.IntegerField(default=0, verbose_name="库存上限")),
        migrations.AddField(model_name="inventory", name="restricted_reason", field=models.CharField(blank=True, max_length=500, null=True, verbose_name="禁用原因")),
        migrations.AddField(model_name="inventory", name="safety_stock_lower", field=models.IntegerField(default=0, verbose_name="安全库存下限")),
        migrations.AddField(model_name="inventory", name="target_level", field=models.IntegerField(default=0, verbose_name="目标水位")),
        migrations.AddField(model_name="salesorder", name="actual_delivery_date", field=models.DateField(blank=True, db_index=True, null=True, verbose_name="实际交付日期")),
        migrations.AddField(model_name="salesorder", name="delivery_priority", field=models.IntegerField(default=5, verbose_name="交付优先级顺序")),
        migrations.AddField(model_name="salesorder", name="remarks", field=models.TextField(blank=True, null=True, verbose_name="备注")),
        migrations.CreateModel(
            name="DeliveryChange",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_no", models.CharField(db_index=True, max_length=100, verbose_name="关联订单号")),
                ("po_no", models.CharField(blank=True, db_index=True, max_length=100, null=True, verbose_name="关联采购单号")),
                ("material_code", models.CharField(blank=True, max_length=50, null=True, verbose_name="物料代码")),
                ("supplier_code", models.CharField(blank=True, max_length=50, null=True, verbose_name="供应商代码")),
                ("change_type", models.CharField(choices=[("supplier_delay","供应商延期"),("supplier_advance","供应商提前"),("customer_rush","客户加急"),("customer_postpone","客户延后"),("logistics_issue","物流问题"),("material_shortage","物料短缺")], default="supplier_delay", max_length=30, verbose_name="变更类型")),
                ("original_date", models.DateField(verbose_name="原定交付日期")),
                ("new_date", models.DateField(verbose_name="新交付日期")),
                ("change_days", models.IntegerField(default=0, verbose_name="变更天数(正=延后/负=提前)")),
                ("reason", models.TextField(blank=True, null=True, verbose_name="变更原因")),
                ("change_by", models.CharField(default="system", max_length=50, verbose_name="变更来源")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="变更时间")),
            ],
            options={
                "verbose_name": "交期变更记录",
                "verbose_name_plural": "交期变更记录",
                "db_table": "delivery_change",
                "indexes": [
                    models.Index(fields=["order_no", "created_at"], name="dc_order_time_idx"),
                    models.Index(fields=["supplier_code", "created_at"], name="dc_supplier_time_idx"),
                    models.Index(fields=["material_code"], name="dc_material_idx"),
                ],
            },
        ),

        # ============================================================
        # [原0011] ImportHistory import_type choices 最终版
        # ============================================================
        migrations.AlterField(
            model_name="importhistory",
            name="import_type",
            field=models.CharField(
                choices=[
                    ("material", "物料数据"),
                    ("supplier", "供应商数据"),
                    ("customer", "客户数据"),
                    ("bom", "BOM数据"),
                    ("inventory", "库存数据"),
                    ("order", "订单数据"),
                    ("purchase", "采购订单数据"),
                    ("workcenter", "工作中心数据"),
                    ("config", "系统配置数据"),
                    ("delivery_change", "交期变更记录"),
                    ("factory_calendar_transfer", "工厂日历调拨"),
                    ("config_rules_ecn", "规则与工程变更"),
                ],
                db_index=True,
                max_length=30,
                verbose_name="导入类型",
            ),
        ),

        # ============================================================
        # [原0012] Supplier 新增6个业务字段
        # ============================================================
        migrations.AddField(
            model_name="supplier",
            name="payment_terms",
            field=models.CharField(blank=True, default="月结30天", max_length=50, null=True, verbose_name="结算方式"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="min_order_qty",
            field=models.IntegerField(blank=True, default=100, null=True, verbose_name="最小起订量(件)"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="capacity_level",
            field=models.CharField(blank=True, default="B", max_length=20, null=True, verbose_name="产能等级"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="cooperation_years",
            field=models.IntegerField(blank=True, default=3, null=True, verbose_name="合作年限(年)"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="warranty_months",
            field=models.IntegerField(blank=True, default=12, null=True, verbose_name="质保期(月)"),
        ),
        migrations.AddField(
            model_name="supplier",
            name="on_time_delivery_rate",
            field=models.FloatField(blank=True, default=0.95, null=True, verbose_name="准时交付率"),
        ),
    ]
