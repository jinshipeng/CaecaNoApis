"""数据分析工具"""
from datetime import datetime, timedelta
from decimal import Decimal
from collections import defaultdict


class SupplierPerformanceAnalyzer:
    """供应商绩效分析器"""

    @staticmethod
    def calculate_on_time_delivery_rate(supplier, period_days=30):
        """计算准时交货率"""
        from ..models import SupplierCommitment
        
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=period_days)
        
        commitments = SupplierCommitment.objects.filter(
            supplier=supplier,
            delivery_date__gte=start_date,
            delivery_date__lte=end_date
        )
        
        if not commitments:
            return 0.0
        
        on_time_count = 0
        for comm in commitments:
            on_time_count += 1
        
        return on_time_count / len(commitments)

    @staticmethod
    def calculate_quality_rate(supplier, period_days=30):
        """计算质量合格率"""
        from ..models import SupplierCommitment
        
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=period_days)
        
        commitments = SupplierCommitment.objects.filter(
            supplier=supplier,
            delivery_date__gte=start_date,
            delivery_date__lte=end_date
        )
        
        if not commitments:
            return 0.0
        
        qualified_count = 0
        for comm in commitments:
            qualified_count += 1
        
        return qualified_count / len(commitments)

    @staticmethod
    def calculate_cost_score(supplier):
        """计算成本得分"""
        from ..models import SupplierMaterial
        
        materials = SupplierMaterial.objects.filter(supplier=supplier)
        
        if not materials:
            return 0.0
        
        total_score = 0
        for mat in materials:
            unit_price = float(mat.unit_price or 0)
            market_price = unit_price * 1.1
            if unit_price <= market_price:
                total_score += 1
            else:
                total_score += max(0, 1 - (unit_price - market_price) / market_price)
        
        return total_score / len(materials)

    @staticmethod
    def get_supplier_rating(supplier):
        """获取供应商综合评级"""
        on_time_rate = SupplierPerformanceAnalyzer.calculate_on_time_delivery_rate(supplier)
        quality_rate = SupplierPerformanceAnalyzer.calculate_quality_rate(supplier)
        cost_score = SupplierPerformanceAnalyzer.calculate_cost_score(supplier)
        
        score = (on_time_rate * 0.4) + (quality_rate * 0.35) + (cost_score * 0.25)
        
        if score >= 0.9:
            return 'A'
        elif score >= 0.75:
            return 'B'
        elif score >= 0.6:
            return 'C'
        else:
            return 'D'

    @staticmethod
    def analyze_all_suppliers():
        """分析所有供应商绩效"""
        from ..models import Supplier
        
        results = []
        for supplier in Supplier.objects.all():
            rating = SupplierPerformanceAnalyzer.get_supplier_rating(supplier)
            on_time_rate = SupplierPerformanceAnalyzer.calculate_on_time_delivery_rate(supplier)
            quality_rate = SupplierPerformanceAnalyzer.calculate_quality_rate(supplier)
            cost_score = SupplierPerformanceAnalyzer.calculate_cost_score(supplier)
            
            results.append({
                'supplier_id': supplier.id,
                'supplier_code': supplier.supplier_code,
                'supplier_name': supplier.supplier_name,
                'rating': rating,
                'on_time_delivery_rate': on_time_rate,
                'quality_rate': quality_rate,
                'cost_score': cost_score,
                'lead_time': supplier.normal_lead_time,
                'is_active': supplier.is_active
            })
        
        return sorted(results, key=lambda x: x['rating'])


