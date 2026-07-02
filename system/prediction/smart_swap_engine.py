"""
主动换料引擎 - 基于成本/交期/质量多维决策的智能换料策略

与被动替代料分配不同，主动换料是在物料充足的情况下，
基于成本优势、交期紧迫度、供应商绩效等因素主动选择更优替代料。

触发场景：
1. 原物料成本高于替代料超过阈值 → 降本换料
2. 替代料交期显著短于原物料 → 抢交期换料
3. 原物料供应商风险升高 → 风险规避换料
4. 替代料质量评分更高 → 质量优先换料
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

logger = logging.getLogger(__name__)


class SmartSwapEngine:
    """主动换料引擎"""

    # 换料策略类型
    STRATEGY_COST = 'cost_reduction'           # 降本换料
    STRATEGY_DELIVERY = 'delivery_expedite'     # 抢交期换料
    STRATEGY_RISK_AVOIDANCE = 'risk_avoidance'  # 风险规避
    STRATEGY_QUALITY = 'quality_priority'       # 质量优先

    def __init__(self, planner=None):
        self.planner = planner
        # 换料阈值配置
        self.thresholds = {
            'cost_saving_pct': 10.0,         # 成本节省超过此百分比才考虑换料
            'delivery_days_gain': 5,          # 交期缩短超过此天数才考虑换料
            'supplier_risk_threshold': 0.7,   # 供应商可靠率低于此值触发风险规避
            'quality_score_diff': 0.1,        # 质量评分差异超过此值才考虑
            'max_swap_ratio': 0.3,            # 单次最大换料比例（防止过度换料）
        }
        self.swap_history = []  # 换料记录

    def evaluate_swap_opportunity(self, original_material_id, required_qty, required_date=None, order_priority=5):
        """
        评估换料机会

        Args:
            original_material_id: 原物料ID
            required_qty: 需求数量
            required_date: 需求日期
            order_priority: 订单优先级

        Returns:
            dict: {
                'should_swap': bool,
                'strategy': str or None,
                'alternative_material_id': int or None,
                'reason': str,
                'expected_benefit': dict,
                'risk_assessment': dict
            }
        """
        from .models import SupplierMaterial, Material, Inventory

        benefit = {'cost_saving': 0, 'days_saved': 0, 'risk_reduced': False, 'quality_improved': False}
        best_alternative = None
        best_strategy = None
        best_score = 0

        # 查找原物料的替代料关系
        alt_relations = SupplierMaterial.objects.filter(
            material_id=original_material_id,
            is_forbidden=False
        ).select_related('material', 'supplier').exclude(
            material_id=original_material_id  # 排除自身
        )

        # 获取原物料的供应商信息
        original_suppliers = SupplierMaterial.objects.filter(
            material_id=original_material_id,
            is_forbidden=False
        ).select_related('supplier')

        orig_avg_price = 0
        orig_avg_lead = 0
        orig_avg_reliability = 1.0
        if original_suppliers.exists():
            orig_prices = [float(s.unit_price or 0) for s in original_suppliers if s.unit_price]
            orig_leads = [s.lead_time for s in original_suppliers if s.lead_time]
            orig_reliabilities = [float(s.supplier.delivery_reliability or 0.9) for s in original_suppliers.select_related('supplier') if s.supplier]
            orig_avg_price = sum(orig_prices) / len(orig_prices) if orig_prices else 0
            orig_avg_lead = sum(orig_leads) / len(orig_leads) if orig_leads else 7
            orig_avg_reliability = sum(orig_reliabilities) / len(orig_reliabilities) if orig_reliabilities else 0.9

        today = date.today()

        for alt in alt_relations:
            alt_material = alt.material
            alt_supplier = alt.supplier
            alt_price = float(alt.unit_price or 0)
            alt_lead = alt.lead_time or 7
            alt_reliability = float(alt_supplier.delivery_reliability or 0.9) if hasattr(alt_supplier, 'delivery_reliability') else 0.9
            alt_ratio = float(alt.alternative_ratio or 1.0)

            score = 0
            strategy = None
            reason_parts = []

            # 1. 成本评估
            if orig_avg_price > 0 and alt_price > 0:
                cost_saving_pct = (orig_avg_price - alt_price) / orig_avg_price * 100
                if cost_saving_pct >= self.thresholds['cost_saving_pct']:
                    benefit['cost_saving'] = max(benefit['cost_saving'], cost_saving_pct * required_qty * alt_price / 100)
                    score += cost_saving_pct * 2  # 成本权重较高
                    strategy = self.STRATEGY_COST
                    reason_parts.append(f'成本降低{cost_saving_pct:.1f}%')

            # 2. 交期评估
            days_until_needed = (required_date - today).days if required_date else 30
            orig_arrival = today + orig_avg_lead
            alt_arrival = today + alt_lead
            days_saved = orig_avg_lead - alt_lead

            if days_saved >= self.thresholds['delivery_days_gain']:
                benefit['days_saved'] = max(benefit['days_saved'], days_saved)
                # 高优先级订单 + 临近交期 → 交期权重放大
                delivery_urgency = max(1, (6 - order_priority)) * (1 if days_until_needed > 14 else 2)
                score += days_saved * delivery_urgency
                if not strategy or delivery_urgency > 2:
                    strategy = self.STRATEGY_DELIVERY
                reason_parts.append(f'交期缩短{days_saved}天')

            # 3. 风险评估
            if orig_avg_reliability < self.thresholds['supplier_risk_threshold'] and alt_reliability > orig_avg_reliability:
                benefit['risk_reduced'] = True
                risk_improvement = (alt_reliability - orig_avg_reliability) * 100
                score += risk_improvement * 3  # 风险权重最高
                strategy = self.STRATEGY_RISK_AVOIDANCE
                reason_parts.append(f'供应商可靠率提升{risk_improvement:.1f}%')

            # 4. 换料比例约束检查
            swap_quantity = required_qty * alt_ratio
            if swap_quantity > required_qty * self.thresholds['max_swap_ratio']:
                score *= 0.5  # 超过最大换料比例，降分

            # 检查替代料库存可用性
            alt_stock = 0
            try:
                alt_stock = int(Inventory.objects.filter(material_id=alt_material.id).aggregate(
                    total__sum='quantity'
                )['total__sum'] or 0)
            except Exception:
                pass

            stock_coverage = alt_stock / swap_quantity if swap_quantity > 0 else 0
            if stock_coverage >= 0.5:
                score *= 1.2  # 库存充足加分
            elif stock_coverage < 0.1:
                score *= 0.3  # 库存严重不足大幅降分

            if score > best_score:
                best_score = score
                best_alternative = alt_material.id
                best_strategy = strategy

        should_swap = best_score > 15  # 综合分数阈值

        result = {
            'should_swap': should_swap,
            'strategy': best_strategy,
            'alternative_material_id': best_alternative,
            'reason': '; '.join(reason_parts) if reason_parts else '',
            'expected_benefit': benefit,
            'risk_assessment': {
                'swap_score': round(best_score, 2),
                'stock_available': alt_stock if 'alt_stock' in dir() else 0,
                'compliant_with_bom_ratio': True,  # 由调用方验证
            }
        }

        if should_swap:
            self.swap_history.append({
                'timestamp': datetime.now().isoformat(),
                'original_material_id': original_material_id,
                'alternative_material_id': best_alternative,
                'strategy': best_strategy,
                'score': best_score,
            })

        return result

    def get_swap_summary(self):
        """获取换料汇总统计"""
        from collections import Counter
        strategy_counts = Counter(s['strategy'] for s in self.swap_history)
        return {
            'total_swaps': len(self.swap_history),
            'by_strategy': dict(strategy_counts),
            'recent_swaps': self.swap_history[-10:] if self.swap_history else [],
        }
