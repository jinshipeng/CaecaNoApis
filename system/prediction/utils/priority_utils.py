"""订单优先级优化工具类"""
from datetime import datetime, timedelta, date
from decimal import Decimal


class OrderPriorityOptimizer:
    """订单优先级优化器"""

    FACTORS = {
        'urgency': {'weight': 0.3, 'name': '紧急度'},
        'customer_level': {'weight': 0.25, 'name': '客户等级'},
        'delivery_date': {'weight': 0.25, 'name': '交付日期'},
        'order_size': {'weight': 0.1, 'name': '订单规模'},
        'material_availability': {'weight': 0.1, 'name': '物料可用性'}
    }

    @staticmethod
    def calculate_urgency_score(order):
        """计算紧急度分数"""
        days_to_delivery = (order.demand_date - date.today()).days

        if days_to_delivery < 0:
            return 100
        elif days_to_delivery <= 3:
            return 90
        elif days_to_delivery <= 7:
            return 70
        elif days_to_delivery <= 14:
            return 50
        elif days_to_delivery <= 30:
            return 30
        else:
            return 10

    @staticmethod
    def calculate_customer_level_score(order):
        """计算客户等级分数"""
        customer_level_map = {'VIP': 100, 'A': 80, 'B': 60, 'C': 40, 'D': 20}
        return customer_level_map.get(getattr(order, 'customer_level', None), 50)

    @staticmethod
    def calculate_delivery_date_score(order):
        """计算交付日期分数（越早交付分数越高）"""
        days_to_delivery = (order.demand_date - date.today()).days

        if days_to_delivery < 0:
            return 100
        elif days_to_delivery <= 7:
            return 90
        elif days_to_delivery <= 14:
            return 70
        elif days_to_delivery <= 30:
            return 50
        elif days_to_delivery <= 60:
            return 30
        else:
            return 10

    @staticmethod
    def calculate_order_size_score(order):
        """计算订单规模分数"""
        try:
            qty = float(order.quantity or 0)
            if qty >= 10000:
                return 100
            elif qty >= 5000:
                return 80
            elif qty >= 1000:
                return 60
            elif qty >= 500:
                return 40
            else:
                return 20
        except Exception:
            return 50

    @staticmethod
    def calculate_material_availability_score(order, inventory_data=None):
        """计算物料可用性分数"""
        if inventory_data is None:
            return 50

        material_id = order.material_id
        available = inventory_data.get(material_id, 0)
        required = float(order.quantity or 0)

        ratio = available / required if required > 0 else 0

        if ratio >= 1.5:
            return 100
        elif ratio >= 1.0:
            return 80
        elif ratio >= 0.5:
            return 50
        elif ratio > 0:
            return 30
        else:
            return 10

    @classmethod
    def calculate_comprehensive_priority(cls, order, inventory_data=None):
        """计算综合优先级分数"""
        scores = {
            'urgency': cls.calculate_urgency_score(order),
            'customer_level': cls.calculate_customer_level_score(order),
            'delivery_date': cls.calculate_delivery_date_score(order),
            'order_size': cls.calculate_order_size_score(order),
            'material_availability': cls.calculate_material_availability_score(order, inventory_data)
        }

        total_score = sum(
            scores[factor] * cls.FACTORS[factor]['weight']
            for factor in cls.FACTORS
        )

        return {
            'total_score': total_score,
            'factor_scores': scores,
            'recommended_priority': cls.score_to_priority(total_score)
        }

    @staticmethod
    def score_to_priority(score):
        """将分数转换为优先级（1-10）"""
        if score >= 90:
            return 1
        elif score >= 80:
            return 2
        elif score >= 70:
            return 3
        elif score >= 60:
            return 4
        elif score >= 50:
            return 5
        elif score >= 40:
            return 6
        elif score >= 30:
            return 7
        elif score >= 20:
            return 8
        elif score >= 10:
            return 9
        else:
            return 10

    @classmethod
    def optimize_all_orders(cls, orders, inventory_data=None):
        """优化所有订单的优先级"""
        results = []

        for order in orders:
            priority_info = cls.calculate_comprehensive_priority(order, inventory_data)
            results.append({
                'order_id': order.id,
                'order_no': order.order_no,
                'current_priority': order.priority,
                'recommended_priority': priority_info['recommended_priority'],
                'total_score': priority_info['total_score'],
                'factor_scores': priority_info['factor_scores'],
                'change_needed': order.priority != priority_info['recommended_priority']
            })

        return sorted(results, key=lambda x: x['recommended_priority'])

    @classmethod
    def get_priority_recommendations(cls, orders, inventory_data=None, top_n=10):
        """获取优先级调整建议"""
        optimized = cls.optimize_all_orders(orders, inventory_data)

        change_needed = [o for o in optimized if o['change_needed']]

        return {
            'total_orders': len(orders),
            'recommended_changes': len(change_needed),
            'priority_changes': change_needed[:top_n],
            'factor_descriptions': {k: v['name'] for k, v in cls.FACTORS.items()}
        }