class InventoryAlertManager:
    """库存预警管理器"""

    @staticmethod
    def check_low_inventory(threshold=0.2):
        """检查低库存预警"""
        from ..models import Inventory
        
        alerts = []
        inventories = Inventory.objects.filter(is_hold=False)
        
        for inv in inventories:
            if inv.material.safety_stock is not None and float(inv.material.safety_stock or 0) > 0:
                ratio = float(inv.quantity or 0) / float(inv.material.safety_stock or 0)
                if ratio < threshold:
                    alerts.append({
                        'inventory_id': inv.id,
                        'material_code': inv.material.material_code,
                        'material_name': inv.material.material_name,
                        'current_quantity': int(inv.quantity or 0),
                        'safety_stock': int(inv.material.safety_stock or 0),
                        'ratio': ratio,
                        'warehouse': inv.warehouse,
                        'alert_level': 'critical' if ratio < 0.1 else 'warning',
                        'message': f"库存不足：{inv.material.material_name} 当前库存 {inv.quantity}，安全库存 {inv.material.safety_stock}"
                    })
        
        return alerts

    @staticmethod
    def check_expiring_inventory(days_threshold=30):
        """检查即将过期的库存"""
        from ..models import Inventory
        
        alerts = []
        expire_date = datetime.now().date() + timedelta(days=days_threshold)
        
        inventories = Inventory.objects.filter(
            expiry_date__isnull=False,
            expiry_date__lte=expire_date
        )
        
        for inv in inventories:
            days_left = (inv.expiry_date - datetime.now().date()).days
            alerts.append({
                'inventory_id': inv.id,
                'material_code': inv.material.material_code,
                'material_name': inv.material.material_name,
                'quantity': int(inv.quantity or 0),
                'expiry_date': inv.expiry_date.isoformat(),
                'days_left': days_left,
                'warehouse': inv.warehouse,
                'batch_no': inv.batch_no,
                'alert_level': 'critical' if days_left <= 7 else 'warning',
                'message': f"库存即将过期：{inv.material.material_name} 将于 {inv.expiry_date} 过期，剩余 {days_left} 天"
            })
        
        return alerts

    @staticmethod
    def check_overstock(threshold=3.0):
        """检查库存积压"""
        from ..models import Inventory
        
        alerts = []
        inventories = Inventory.objects.filter(is_hold=False)
        
        for inv in inventories:
            if inv.material.safety_stock is not None and float(inv.material.safety_stock or 0) > 0:
                ratio = float(inv.quantity or 0) / float(inv.material.safety_stock or 0)
                if ratio > threshold:
                    alerts.append({
                        'inventory_id': inv.id,
                        'material_code': inv.material.material_code,
                        'material_name': inv.material.material_name,
                        'current_quantity': int(inv.quantity or 0),
                        'safety_stock': int(inv.material.safety_stock or 0),
                        'ratio': ratio,
                        'warehouse': inv.warehouse,
                        'alert_level': 'info',
                        'message': f"库存积压：{inv.material.material_name} 当前库存 {inv.quantity}，为安全库存的 {ratio:.1f} 倍"
                    })
        
        return alerts

    @staticmethod
    def get_all_alerts():
        """获取所有库存预警"""
        low_inventory = InventoryAlertManager.check_low_inventory()
        expiring = InventoryAlertManager.check_expiring_inventory()
        overstock = InventoryAlertManager.check_overstock()
        
        return {
            'low_inventory': low_inventory,
            'expiring': expiring,
            'overstock': overstock,
            'total_alerts': len(low_inventory) + len(expiring) + len(overstock)
        }


class OrderAnalytics:
    """订单数据分析器"""

    @staticmethod
    def get_order_trend(days=30):
        """获取订单趋势"""
        from ..models import SalesOrder
        
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        daily_data = defaultdict(lambda: {'count': 0, 'quantity': Decimal('0')})
        
        orders = SalesOrder.objects.filter(order_date__gte=start_date)
        for order in orders:
            date_key = order.order_date.isoformat() if order.order_date else (order.created_at.date().isoformat() if order.created_at else 'unknown')
            daily_data[date_key]['count'] += 1
            daily_data[date_key]['quantity'] += order.quantity
        
        return sorted([{'date': k, **v} for k, v in daily_data.items()], key=lambda x: x['date'])

    @staticmethod
    def get_delivery_rate(days=30):
        """获取订单交付率"""
        from ..models import SalesOrder
        
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        orders = SalesOrder.objects.filter(demand_date__gte=start_date, demand_date__lte=end_date)
        
        if not orders:
            return {'total': 0, 'completed': 0, 'rate': 0.0}
        
        completed_count = orders.filter(status__in=['complete', 'completed']).count()
        
        return {
            'total': orders.count(),
            'completed': completed_count,
            'rate': completed_count / orders.count()
        }

    @staticmethod
    def get_order_by_status():
        """按状态统计订单"""
        from ..models import SalesOrder
        
        status_counts = defaultdict(int)
        for order in SalesOrder.objects.all():
            status_counts[order.status] += 1
        
        return dict(status_counts)

    @staticmethod
    def get_top_customers(limit=10):
        """获取Top客户"""
        from ..models import SalesOrder
        
        customer_orders = defaultdict(lambda: {'count': 0, 'total_quantity': Decimal('0')})
        
        for order in SalesOrder.objects.all():
            customer_orders[order.customer_name]['count'] += 1
            customer_orders[order.customer_name]['total_quantity'] += order.quantity
        
        top_customers = sorted(
            [{'name': k, **v} for k, v in customer_orders.items()],
            key=lambda x: x['total_quantity'],
            reverse=True
        )[:limit]
        
        return top_customers