class OrderDeliveryRiskAnalyzer:
    """订单交付风险分析器"""

    @staticmethod
    def analyze_delivery_risk(order, shipping_days=45, production_days=2,
                               preloaded_inventory=None, preloaded_plan_result=None):
        """分析订单交付风险（支持预加载参数避免N+1查询）"""
        from ..models import Inventory, MaterialPlanResult

        days_to_delivery = (order.demand_date - date.today()).days
        lead_time = shipping_days + production_days

        risk_factors = []
        risk_score = 0

        if days_to_delivery < lead_time:
            risk_factors.append({
                'factor': '时间紧迫',
                'description': f'剩余天数({days_to_delivery})少于需求提前期({lead_time})',
                'severity': 'high'
            })
            risk_score += 40

        if order.status == 'partial':
            risk_factors.append({
                'factor': '部分齐套',
                'description': '订单处于部分齐套状态，可能存在缺料',
                'severity': 'high'
            })
            risk_score += 30

        # 使用预加载的库存数据，或回退到单次查询
        if preloaded_inventory is not None:
            inventory = preloaded_inventory
        else:
            inventory = Inventory.objects.filter(material_id=getattr(order, 'material_id', None), is_hold=False).first()

        if inventory and float(inventory.quantity or 0) < float(order.quantity or 0) * 0.5:
            risk_factors.append({
                'factor': '库存不足',
                'description': f'当前库存({inventory.quantity})低于订单需求50%',
                'severity': 'medium'
            })
            risk_score += 20

        # 使用预加载的计划结果，或回退到单次查询
        if preloaded_plan_result is not None:
            plan_result = preloaded_plan_result
        else:
            plan_result = MaterialPlanResult.objects.filter(order_id=order.id).first()

        if plan_result and plan_result.complete_rate < 0.5:
            risk_factors.append({
                'factor': '齐套率低',
                'description': f'当前齐套率仅{plan_result.complete_rate:.0%}',
                'severity': 'medium'
            })
            risk_score += 20

        risk_level = 'low'
        if risk_score >= 70:
            risk_level = 'critical'
        elif risk_score >= 50:
            risk_level = 'high'
        elif risk_score >= 30:
            risk_level = 'medium'

        return {
            'order_id': order.id,
            'order_no': order.order_no,
            'demand_date': order.demand_date.isoformat(),
            'days_to_delivery': days_to_delivery,
            'risk_level': risk_level,
            'risk_score': risk_score,
            'risk_factors': risk_factors,
            'can_deliver_on_time': days_to_delivery >= lead_time and risk_score < 50
        }

    @classmethod
    def analyze_all_orders_risk(cls, orders):
        """分析所有订单的交付风险（优化版：批量查询避免N+1）"""
        from ..models import Inventory, MaterialPlanResult

        # 批量预取库存和计划结果，避免N+1查询
        order_ids = [o.id for o in orders]
        material_ids = list(set(o.material_id for o in orders if hasattr(o, 'material_id') and o.material_id))

        # 一次查询所有相关库存
        inventory_map = {}
        if material_ids:
            for inv in Inventory.objects.filter(material_id__in=material_ids, is_hold=False):
                inventory_map[inv.material_id] = inv

        # 一次查询所有计划结果
        plan_result_map = {}
        if order_ids:
            for pr in MaterialPlanResult.objects.filter(order_id__in=order_ids):
                plan_result_map[pr.order_id] = pr

        results = []
        for order in orders:
            risk_info = cls.analyze_delivery_risk(
                order,
                preloaded_inventory=inventory_map.get(order.material_id) if hasattr(order, 'material_id') else None,
                preloaded_plan_result=plan_result_map.get(order.id)
            )
            results.append(risk_info)

        risk_summary = {
            'total': len(results),
            'critical': sum(1 for r in results if r['risk_level'] == 'critical'),
            'high': sum(1 for r in results if r['risk_level'] == 'high'),
            'medium': sum(1 for r in results if r['risk_level'] == 'medium'),
            'low': sum(1 for r in results if r['risk_level'] == 'low'),
            'at_risk': sum(1 for r in results if not r['can_deliver_on_time'])
        }

        return {
            'summary': risk_summary,
            'orders': sorted(results, key=lambda x: x['risk_score'], reverse=True)
        }
