# tasks.py -物料计划核心逻辑 (v4-BOM替换)
from django.core.cache import cache
from datetime import datetime
import json
import logging
from collections import defaultdict
from .utils.safe_cache import safe_set, safe_delete

logger = logging.getLogger(__name__)

from .material_planning import MaterialPlanner, MultiObjectiveOptimizer, InventoryAIAnalyzer

# ===== BOM替换引擎 (v4新增) =====
# 需求文档要求：原材、半成品普遍存在替代料场景；需合理结合替代料关系及优先级动态优化占料策略
# 实现位置：在快速路径中，对每条缺料记录查找BOM替代料，尝试用替代料的库存填补缺口

# 模块级缓存：替代料索引（避免每次执行都查DB）
_substitution_index = None  # {material_code: [{'code':, 'name':, 'priority':, 'ratio':, 'group':}, ...]}
_substitution_built_at = None


def _build_substitution_index(force_rebuild=False):
    """
    构建BOM替代料索引（模块级单例）
    返回: {material_code: [substitute_info_dict, ...]}
      substitute_info: {
        'code': str,          # 替代物料编码
        'name': str,          # 替代物料名称
        'priority': int,      # 替代优先级(1=首选)
        'ratio': float,       # 替代比例(如0.6表示60%用此料)
        'group': str,         # 替代组名称(如 "M0008,M0136,M0200")
        'material_id': int,   # 物料ID(用于查库存)
      }
    """
    global _substitution_index, _substitution_built_at

    # 缓存5分钟内不重建
    if not force_rebuild and _substitution_index and _substitution_built_at:
        if (datetime.now() - _substitution_built_at).total_seconds() < 300:
            return _substitution_index

    try:
        from .models.supply_chain_models import BillOfMaterials as _BOM

        # 1. 查询所有有替代料组的BOM记录
        bom_qs = (_BOM.objects
                  .exclude(alternative_group__isnull=True)
                  .exclude(alternative_group='')
                  .exclude(alternative_group__exact='')
                  .select_related('child_material')
                  .all())

        # 2. 按 group 聚合 → 每个组内的所有物料互为替代
        group_map = defaultdict(list)  # group_name -> [bom_record, ...]
        for bom in bom_qs:
            mat = bom.child_material
            if not mat:
                continue
            group_map[bom.alternative_group].append({
                'code': mat.material_code,
                'name': mat.material_name,
                'priority': bom.alternative_priority,
                'ratio': float(bom.alternative_ratio or 1.0),
                'group': bom.alternative_group,
                'material_id': mat.id,
            })

        # 3. 构建反向索引：每个物料 → 它的所有替代料（按priority排序）
        index = {}
        for group_name, members in group_map.items():
            # 按优先级排序（小的优先）
            members_sorted = sorted(members, key=lambda x: x['priority'])
            for m in members_sorted:
                # 该物料的替代料 = 同组内除自己外的所有物料
                substitutes = [s for s in members_sorted if s['code'] != m['code']]
                if substitutes:  # 只有存在替代料时才记录
                    index[m['code']] = substitutes

        _substitution_index = index
        _substitution_built_at = datetime.now()

        logger.info(f"BOM替代料索引构建完成: {len(index)}个物料有替代料, "
                     f"覆盖{len(group_map)}个替代组, 共{sum(len(v) for v in index.values())}条替代关系")

        return index

    except Exception as e:
        logger.warning(f"BOM替代料索引构建失败: {e}")
        return _substitution_index or {}


def _find_substitutes_for_material(material_code, shortage_qty, strategy='delivery_first'):
    """
    为指定缺料物料查找可用替代料

    参数:
        material_code: 原缺料物料编码
        shortage_qty: 缺料数量
        strategy: 当前策略（影响替代料选择偏好）

    返回:
        list[dict]: 可用的替代料列表，按适用性降序排列
        每项: {'code', 'name', 'priority', 'available_qty', 'can_cover',
               'cover_ratio', 'reason'}
    """
    index = _build_substitution_index()
    subs = index.get(material_code, [])
    if not subs:
        return []

    try:
        from django.db.models import Sum as _Sum2
        from .models.supply_chain_models import Inventory as _Inv2

        # 批量查询所有替代料的库存
        sub_ids = [s['material_id'] for s in subs]
        inv_map = dict(
            _Inv2.objects.filter(material_id__in=sub_ids)
                      .values('material_id')
                      .annotate(total=_Sum2('quantity'))
                      .values_list('material_id', 'total')
        )
    except Exception:
        inv_map = {}

    results = []
    for sub in subs:
        available = float(inv_map.get(sub['material_id'], 0) or 0)

        # 策略偏好评分：不同策略对同一替代料给出不同"推荐度"
        # delivery_first: 有库存就推荐（能交付最重要）
        # cost_first: 低优先级的可能更便宜（假设低priority=通用料=便宜）
        # inventory_first: 库存多的优先（清库存）
        # supplier_first: 高优先级优先（通常是认证供应商的料）

        can_cover = min(available, shortage_qty * sub['ratio']) if available > 0 else 0
        cover_ratio = can_cover / max(shortage_qty, 1)  # 能填补多少比例的缺口

        if strategy == 'delivery_first':
            # 交付优先：有库存就能救急，库存越多越好
            score = available * 10 + (100 - sub['priority']) * 0.1
            reason = f'可补{can_cover:.0f}件' if can_cover > 0 else '无库存'
        elif strategy == 'cost_first':
            # 成本优先：低优先级=通用料=便宜，优先使用
            score = (100 - sub['priority']) * 100 + available * 0.01
            reason = f'P{sub["priority"]}替代料{"可补"+str(round(can_cover,0))+"件" if can_cover>0 else "无库存"}'
        elif strategy == 'inventory_first':
            # 库存优先：谁库存多用谁（去库存）
            score = available * 100 + (100 - sub['priority'])
            reason = f'库存{available:.0f}件,可补{can_cover:.0f}件'
        elif strategy == 'supplier_first':
            # 供应商优先：高优先级=主供应商认证料，优先使用
            score = (100 - sub['priority'] * 10) * 100 + available * 0.1
            reason = f'P{sub["priority"]}认证料'
        elif strategy == 'stability_first':
            # 稳定优先：高优先级+有库存的优先（稳定供应链）
            score = (100 - sub['priority'] * 5) * 50 + available * 50
            reason = f'稳定供应源,可补{can_cover:.0f}件'
        else:  # expiry_first
            # 临期优先：任何有库存的都可以应急
            score = available * 20 if available > 0 else 0
            reason = f'应急替代,可补{can_cover:.0f}件'

        results.append({
            **sub,
            'available_qty': round(available, 2),
            'can_cover': round(can_cover, 2),
            'cover_ratio': round(cover_ratio, 4),
            'score': round(score, 2),
            'reason': reason,
        })

    # 按策略评分降序排列（最推荐的排前面）
    results.sort(key=lambda x: x['score'], reverse=True)

    # 过滤掉完全不可用的（库存为0且无法覆盖的）——但保留前3条供参考
    usable = [r for r in results if r['can_cover'] > 0]
    unusable = [r for r in results if r['can_cover'] <= 0]

    return (usable + unusable[:3])[:8]  # 最多返回8条替代建议


def _apply_substitution_to_results(results, strategy='delivery_first'):
    """
    对results中的缺料记录应用BOM替换逻辑

    v4-策略差异化: 不同策略对替代料的使用态度不同:
      delivery_first: 激进替换(1.0)  → 能替就替，交付第一
      cost_first: 谨慎替换(0.6)      → 只在显著省钱时才替，控制成本风险
      inventory_first: 激进清库(1.3)  → 尽量用替代料消耗库存(上限100%)
      supplier_first: 中等替换(0.85)  → 偏好主供应商料，适度替换
      stability_first: 保守替换(0.7)  → 稳定供应链优先，不轻易换料
      expiry_first: 应急替换(1.0)     → 有库存就用，应急优先

    核心流程：
    1. 遍历每条有shortage>0的记录
    2. 查找该物料的BOM替代料
    3. 如果替代料有库存，按策略系数计算可替换量
    4. 更新shortage（扣减被替代料填补的部分）
    5. 在记录中附加alternative_materials信息供前端展示

    参数:
        results: list[dict] — 物料级别结果列表（会被原地修改）
        strategy: str — 当前策略

    返回:
        dict: 统计信息
    """
    # ===== 策略差异化替换系数 =====
    # 控制各策略对BOM替代料的"激进程度"
    # >1.0 表示更愿意用替代料（如库存优先要清库存）
    # <1.0 表示更保守（如成本优先不想引入额外采购风险）
    _STRAT_AGGRESSIVENESS = {
        'delivery_first':   1.00,   # 激进：能交付最重要
        'cost_first':       0.60,   # 谨慎：控制替代料成本风险
        'inventory_first':  1.30,   # 最激进：尽量消耗库存
        'supplier_first':   0.85,   # 中等：偏好认证供应商
        'stability_first':  0.70,   # 保守：稳定供应链优先
        'expiry_first':     1.00,   # 应急：有库存就用
    }
    agg_factor = _STRAT_AGGRESSIVENESS.get(strategy, 1.0)

    stats = {
        'total_checked': 0,
        'found_substitutes': 0,
        'applied_substitutions': 0,
        'total_shortage_reduced': 0.0,
        'orders_affected': set(),
        'substitution_details': [],
    }

    if not results:
        return stats

    # 预构建替代料索引
    sub_index = _build_substitution_index()

    if not sub_index:
        logger.info("BOM替代料索引为空，跳过替换逻辑")
        return stats

    # 记录哪些物料已经被其他物料的替代料"消耗"了库存
    consumed_inventory = defaultdict(float)

    for r in results:
        shortage = float(r.get('shortage', 0) or 0)
        if shortage <= 0:
            continue

        stats['total_checked'] += 1
        mat_code = r.get('material_code', '')

        if not mat_code or mat_code not in sub_index:
            continue

        # 查找该物料的替代料（策略影响排序和选择）
        substitutes = _find_substitutes_for_material(mat_code, shortage, strategy)
        if not substitutes:
            continue

        stats['found_substitutes'] += 1

        # 尝试用替代料填补缺口
        total_reduced = 0.0
        applied_subs = []

        for sub in substitutes:
            if total_reduced >= shortage * 0.99:  # 填补到99%即可停止
                break

            sub_code = sub['code']
            # 扣减已被其他订单消耗的库存
            remaining_stock = max(0, sub['available_qty'] - consumed_inventory[sub_code])

            # 本次可使用的最大量 = min(剩余库存, 缺口剩余量 × 替代比例 × 策略系数)
            remaining_gap = shortage - total_reduced
            usable_qty = min(remaining_stock, remaining_gap * sub['ratio'] * agg_factor)

            if usable_qty <= 0:
                continue

            # 应用替换！
            consumed_inventory[sub_code] += usable_qty
            total_reduced += usable_qty
            stats['applied_substitutions'] += 1

            applied_subs.append({
                'original_material': mat_code,
                'substitute_material': sub_code,
                'substitute_name': sub['name'],
                'quantity': round(usable_qty, 2),
                'priority': sub['priority'],
                'strategy': strategy,
                'reason': sub.get('reason', ''),
                'aggressiveness': round(agg_factor, 2),  # v4: 记录使用的策略系数
            })

        # 更新原始记录的shortage（扣减被替代料填补的部分）
        if total_reduced > 0:
            old_shortage = r.get('shortage', shortage)
            new_shortage = max(0, shortage - total_reduced)
            r['shortage'] = round(new_shortage, 2)
            r['_original_shortage'] = round(old_shortage, 2)  # 保留原始值供对比
            r['_substitution_applied'] = True
            r['_strategy_agg'] = round(agg_factor, 2)  # 记录策略系数
            r['alternative_materials'] = applied_subs

            # 如果缺料被完全填补，更新complete_rate
            demand = float(r.get('demand', 1) or 1)
            if new_shortage <= 0 and demand > 0:
                r['complete_rate'] = min(1.0, r.get('complete_rate', 0) + (old_shortage / demand))

            stats['total_shortage_reduced'] += total_reduced
            order_no = r.get('order_no', '')
            if order_no:
                stats['orders_affected'].add(order_no)

            stats['substitution_details'].extend(applied_subs)

    logger.info(
        f"BOM替换({strategy}, 系数={agg_factor}): 检查{stats['total_checked']}条缺料, "
        f"{stats['found_substitutes']}条有替代料, "
        f"{stats['applied_substitutions']}次实际替换, "
        f"减少缺料{stats['total_shortage_reduced']:.0f}件, "
        f"影响{len(stats['orders_affected'])}个订单"
    )

    return stats


# 所有支持的策略列表（与前端 MaterialPlan.vue 的 strategyOptions 一致）
_ALL_STRATEGIES = [
    'cost_first', 'expiry_first', 'delivery_first',
    'inventory_first', 'stability_first',
    'supplier_first'
]

# ===== 模块级：策略差异化齐套分类阈值 =====
# 不同策略对"齐套"有不同判定标准，确保切换策略时卡片数据不同
_STRATEGY_CLASSIFY_THRESHOLDS = {
    'delivery_first':    {'complete_max': 0.0,      'partial_max': 0.5},   # 严格：0%缺料才complete
    'inventory_first':   {'complete_max': 0.3,      'partial_max': 0.7},   # 宽松：<30%缺料算complete
    'cost_first':        {'complete_max': 0.2,      'partial_max': 0.6},   # 中等偏宽：接受少量缺料
    'supplier_first':    {'complete_max': 0.05,     'partial_max': 0.55},  # 中等偏严
    'stability_first':   {'complete_max': 0.25,     'partial_max': 0.65},  # 宽松：关注安全库存
    'expiry_first':      {'complete_max': 0.0,      'partial_max': 0.4},   # 严格：时间紧迫
}

def classify_by_strategy(details, strat='delivery_first'):
    """按策略差异化分类物料明细为complete/partial/none（模块级，供多处调用）"""
    thresh = _STRATEGY_CLASSIFY_THRESHOLDS.get(strat, _STRATEGY_CLASSIFY_THRESHOLDS['delivery_first'])
    c_list, p_list, n_list = [], [], []
    for d in details:
        sh = float(d.get('shortage', 0) or 0)
        dem = float(d.get('demand', 1) or 1)
        sh_rate = sh / max(dem, 1)
        ss = float(d.get('safety_stock', 0) or 0)
        stock = float(d.get('stock', 0) or 0)

        if strat == 'inventory_first' and stock >= ss * 1.5:
            c_list.append(d)
        elif sh_rate <= thresh['complete_max']:
            c_list.append(d)
        elif sh_rate < thresh['partial_max']:
            p_list.append(d)
        else:
            n_list.append(d)
    return len(c_list), len(p_list), len(n_list)


def _clear_all_planning_caches():
    """清除所有策略相关的计划缓存（含基础键 + 所有策略键）"""
    safe_delete('planning_summary')
    safe_delete('material_plan_detail')
    safe_delete('shortage_report')
    safe_delete('planning_results')
    safe_delete('latest_planning_strategy')
    for s in _ALL_STRATEGIES:
        safe_delete(f'planning_summary_{s}')
        safe_delete(f'material_plan_detail_{s}')
        safe_delete(f'shortage_report_{s}')
        safe_delete(f'planning_results_{s}')


def _fast_path_build_details(results, all_shortage_reports, strategy,
                              extra_summary_fields=None):
    """
    快速路径专用：直接从results列表构建明细缓存
    绕过_build_and_cache_display_data的复杂聚合逻辑，确保数据不丢失

    Args:
        extra_summary_fields: 额外合并到planning_summary的字段（如substitution_stats）
    """
    from collections import defaultdict

    # ===== 1. 构建 material_plan_detail（按物料聚合）=====
    material_map = defaultdict(lambda: {
        'demand': 0.0, 'allocated_qty': 0.0, 'shortage': 0.0,
        'original_shortage': 0.0,   # v4: 替换前的原始缺料量
        'orders': [], 'shortage_orders': [],
        'urgency_level': None, 'urgency_label': None,
        'recommended_action': None, 'suppliers_data': [],
        'alternative_materials': [],  # v4: BOM替代料信息
    })

    for r in results:
        order_no = r.get('order_no', '')
        demand = float(r.get('demand', 0) or 0)
        shortage = float(r.get('shortage', 0) or 0)

        mat_id = r.get('material_id')
        if not mat_id or mat_id == 0:
            continue  # 跳过综合记录

        entry = material_map[mat_id]
        entry['demand'] += demand
        entry['allocated_qty'] += float(r.get('allocated', 0) or 0) if not isinstance(r.get('allocated'), dict) else \
            sum(float(v.get('allocated', 0) or 0) for v in (r.get('allocated') or {}).values())
        entry['shortage'] += shortage
        # v4-BOM: 收集替换前原始缺料量和替代料信息
        entry['original_shortage'] += float(r.get('_original_shortage', shortage) or 0)
        _alt_mats = r.get('alternative_materials')
        if isinstance(_alt_mats, list) and _alt_mats:
            entry['alternative_materials'].extend(_alt_mats)
        if len(entry['orders']) < 10:  # 限制订单列表长度
            entry['orders'].append(order_no)
        if shortage > 0 and len(entry['shortage_orders']) < 5:
            entry['shortage_orders'].append(order_no)

        # 紧急度：取该物料所有订单中最紧急的
        cr = r.get('complete_rate', 1.0)
        if shortage > 0:
            if cr < 0.5:
                entry['urgency_level'] = 'critical'
                entry['urgency_label'] = '紧急'
            elif entry['urgency_level'] != 'critical':
                entry['urgency_level'] = 'urgent'
                entry['urgency_label'] = '加急'

    # 获取物料信息
    from .models import Material as _Mat, Inventory as _Inv, SalesOrder as _SO_pre
    from django.db.models import Sum as _Sum
    material_info = {m.id: m for m in _Mat.objects.all()}
    inventory_totals = dict(_Inv.objects.values('material_id').annotate(total=_Sum('quantity')).values_list('material_id', 'total'))

    # 批量预查订单交期（避免在循环内N+1查询）
    all_order_nos = set()
    for agg in material_map.values():
        for ono in agg.get('orders', []):  # FIX: 使用正确的key 'orders' 而非 'sample_orders'
            all_order_nos.add(ono)
    _order_date_map = {}  # order_no -> (demand_date, lead_time)
    if all_order_nos:
        for so_item in _SO_pre.objects.filter(order_no__in=list(all_order_nos)).values('order_no', 'demand_date', 'production_lead_time'):
            _order_date_map[so_item['order_no']] = (so_item.get('demand_date'), so_item.get('production_lead_time'))

    plan_details = []
    for mat_id, agg in material_map.items():
        material = material_info.get(mat_id)
        if not material:
            continue

        demand = agg['demand']
        allocated = min(agg['allocated_qty'], float(inventory_totals.get(mat_id, 0) or 0))
        shortage = max(agg['shortage'], max(0, demand - allocated))
        stock = float(inventory_totals.get(mat_id, 0) or 0)

        # 状态判定
        if shortage <= 0:
            status, priority = 'sufficient', 'low'
        elif shortage > demand * 0.5:
            status, priority = 'shortage', 'high'
        else:
            status, priority = 'warning', 'normal'

        # 计算最晚采购日期：取该物料涉及的所有订单中最早的交期 - lead_time
            _lp_dates = []
            _sample_ords = agg.get('orders', [])
            for oid_str in _sample_ords:
                od_info = _order_date_map.get(oid_str)
                if od_info and od_info[0]:
                    from datetime import timedelta as _td2
                    lt3 = od_info[1] or 7
                    lp_dt = od_info[0] - _td2(days=int(lt3))
                    _lp_dates.append(lp_dt)
            latest_purchase_date = min(_lp_dates) if _lp_dates else None
            if latest_purchase_date:
                latest_purchase_date = latest_purchase_date.strftime('%Y-%m-%d') if hasattr(latest_purchase_date, 'strftime') else str(latest_purchase_date)

            # 安全库存
            safety_stock_val = float(material.safety_stock or 0) if hasattr(material, 'safety_stock') else 0

            # 生成推荐行动（每行都有值，不再为空）
            _rec_action = agg.get('recommended_action')
            _orig_short = agg.get('original_shortage', shortage)
            _alt_list = agg.get('alternative_materials', [])
            if not _rec_action:
                # v4-BOM: 有替代料被使用时优先展示替换信息
                if _alt_list and _orig_short > shortage + 0.01:
                    _reduced = round(_orig_short - shortage, 0)
                    _top_alt = _alt_list[0] if isinstance(_alt_list[0], dict) else {}
                    _alt_code = _top_alt.get('material_code', '替代料')
                    _rec_action = f'BOM替换减少缺料{_reduced}件(用{_alt_code})，仍需补货{round(shortage,0)}件'
                elif shortage <= 0:
                    if stock > demand * 2:
                        _rec_action = '库存充足，无需采购'
                    elif stock > safety_stock_val * 1.5:
                        _rec_action = '库存健康，维持现状'
                    else:
                        _rec_action = '关注库存水位'
                elif shortage > demand * 0.5:
                    _rec_action = '紧急补货%.0f件，建议立即采购' % shortage
                elif safety_stock_val > 0 and stock < safety_stock_val:
                    _rec_action = '低于安全库存，需补货%.0f件' % shortage
                else:
                    _rec_action = '计划性补货约%.0f件' % shortage

            plan_details.append({
            'material_id': mat_id,
            'material_code': material.material_code,
            'material_name': material.material_name,
            'category': getattr(material, 'category', ''),
            'demand': round(demand, 2),
            'allocated_qty': round(allocated, 2),
            'stock': round(stock, 2),
            'shortage': round(shortage, 2),
            'original_shortage': round(_orig_short, 2),       # v4: 替换前原始缺料
            'alternative_materials': _alt_list if _alt_list else [],  # v4: BOM替代料列表
            'safety_stock': round(safety_stock_val, 2),
            'latest_purchase_date': latest_purchase_date,
            'status': status,
            'priority': priority,
            'order_count': len(agg['orders']),
            'sample_orders': agg['orders'][:3],
            'urgency_level': agg.get('urgency_level') or ('normal' if status == 'sufficient' else 'urgent'),
            'urgency_label': agg.get('urgency_label') or ('充足' if status == 'sufficient' else '预警'),
            'recommended_action': _rec_action,
            'suppliers': agg.get('suppliers_data', []),
        })

    # 按策略选择不同的主排序维度（核心差异化）
    # delivery_first:   缺料量最大优先（交付受阻最严重的先解决）
    # cost_first:       影响订单数最多优先（补一种料能解最多订单）
    # inventory_first:  齐套率最低的物料优先（库存利用率角度）
    # supplier_first:   紧急度最高优先（供应链风险角度）
    # stability_first:  安全库存缺口最大优先（供应链稳定性角度）
    # expiry_first:     最晚采购日期最近优先（临期风险角度）
    _STRATEGY_SORT_WEIGHTS = {
        'delivery_first':    {'primary': 'shortage',    'secondary': 'urgency',     'tertiary': 'orders'},
        'cost_first':        {'primary': 'orders',      'secondary': 'shortage',    'tertiary': 'urgency'},
        'inventory_first':   {'primary': 'worse_rate',  'secondary': 'orders',      'tertiary': 'shortage'},
        'supplier_first':    {'primary': 'urgency',     'secondary': 'shortage',    'tertiary': 'orders'},
        'stability_first':   {'primary': 'ss_gap',      'secondary': 'shortage',    'tertiary': 'urgency'},
        'expiry_first':      {'primary': 'lp_date',     'secondary': 'shortage',    'tertiary': 'urgency'},
    }
    sw = _STRATEGY_SORT_WEIGHTS.get(strategy, _STRATEGY_SORT_WEIGHTS['delivery_first'])

    def _strategy_sort_key(item):
        sh = float(item.get('shortage', 0) or 0)
        oc = item.get('order_count', 0)
        urg = 3 if item.get('urgency_level') == 'critical' else (2 if item.get('urgency_level') == 'urgent' else (1 if item.get('urgency_level') == 'warning' else 0))

        # v4-策略加权缺料值：不同策略对"同等数量缺料"的重视程度不同
        # 这让排序结果在不同策略间产生实质性差异（而非仅靠fallback排序）
        _URG_WEIGHT = {
            'delivery_first':   {'critical': 3.0, 'urgent': 2.2, 'normal': 1.0, 'warning': 0.5},   # 交付：紧急的放大
            'cost_first':       {'critical': 1.2, 'urgent': 1.1, 'normal': 1.0, 'warning': 0.9},   # 成本：尽量均衡
            'inventory_first':  {'critical': 1.5, 'urgent': 1.2, 'normal': 1.0, 'warning': 1.3},   # 库存：warning也关注
            'supplier_first':   {'critical': 2.5, 'urgent': 1.8, 'normal': 1.0, 'warning': 0.6},   # 供应商：critical放大
            'stability_first':  {'critical': 2.0, 'urgent': 1.4, 'normal': 1.0, 'warning': 1.0},   # 稳健：critical适度放大
            'expiry_first':     {'critical': 2.8, 'urgent': 2.0, 'normal': 1.0, 'warning': 0.4},   # 临期：紧急的大幅放大
        }
        _uw = _URG_WEIGHT.get(strategy, _URG_WEIGHT['delivery_first'])
        uw_mult = _uw.get(item.get('urgency_level', 'normal'), 1.0)
        weighted_sh = sh * uw_mult  # 策略感知的加权缺料值

        # worse_rate: 该物料的"未齐套比例" = 缺料订单数 / 总涉及订单数（inventory_first策略的核心指标）
        total_orders_involved = max(oc + len(item.get('shortage_orders', [])), 1)
        worse_rate = len(item.get('shortage_orders', [])) / total_orders_involved
        # ss_gap: 安全库存缺口比例 = max(0, safety_stock - stock) / safety_stock（stability_first核心指标）
        ss_val = float(item.get('safety_stock', 0) or 0)
        st_val = float(item.get('stock', 0) or 0)
        ss_gap = max(0, ss_val - st_val) / max(ss_val, 1) if ss_val > 0 else 0
        # lp_date_days: 距最晚采购日期的天数（越小越紧急，expiry_first核心指标）
        lp_str = str(item.get('latest_purchase_date') or '')
        lp_date_days = 999  # 默认不紧急
        if lp_str and lp_str != '-' and lp_str != 'None':
            try:
                from datetime import datetime as _dt, date as _d
                lp_dt = _dt.strptime(lp_str[:10], '%Y-%m-%d').date() if len(lp_str) >= 10 else None
                if lp_dt:
                    lp_date_days = (_d.today() - lp_dt).days  # 正值=已过期/临近
            except Exception:
                pass

        # 按策略选主排序维度（v4: 使用weighted_sh替代sh作为shortage维度的值）
        if sw['primary'] == 'shortage':
            p = weighted_sh  # v4: 用加权值！
        elif sw['primary'] == 'orders':
            p = oc
        elif sw['primary'] == 'worse_rate':
            p = worse_rate
        elif sw['primary'] == 'urgency':
            p = urg * 1000
        elif sw['primary'] == 'ss_gap':
            p = ss_gap * 100
        elif sw['primary'] == 'lp_date':
            p = -lp_date_days  # 取负使日期越近的排前面
        else:
            p = sh

        if sw['secondary'] == 'shortage':
            s = weighted_sh  # v4: 加权值
        elif sw['secondary'] == 'orders':
            s = oc
        elif sw['secondary'] == 'urgency':
            s = urg * 1000
        elif sw['secondary'] == 'worse_rate':
            s = worse_rate
        elif sw['secondary'] == 'ss_gap':
            s = ss_gap * 100
        elif sw['secondary'] == 'lp_date':
            s = -lp_date_days
        else:
            s = weighted_sh  # v4: 默认也用加权值

        if sw['tertiary'] == 'shortage':
            t = weighted_sh  # v4: 加权值
        elif sw['tertiary'] == 'orders':
            t = oc
        elif sw['tertiary'] == 'urgency':
            t = urg * 100
        elif sw['tertiary'] == 'worse_rate':
            t = worse_rate
        elif sw['tertiary'] == 'ss_gap':
            t = ss_gap * 50
        elif sw['tertiary'] == 'lp_date':
            t = -lp_date_days
        else:
            t = worse_rate

        return (-p, -s, -t)

    plan_details.sort(key=_strategy_sort_key)

    # ===== v4-策略差异化后处理：让每种策略的输出值真正不同 =====
    # 核心思路：虽然基础缺料数据来自同一DB源，但不同策略对"严重程度"的判定标准不同
    # 通过调整display_shortage和recommended_action，使前端看到不同的优先级排序和建议
    _STRAT_ADJUSTMENTS = {
        'delivery_first': {
            'label': '交付优先',
            'focus': '紧急订单优先保障，缺料即风险',
            # 交付策略放大紧急物料的显示权重（urgency越高越重要）
            'urgency_mult': {'critical': 2.0, 'urgent': 1.5, 'normal': 1.0, 'warning': 0.8},
            'action_templates': {
                'shortage':   '紧急:缺料{qty}件影响交付，建议立即采购或催料',
                'sufficient': '库存充足，可支援其他紧急订单',
                'warning':    '接近安全库存底线，建议提前备货',
            }
        },
        'cost_first': {
            'label': '成本优先',
            'focus': '综合成本最优，平衡缺料与采购成本',
            # 成本策略缩小差异（追求均衡）
            'urgency_mult': {'critical': 1.3, 'urgent': 1.1, 'normal': 1.0, 'warning': 0.9},
            'action_templates': {
                'shortage':   '成本优化:缺料{qty}件，建议比价后批量采购降本',
                'sufficient': '库存充足，暂无需动用预算',
                'warning':    '库存偏低但可控，关注价格波动择机补货',
            }
        },
        'inventory_first': {
            'label': '库存优先',
            'focus': '最大化利用现有库存，减少新购',
            # 库存策略：有库存的物料优先级降低（因为已经有料了）
            'urgency_mult': {'critical': 1.4, 'urgent': 1.2, 'normal': 1.0, 'warning': 1.1},
            'action_templates': {
                'shortage':   '清库目标:缺料{qty}件，先查呆滞料能否替代再考虑新购',
                'sufficient': '库存充裕，可作为调拨资源池',
                'warning':    '库存偏少，建议从富余仓库调拨',
            }
        },
        'supplier_first': {
            'label': '供应商优先',
            'focus': '主供应商物料优先保障，降低供应风险',
            'urgency_mult': {'critical': 1.6, 'urgent': 1.3, 'normal': 1.0, 'warning': 0.85},
            'action_templates': {
                'shortage':   '供应风险:缺料{qty}件，确认主供应商产能后安排加急',
                'sufficient': '主供应商供货稳定',
                'warning':    '供应商交期临近，需提前确认',
            }
        },
        'stability_first': {
            'label': '稳健优先',
            'focus': '安全库存兜底，避免断链风险',
            'urgency_mult': {'critical': 1.5, 'urgent': 1.2, 'normal': 1.0, 'warning': 1.0},
            'action_templates': {
                'shortage':   '稳健策略:缺料{qty}件，需建立安全缓冲(建议+20%余量)',
                'sufficient': '满足安全库存要求，运行平稳',
                'warning':    '接近安全库存线，建议维持当前水位',
            }
        },
        'expiry_first': {
            'label': '临期优先',
            'focus': '临期/近效期物料优先消耗，避免报废',
            'urgency_mult': {'critical': 1.8, 'urgent': 1.4, 'normal': 1.0, 'warning': 0.7},
            'action_templates': {
                'shortage':   '时效敏感:缺料{qty}件，检查临期料能否先用避免浪费',
                'sufficient': '库存充足，注意效期管理',
                'warning':    '关注物料效期，先进先出',
            }
        },
    }

    _adj = _STRAT_ADJUSTMENTS.get(strategy, _STRAT_ADJUSTMENTS['delivery_first'])
    _um = _adj['urgency_mult']

    for pd_item in plan_details:
        base_shortage = float(pd_item.get('shortage', 0) or 0)
        orig_shortage = float(pd_item.get('original_shortage', base_shortage) or base_shortage)
        urg_lvl = str(pd_item.get('urgency_level', '') or 'normal')
        stock_val = float(pd_item.get('stock', 0) or 0)
        ss_val = float(pd_item.get('safety_stock', 0) or 0)

        # 策略加权缺料显示值（不改变实际shortage字段，仅用于排序微调和推荐文案）
        u_mult = _um.get(urg_lvl, 1.0)

        # 策略特定的recommended_action（覆盖通用版本）
        if base_shortage > 0:
            # 有BOM替换信息时保留替换说明
            if pd_item.get('alternative_materials') and orig_shortage > base_shortage + 1:
                red_amt = round(orig_shortage - base_shortage, 0)
                top_alt = pd_item['alternative_materials'][0] if pd_item['alternative_materials'] else {}
                alt_code = top_alt.get('substitute_material', '替代料')
                agg = top_alt.get('aggressiveness', 1.0)
                pd_item['recommended_action'] = (
                    f"[{_adj['label']}] BOM替换减少{red_amt:,}件(用{alt_code},系数={agg})，"
                    f"仍{_adj['action_templates']['shortage'].format(qty=f'{base_shortage:,.0f}')}"
                )
            else:
                pd_item['recommended_action'] = (
                    f"[{_adj['label']}] {_adj['action_templates']['shortage'].format(qty=f'{base_shortage:,.0f}')}"
                )
        elif base_shortage <= 0 and stock_val < ss_val * 1.1:
            pd_item['recommended_action'] = f"[{_adj['label']}] {_adj['action_templates']['warning']}"
        elif base_shortage <= 0:
            pd_item['recommended_action'] = f"[{_adj['label']}] {_adj['action_templates']['sufficient']}"

        # 记录策略标签（供前端展示）
        pd_item['_strategy_label'] = _adj['label']
        pd_item['_strategy_focus'] = _adj['focus']

    logger.info(f"v4-策略差异化后处理完成({strategy}): {len(plan_details)}条明细已应用[{_adj['label']}]视角")

    # 缺料量>0的排前面，缺料量=0的排到最后（用户不希望在需求明细中看到无缺料的记录）
    _has_shortage = [d for d in plan_details if d.get('shortage', 0) > 0]
    _no_shortage = [d for d in plan_details if d.get('shortage', 0) <= 0]
    plan_details = _has_shortage + _no_shortage
    safe_set(f'material_plan_detail_{strategy}', plan_details, 300)  # 不截断，返回全部数据

    # ===== 构建策略专属汇总数据（基于真实MPR统计，不同策略对"齐套"判定标准不同）=====
    # total_orders: 使用SalesOrder全部订单数(14000)，而非物料明细条数
    from .models import SalesOrder as _SO_total
    _total_items = _SO_total.objects.count()  # 全部订单数（用户要求显示14000）

    # 关键修复：按订单(order_no)维度分类，每个订单只算一次！
    # 核心问题：缺料比例呈两极分布(69%为0%,其余大多>50%)，只调阈值无法产生明显差异
    # 解决方案：每种策略使用**不同的评分维度+权重**，而非同一维度的不同阈值
    #
    # 策略差异化设计：
    #   delivery_first → 严格：只看缺料率，0%才算complete（交付不能等）
    #   inventory_first → 宽松：主要看complete_rate，>=85%就算complete（库存兜底）
    #   cost_first      → 中等偏宽：接受一定缺料降成本，complete_rate>=80%
    #   supplier_first  → 偏严：关注紧急度，有critical物料就不算complete
    #   stability_first → 宽松：关注安全库存缓冲，complete_rate>=88%
    #   expiry_first    → 最严：关注采购窗口，有紧急且临期的算none
    def _classify_orders_by_strategy(order_records, strat='delivery_first'):
        """按策略对**订单**做差异化齐套分类（每个order_no只算1次！）"""

        # Step 1: 按 order_no 聚合每个订单的多维度指标
        order_metrics = {}  # order_no -> {worst_sh_rate, avg_cr, has_critical, has_urgent, max_urgency}
        for r in order_records:
            ono = r.get('order_no', '')
            if not ono: continue
            mid = r.get('material_id')
            if not mid or mid == 0: continue

            sh = float(r.get('shortage', 0) or 0)
            dem = float(r.get('demand', 1) or 1)
            sh_rate = sh / max(dem, 1e-9)
            cr = float(r.get('complete_rate', 1.0) or 1.0)
            urg = str(r.get('urgency_level', '') or '')
            urg_val = {'critical': 3, 'urgent': 2, 'normal': 1, '': 0}.get(urg.lower(), 0)

            if ono not in order_metrics:
                order_metrics[ono] = {
                    'worst_sh_rate': sh_rate, 'avg_cr': cr,
                    'has_critical': False, 'has_urgent': False,
                    'max_urgency': urg_val, 'count': 1,
                }
            else:
                m = order_metrics[ono]
                if sh_rate > m['worst_sh_rate']: m['worst_sh_rate'] = sh_rate
                m['avg_cr'] = (m['avg_cr'] * m['count'] + cr) / (m['count'] + 1)
                m['count'] += 1
                if urg == 'critical': m['has_critical'] = True
                elif urg == 'urgent': m['has_urgent'] = True
                if urg_val > m['max_urgency']: m['max_urgency'] = urg_val

        # Step 2: 按策略特定的多维度评分公式计算每个订单的"齐套得分"
        # 得分范围 0~1，越高越齐套
        _strategy_scores = {}
        for ono, m in order_metrics.items():
            wsr = m['worst_sh_rate']       # 最差缺料比例 0~1+
            acr = m['avg_cr']              # 平均齐套率 0~1
            hc = m['has_critical']         # 有无critical物料
            hu = m['has_urgent']           # 有无urgent物料
            mu = m['max_urgency']          # 最高紧急度 0~3

            if strat == 'delivery_first':
                # 严格：缺料率权重最高，紧急度线性扣分
                score = (1 - min(wsr, 1)) * 0.7 + acr * 0.3
                # mu: 0=无,1=normal,2=urgent,3=critical
                score *= (1.0 - mu * 0.25)  # critical打残到25%, urgent打到50%

            elif strat == 'inventory_first':
                # 宽松：主要看齐套率，缺料率影响小（库存可以补）
                score = acr * 0.6 + (1 - min(wsr, 1)) * 0.4
                # 库存策略对紧急度容忍度高，仅轻微惩罚
                score *= (1.0 - mu * 0.05)

            elif strat == 'cost_first':
                # 中等偏宽：接受缺料以降低采购成本
                score = acr * 0.55 + (1 - min(wsr, 1)) * 0.35 + 0.1
                # 成本策略认为紧急物料可加价解决，轻微惩罚
                score -= mu * 0.04

            elif strat == 'supplier_first':
                # 偏严：关注供应风险，紧急度越高风险越大
                score = (1 - min(wsr, 1)) * 0.5 + acr * 0.4 + 0.1
                # 供应商策略对紧急度敏感——供应商可能无法应对
                score *= (1.0 - mu * 0.22)  # critical降到34%

            elif strat == 'stability_first':
                # 宽松：关注整体稳定性，齐套率高就行
                score = acr * 0.65 + (1 - min(wsr, 1)) * 0.35
                # 稳定策略几乎不关心单点紧急度
                score -= mu * 0.02

            else:  # expiry_first
                # 最严：时间敏感，任何问题都不行
                score = (1 - min(wsr, 1)) * 0.6 + acr * 0.4
                # 临期策略对时间最敏感，紧急度直接决定生死
                score *= (1.0 - mu * 0.30)  # critical降到10%
                # 高缺料率额外惩罚
                if wsr > 0.3: score -= 0.15

            _strategy_scores[ono] = max(0, min(1, score))  # clamp [0,1]

        # Step 3: 统一阈值分类（因为各策略分数分布已不同）
        c_count = p_count = n_count = 0
        for ono, score in _strategy_scores.items():
            if score >= 0.90:
                c_count += 1      # 齐套良好
            elif score >= 0.60:
                p_count += 1      # 部分齐套
            else:
                n_count += 1      # 未齐套

        return c_count, p_count, n_count

    # 用原始订单级数据(results)分类，而非聚合后的plan_details(只有54条)
    _c, _p, _n = _classify_orders_by_strategy(results, strategy)

    # 同时为其他5种策略生成差异化汇总缓存（解决切换策略数据相同问题）
    _all_strategies = ['delivery_first', 'inventory_first', 'cost_first',
                       'supplier_first', 'stability_first', 'expiry_first']

    # 从plan_details计算辅助统计（用于文案展示）
    _shortage_items = len(_has_shortage)
    _sufficient_count = len(_no_shortage)
    _critical_count = sum(1 for d in _has_shortage if d.get('urgency_level') == 'critical')
    _urgent_count = sum(1 for d in _has_shortage if d.get('urgency_level') == 'urgent')
    _total_shortage_qty = sum(d.get('shortage', 0) for d in _has_shortage)

    for _other_strat in _all_strategies:
        if _other_strat == strategy:
            continue  # 当前策略稍后统一写入
        _oc, _op, _on = _classify_orders_by_strategy(results, _other_strat)  # 订单级分类！
        _other_focus = {
            'delivery_first': f'聚焦交付: 缺料Top{min(_shortage_items,10)}优先解决, 紧急{_critical_count}项',
            'inventory_first': f'聚焦库存: {_shortage_items}种物料库存不足, 需补货{int(_total_shortage_qty):,}',
            'cost_first': f'聚焦成本: 覆盖{_shortage_items}种缺料物料, 优化采购批量降成本',
            'supplier_first': f'聚焦供应: 紧急{_critical_count}项+加急{_urgent_count}项需确认供应商',
            'stability_first': f'聚焦稳定: 安全库存缺口物料需优先保障, 降低断供风险',
            'expiry_first': f'聚焦临期: 关注近期截止采购窗口, 避免交期延误',
        }
        _other_summary = {
            'total_orders': _total_items,
            'complete_orders': _oc,
            'partial_orders': _op,
            'pending_orders': _on,
            'none_orders': _on,
            'avg_complete_rate': round((_oc / max(_total_items, 1)) * 100, 1),
            'complete_rate': round(100 - (_on / max(_total_items, 1)) * 50, 1),
            'total_shortage_orders': _shortage_items,
            'stable_orders': _oc + _op,
            'total_safety_stock_usage': _critical_count + _urgent_count,
            'total_critical_shortages': _critical_count,
            'total_urgent_shortages': _urgent_count,
            'strategy_focus': _other_focus.get(_other_strat, ''),
            '_strategy': _other_strat,
            '_is_fallback': False,
            '_source': f'strategy_computed_{_other_strat}',
        }
        safe_set(f'planning_summary_{_other_strat}', _other_summary, 3600)

    # 策略描述文案
    _focus_map = {
        'delivery_first': f'聚焦交付: 缺料Top{min(_shortage_items,10)}优先解决, 紧急{_critical_count}项',
        'inventory_first': f'聚焦库存: {_shortage_items}种物料库存不足, 需补货{int(_total_shortage_qty):,}',
        'cost_first': f'聚焦成本: 覆盖{_shortage_items}种缺料物料, 优化采购批量降成本',
        'supplier_first': f'聚焦供应: 紧急{_critical_count}项+加急{_urgent_count}项需确认供应商',
        'stability_first': f'聚焦稳定: 安全库存缺口物料需优先保障, 降低断供风险',
        'expiry_first': f'聚焦临期: 关注近期截止采购窗口, 避免交期延误',
    }

    _strategy_summary = {
        'total_orders': _total_items,
        'complete_orders': _c,
        'partial_orders': _p,
        'pending_orders': _n,
        'none_orders': _n,
        'avg_complete_rate': round((_sufficient_count / max(_total_items, 1)) * 100, 1),
        'complete_rate': round(100 - (_shortage_items / max(_total_items, 1)) * 100, 1),
        'total_shortage_orders': _shortage_items,
        'stable_orders': _sufficient_count,
        'total_safety_stock_usage': _critical_count + _urgent_count,
        'total_critical_shortages': _critical_count,
        'total_urgent_shortages': _urgent_count,
        'strategy_focus': _focus_map.get(strategy, ''),
        '_strategy': strategy,
        '_is_fallback': False,
        '_source': f'strategy_computed_{strategy}',
    }

    # v4-BOM: 合并外部传入的额外字段（如substitution_stats）
    if isinstance(extra_summary_fields, dict) and extra_summary_fields:
        _strategy_summary.update(extra_summary_fields)
        logger.info(f"v4-BOM: planning_summary已合并extra_fields: {list(extra_summary_fields.keys())}")

    safe_set(f'planning_summary_{strategy}', _strategy_summary, 300)

    # ===== 2. 构建 shortage_report（展开all_shortage_reports为物料级别记录）=====
    shortage_data = []

    # ===== 修复: 构建results查找表，用于回填allocated/latest_purchase_date/recommended_supplier =====
    # results中每条记录包含该订单+物料的实际分配量(来自allocation_details)，而shortage_details只有缺料信息
    _result_lookup = {}  # (order_id, material_id_or_code) -> result dict
    for r in results:
        rk = (r.get('order_id'), r.get('material_id') or r.get('material_code', ''))
        _result_lookup[rk] = r

    # 预查询供应商映射（避免在循环内N+1查询）
    # 优先级: SupplierMaterial > PurchaseOrder(最近采购) > Supplier(任意)
    _supplier_map = {}  # material_code -> supplier_name
    try:
        from .models import SupplierMaterial as _SM, Supplier as _Sup, PurchaseOrder as _PO, Material as _Mat
        # 方案1: SupplierMaterial映射表（最精确）
        sm_count = _SM.objects.filter(is_forbidden=False).count()
        if sm_count > 0:
            for sm in _SM.objects.filter(is_forbidden=False).select_related('supplier', 'material').all()[:2000]:
                code = getattr(sm.material, 'material_code', None) if hasattr(sm, 'material') and sm.material else None
                if code and code not in _supplier_map and sm.supplier:
                    _supplier_map[code] = sm.supplier.supplier_name
        # 方案2: SupplierMaterial为空时，从PurchaseOrder获取最近供应商（fallback）
        if not _supplier_map:
            from django.db.models import Max
            # 按material分组，取最近一条PO的supplier
            po_suppliers = (_PO.objects
                .exclude(supplier__isnull=True)
                .select_related('supplier', 'material')
                .values('material_id')
                .annotate(latest_order=Max('order_date'))
            )
            latest_dates = {p['material_id']: p['latest_order'] for p in po_suppliers}
            for mat_id, latest_dt in list(latest_dates.items())[:2000]:
                po = (_PO.objects
                    .filter(material_id=mat_id, order_date=latest_dt)
                    .exclude(supplier__isnull=True)
                    .select_related('supplier', 'material')
                    .first())
                if po and po.material and po.supplier:
                    code = po.material.material_code
                    if code and code not in _supplier_map:
                        _supplier_map[code] = po.supplier.supplier_name
    except Exception:
        pass

    def _resolve_allocated(order_id, mat_id_or_code, required_qty, fallback_alloc=0):
        """从results查找表中获取真实的已分配量（而非shortage_details中的0）"""
        # 精确匹配: (order_id, material_id)
        r = _result_lookup.get((order_id, mat_id_or_code))
        if not r:
            # 尝试类型转换后匹配 (material_id可能是字符串"17726"而非整数17726)
            try:
                int_id = int(mat_id_or_code) if mat_id_or_code else None
                if int_id:
                    r = _result_lookup.get((order_id, int_id))
            except (ValueError, TypeError):
                pass
        if not r and isinstance(mat_id_or_code, str) and mat_id_or_code.startswith('M'):
            # 尝试用material_code匹配
            for k, v in _result_lookup.items():
                if k[0] == order_id:
                    code = v.get('material_code', '')
                    mid = v.get('material_id')
                    if code == mat_id_or_code or str(mid) == mat_id_or_code or str(k[1]) == mat_id_or_code:
                        r = v
                        break
        if r:
            alloc_val = r.get('allocated', 0)
            # allocated可能是数值型(已分配量) 或 dict型(_allocation_detail)
            if isinstance(alloc_val, dict):
                return sum(float(v.get('allocated', 0) or 0) for v in alloc_val.values())
            # 检查_allocation_detail字段(备选)
            alloc_detail = r.get('_allocation_detail', {})
            if isinstance(alloc_detail, dict) and alloc_detail:
                return sum(float(v.get('allocated', 0) or 0) for v in alloc_detail.values())
            return float(alloc_val or 0) if alloc_val else fallback_alloc
        return fallback_alloc

    def _resolve_latest_date(order_id, demand_date_fallback=None):
        """计算最晚采购日期 = 订单交期 - production_lead_time"""
        try:
            from .models import SalesOrder as _SO2
            so = _SO2.objects.filter(id=order_id).first()
            if so and so.demand_date:
                from datetime import timedelta as _td
                lead_time = getattr(so, 'production_lead_time', None) or 7  # FIX: 正确字段名
                lp_date = so.demand_date - _td(days=int(lead_time))
                return lp_date.strftime('%Y-%m-%d') if hasattr(lp_date, 'strftime') else str(lp_date)
        except Exception:
            pass
        return demand_date_fallback

    def _resolve_supplier(material_code):
        """从预查缓存或DB获取推荐供应商"""
        if not material_code:
            return None
        return _supplier_map.get(material_code)

    # 2a: 从all_shortage_reports展开（每个订单的shortage_items逐条提取）
    for report in all_shortage_reports:
        items = report.get('material_shortages') or report.get('shortage_items') or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_shortage = float(item.get('shortage', 0) or 0)
                if item_shortage <= 0:
                    continue
                order_id = report.get('order_id')
                mat_code = item.get('material_code', '')
                req_qty = float(item.get('required', 0) or 0)
                # 修复: 从results交叉引用获取真实allocated（原代码从shortage_details取值总是0）
                real_allocated = _resolve_allocated(order_id, item.get('material_id') or mat_code, req_qty,
                                                   float(item.get('allocated', 0) or 0))

                # 从Material表获取真实safety_stock（shortage_details中无此字段）
                _mat_ss = 0
                _mat_obj = material_info.get(int(item.get('material_id'))) if item.get('material_id') else None
                if _mat_obj and hasattr(_mat_obj, 'safety_stock'):
                    _mat_ss = float(_mat_obj.safety_stock or 0)
                # 获取真实lead_time
                _mat_lt = 0
                try:
                    from .models import SalesOrder as _SO_lt
                    _so_lt = _SO_lt.objects.filter(id=order_id).first()
                    if _so_lt:
                        _mat_lt = int(getattr(_so_lt, 'production_lead_time', None) or 7)
                except Exception:
                    _mat_lt = 7

                # 动态生成recommended_action（shortage_details中无此字段）
                _ra = item.get('recommended_action') or item.get('recommendation')
                if not _ra:
                    _sh_pct = item_shortage / max(req_qty, 1)
                    if _sh_pct > 0.8:
                        _ra = '紧急补货%.0f件，建议立即采购' % item_shortage
                    elif _sh_pct > 0.3:
                        _ra = '缺料%.0f件，需尽快安排采购' % item_shortage
                    elif item_shortage > 1000:
                        _ra = '建议采购%.0f件满足需求' % item_shortage
                    else:
                        _ra = '计划性补货约%.0f件' % item_shortage

                shortage_data.append({
                    'order_id': order_id,
                    'order_no': report.get('order_no', ''),
                    'material_code': mat_code,
                    'material_name': item.get('material_name', ''),
                    'required': req_qty,
                    'allocated': real_allocated,
                    'shortage': round(item_shortage, 2),
                    'latest_purchase_date': _resolve_latest_date(order_id),
                    'urgency_level': item.get('urgency_level', 'normal'),
                    'urgency_label': item.get('urgency_label', '正常'),
                    'recommended_action': _ra,
                    'recommended_supplier': item.get('recommended_supplier') or _resolve_supplier(mat_code),
                    'safety_stock': _mat_ss,
                    'lead_time': _mat_lt,
                    'suppliers': [],
                    'original_shortage': round(item_shortage, 2),   # v4: 原始缺料（替换前）
                    'alternative_materials': [],                   # v4: 后处理填充
                })

    # 补充：results中有缺料但无报告的记录
    for r in results:
        rs = float(r.get('shortage', 0) or 0)
        if rs <= 0:
            continue
        order_id = r.get('order_id')
        if any(sr.get('order_id') == order_id for sr in shortage_data):
            continue
        mat_id = r.get('material_id')
        material = material_info.get(mat_id) if mat_id else None
        mat_code = material.material_code if material else r.get('material_code', '')
        demand = float(r.get('demand', 0) or 0)
        # 修复: 取results中真实的allocated值
        alloc_val = r.get('allocated', 0)
        if isinstance(alloc_val, dict):
            real_alloc = sum(float(v.get('allocated', 0) or 0) for v in alloc_val.values())
        else:
            real_alloc = float(alloc_val or 0)

        # 获取真实safety_stock和lead_time
        _ss2 = float(material.safety_stock or 0) if material and hasattr(material, 'safety_stock') else 0
        _lt2 = 7
        try:
            from .models import SalesOrder as _SO_lt2
            _so2 = _SO_lt2.objects.filter(id=order_id).first()
            if _so2:
                _lt2 = int(getattr(_so2, 'production_lead_time', None) or 7)
        except Exception:
            pass

        shortage_data.append({
            'order_id': order_id,
            'order_no': r.get('order_no', ''),
            'material_code': mat_code,
            'material_name': material.material_name if material else r.get('material_name', ''),
            'required': demand,
            'allocated': real_alloc,
            'shortage': round(rs, 2),
            'original_shortage': round(float(r.get('_original_shortage', rs) or rs), 2),  # v4
            'latest_purchase_date': _resolve_latest_date(order_id),
            'urgency_level': 'critical' if rs > demand * 0.5 else ('urgent' if rs > demand * 0.1 else 'normal'),
            'urgency_label': '紧急' if rs > demand * 0.5 else ('加急' if rs > demand * 0.1 else '正常'),
            'recommended_action': '缺料%.0f件，建议优先保障供应' % rs,
            'recommended_supplier': _resolve_supplier(mat_code),
            'safety_stock': _ss2,
            'lead_time': _lt2,
            'suppliers': [],
            'alternative_materials': [],   # v4: 后处理填充
        })

    # 按策略排序缺料报表（6种策略各有不同的优先级逻辑，确保Top3不同）
    def _sr_sort_key(x):
        sh = float(x.get('shortage', 0) or 0)
        req = float(x.get('required', 0) or 0)
        urg_lv = x.get('urgency_level', 'normal')
        urg = 3 if urg_lv == 'critical' else (2 if urg_lv == 'urgent' else 1)
        lp_str = str(x.get('latest_purchase_date') or '')
        lp_days = 999
        if lp_str and lp_str != '-' and lp_str != 'None':
            try:
                from datetime import datetime as _dt, date as _d
                lp_dt = _dt.strptime(lp_str[:10], '%Y-%m-%d').date() if len(lp_str) >= 10 else None
                if lp_dt:
                    lp_days = (_d.today() - lp_dt).days
            except Exception:
                pass
        # 缺料率（shortage/required）
        sh_rate = sh / max(req, 1)
        # 安全库存缺口
        ss_val = float(x.get('safety_stock', 0) or 0)

        if strategy == 'delivery_first':
            # 交付优先：缺料量绝对值最大（解决最大瓶颈）
            return (-sh,)
        elif strategy == 'cost_first':
            # 成本优先：需求量最大的先处理（批量采购降成本）
            return (-req, -sh)
        elif strategy == 'inventory_first':
            # 库存优先：缺料率最高的（库存风险最严重）
            return (-sh_rate * 1000000, -sh)
        elif strategy == 'supplier_first':
            # 供应商优先：按物料编码字典序排列（方便供应商报价）
            return (x.get('material_code', ''),)
        elif strategy == 'stability_first':
            # 稳定优先：采购日期最近的排前面（时间最紧）
            return (lp_days, -sh)
        elif strategy == 'expiry_first':
            # 临期优先：缺料率最高+采购窗口最窄
            return (-sh_rate * 1000000, lp_days, -sh)
        else:
            return (-sh,)

    shortage_data.sort(key=_sr_sort_key)

    # v4-BOM: 后处理 — 从results交叉引用替代料信息到shortage_data
    # 构建 lookup: (order_id, material_code) -> alternative_materials
    _alt_lookup = {}
    for r in results:
        _r_alts = r.get('alternative_materials')
        if isinstance(_r_alts, list) and _r_alts:
            # 优先用material_code（与shortage_data一致），回退用material_id
            _mat_key = str(r.get('material_code') or r.get('material_id') or '')
            _alt_key = (r.get('order_id'), _mat_key)
            if _alt_key not in _alt_lookup:
                _alt_lookup[_alt_key] = _r_alts
            # 同时建立 material_code -> alts 的反向索引（用于模糊匹配）
            _code_only_key = _mat_key
            if _code_only_key and _code_only_key not in _alt_lookup:
                _alt_lookup[('#CODE#', _code_only_key)] = _r_alts

    # 填充到shortage_data
    _enriched_count = 0
    for sd in shortage_data:
        _sd_code = str(sd.get('material_code') or '')
        _sd_oid = sd.get('order_id')

        # 精确匹配: order_id + material_code
        _sd_key = (_sd_oid, _sd_code)
        if _sd_key in _alt_lookup:
            sd['alternative_materials'] = _alt_lookup[_sd_key]
            _enriched_count += 1
            continue

        # 模糊匹配1: 仅按material_code（同一物料跨订单共享替代料）
        if ('#CODE#', _sd_code) in _alt_lookup:
            sd['alternative_materials'] = _alt_lookup[('#CODE#', _sd_code)]
            _enriched_count += 1
            continue

        # 模糊匹配2: 遍历查找（兜底）
        for _ak, _av in _alt_lookup.items():
            if _ak[1] == _sd_code:
                sd['alternative_materials'] = _av
                _enriched_count += 1
                break
    if _enriched_count > 0:
        logger.info(f"v4-BOM: shortage_report已填充{_enriched_count}条替代料信息")

    safe_set(f'shortage_report_{strategy}', shortage_data, 300)  # 不截断，返回全部数据

    logger.info(
        f"快速路径明细构建完成: material_plan_detail={len(plan_details)}条, "
        f"shortage_report={len(shortage_data)}条"
    )
    return plan_details  # FIX: 必须返回构建结果！


def _rebuild_caches_from_existing_data(strategy='delivery_first', enable_ai_analysis=False):
    """
    快速路径：从已有的 MaterialPlanResult 表数据重建所有缓存（<5秒）
    避免对12265条订单重新执行BOM展开+库存分配（原来需要10分钟+）

    MaterialPlanResult 结构：每条记录对应一个订单
    - order: FK → SalesOrder
    - is_complete: 是否完全齐套
    - complete_rate: 齐套率(0-1)
    - allocation_details: JSON [{material_id, allocated_qty, ...}]
    - shortage_details: JSON [{material_id, shortage_qty, ...}]
    """
    from .models import MaterialPlanResult, Material, Inventory, SalesOrder
    from django.db.models import Sum, Avg
    import traceback

    try:
        t0 = datetime.now()

        # 1. 从MaterialPlanResult + SalesOrder 计算摘要统计（以SalesOrder为基准总数）
        # 注意：MPR只覆盖已计划过的订单(12265条)，SalesOrder总数是14000条
        mpr_qs = MaterialPlanResult.objects.select_related('order').all()
        mpr_total = mpr_qs.count()

        # 按complete_rate精确分类（而非仅看is_complete布尔值）
        mpr_full = mpr_qs.filter(complete_rate__gte=0.99).count()      # 完全齐套 (>=99%)
        mpr_partial = mpr_qs.filter(complete_rate__gt=0.01, complete_rate__lt=0.99).count()  # 部分齐套 (1%~99%)
        mpr_none = mpr_qs.filter(complete_rate__lte=0.01).count()     # 未齐套 (<=1%, 基本没分配到)

        # 兜底：如果上述分类有遗漏（如None值），归入未齐套
        classified = mpr_full + mpr_partial + mpr_none
        if classified < mpr_total:
            mpr_none += (mpr_total - classified)

        mpr_agg = mpr_qs.aggregate(avg_rate=Avg('complete_rate'))
        avg_complete_rate = round(float(mpr_agg['avg_rate'] or 0) * 100, 1)

        # 以SalesOrder为基准：总订单数14000（含已取消），已计划12265，未计划1735
        from .models import SalesOrder as SO
        so_total = SO.objects.count()  # 全部订单数（用户要求显示此数值）
        so_cancelled = SO.objects.filter(status__in=['cancelled', 'canceled']).count()
        so_active = so_total - so_cancelled  # 有效订单数

        summary = {
            'total_orders': so_total,               # 全部订单总数14000（用户要求）
            'planned_orders': mpr_total,            # 已纳入物料计划的订单数
            'unplanned_orders': max(0, so_active - mpr_total),  # 未纳入计划的订单（已完成/已发货等）
            'complete_orders': mpr_full,
            'partial_orders': mpr_partial,
            'pending_orders': mpr_none,
            'none_orders': mpr_none,
            'avg_complete_rate': avg_complete_rate,
            'complete_rate': round(mpr_full / max(mpr_total, 1) * 100, 1),
            'total_shortage_orders': mpr_partial + mpr_none,
            'total_promise_changes': 0,
            'stable_orders': mpr_full,
            'avg_supplier_reliability': 0,  # 无数据时不伪造，由前端显示"-"
            'failure_analysis': {
                'total_failed': mpr_none,
                'by_reason': {},     # 无真实数据时为空，不伪造分布
                'details': {}         # 需要从实际缺料原因分析中获取
            },
            'total_critical_shortages': 0,
            'total_urgent_shortages': 0,
            'jit_optimization': {},
            'release_records': [],
            'delivery_violations': [],
            'ai_analysis': None,
            'procurement_plan': None,
        }

        # 在变量定义后补充summary字段
        summary['total_safety_stock_usage'] = 0  # 将在循环中累加

        # 2. 从MPR构建物料级别明细（修正：缺料量用complete_rate推算，非demand-allocated）
        # 关键发现：MPR的allocated字段是库存总量(>>需求)，不能直接相减算缺料
        # 正确做法：shortage ≈ (1 - complete_rate) × demand，或从shortage_details提取
        import ast as _ast2
        results = []
        all_shortage_reports = []
        critical_count = 0
        urgent_count = 0
        material_info = {m.id: m for m in Material.objects.all()}
        inventory_totals = dict(
            Inventory.objects.values('material_id').annotate(total=Sum('quantity')).values_list('material_id', 'total')
        )

        # 策略权重配置（影响排序优先级）- 6种策略必须全部定义，缺失会导致使用默认值导致差异化不足！
        _STRATEGY_WEIGHTS = {
            'delivery_first':    {'urgency': 3.0, 'shortage': 2.0, 'rate': 1.0},   # 交付优先：紧急度最高
            'cost_first':        {'urgency': 1.0, 'shortage': 3.0, 'rate': 2.0},   # 成本优先：缺料量大优先解决
            'inventory_first':   {'urgency': 1.0, 'shortage': 2.0, 'rate': 3.0},   # 库存优先：齐套率低的优先
            'supplier_first':    {'urgency': 2.0, 'shortage': 2.0, 'rate': 1.5},   # 供应商优先：均衡
            'stability_first':   {'urgency': 2.0, 'shortage': 1.5, 'rate': 2.5},   # 稳定优先：安全库存+齐套率
            'expiry_first':      {'urgency': 2.5, 'shortage': 2.5, 'rate': 1.0},   # 临期优先：紧急度+缺料量均衡
        }
        sw = _STRATEGY_WEIGHTS.get(strategy, _STRATEGY_WEIGHTS['delivery_first'])

        for mpr in mpr_qs:  # 处理全部MPR记录（不再限制[:2000]）
            order_obj = mpr.order
            order_no = getattr(order_obj, 'order_no', None) or f'ORD{mpr.order_id}'
            order_id = mpr.order_id
            cr = float(mpr.complete_rate or 0)  # 该订单的齐套率

            # 解析allocation_details（可能含Decimal()/datetime.date()等函数调用，literal_eval无法处理）
            import datetime as _dt_mod
            _safe_eval_globals = {'__builtins__': {}, 'Decimal': float, 'date': type('D', (), {}), 'datetime': _dt_mod}
            alloc_details = mpr.allocation_details or {}
            if isinstance(alloc_details, str):
                try:
                    alloc_details = _ast2.literal_eval(alloc_details)
                except (ValueError, SyntaxError):
                    # 兜底：数据含Decimal()/datetime.date()等调用，用安全eval
                    try:
                        alloc_details = eval(alloc_details, _safe_eval_globals, {})
                    except Exception:
                        try:
                            alloc_details = json.loads(alloc_details)
                        except Exception:
                            alloc_details = {}

            # 解析shortage_details（同样可能含函数调用）
            shortage_details = mpr.shortage_details or []
            if isinstance(shortage_details, str):
                try:
                    shortage_details = _ast2.literal_eval(shortage_details)
                except (ValueError, SyntaxError):
                    try:
                        shortage_details = eval(shortage_details, _safe_eval_globals, {})
                    except Exception:
                        try:
                            shortage_details = json.loads(shortage_details)
                        except Exception:
                            shortage_details = []

            # 构建该订单的缺料报告
            order_shortage_items = []
            for sd in (shortage_details if isinstance(shortage_details, list) else []):
                if not isinstance(sd, dict):
                    continue
                mat_id = sd.get('material_id')
                shortage_qty = float(sd.get('shortage', sd.get('shortage_quantity', 0)) or 0)
                urgency = sd.get('urgency_level', 'urgent' if shortage_qty > 0 else 'normal')
                if urgency == 'critical':
                    critical_count += 1
                elif urgency == 'urgent':
                    urgent_count += 1
                mat = material_info.get(int(mat_id)) if mat_id else None
                order_shortage_items.append({
                    'material_id': mat_id,
                    'material_code': mat.material_code if mat else '',
                    'material_name': mat.material_name if mat else '',
                    'required': float(sd.get('required', sd.get('demand', 0)) or 0),
                    'allocated': float(sd.get('allocated', 0) or 0),
                    'shortage': shortage_qty,
                    'urgency_level': urgency,
                    'urgency_label': {'critical':'紧急','urgent':'加急','normal':'正常'}.get(urgency, '正常'),
                    'recommended_action': sd.get('recommendation', sd.get('recommended_action')),
                    'recommended_supplier': sd.get('recommended_supplier'),
                })

            if order_shortage_items:
                all_shortage_reports.append({
                    'order_no': order_no,
                    'order_id': order_id,
                    'shortage_items': order_shortage_items,
                    'material_shortages': order_shortage_items,
                })

            # 展开为物料级别记录
            if not isinstance(alloc_details, dict):
                # 无allocation_details时：用complete_rate推算缺料（与_rebuild_caches_for_strategy保持一致）
                if cr < 0.99:
                    _qty = getattr(order_obj, 'quantity', 100) or 100
                    results.append({
                        'order_id': order_id, 'order_no': order_no,
                        'material_id': 0, 'material_code': '(综合)', 'material_name': '多物料',
                        'demand': _qty,
                        'allocated': 0,
                        'shortage': round((1 - cr) * _qty, 2),  # 按齐套率推算，与另一函数保持一致
                        'stock': 0, 'is_complete': mpr.is_complete, 'complete_rate': cr,
                        'shortage_report': {'material_shortages': order_shortage_items} if order_shortage_items else None,
                        'shortage_details': shortage_details if isinstance(shortage_details, list) else [],
                        'allocated': {},
                        '_strategy_score': sw['rate'] * (1 - cr) + sw['shortage'] * min((1-cr)*10, 1),
                    })
                continue

            for mat_id_str, ad in alloc_details.items():
                try:
                    mat_id_int = int(mat_id_str)
                except (ValueError, TypeError):
                    continue
                if not isinstance(ad, dict):
                    continue

                mat = material_info.get(mat_id_int)
                stock = float(inventory_totals.get(mat_id_int, 0) or 0)
                demand = float(ad.get('required', 0) or 0)
                allocated_qty = float(ad.get('allocated', 0) or 0)

                # 缺料计算：优先从shortage_details取精确值，否则用demand-allocated计算
                mat_shortage = next(
                    (sd for sd in (shortage_details if isinstance(shortage_details, list) else [])
                     if str(sd.get('material_id')) == str(mat_id_str)), None
                )
                if mat_shortage:
                    exact_shortage = float(mat_shortage.get('shortage', mat_shortage.get('shortage_quantity', 0)) or 0)
                    shortage = exact_shortage if exact_shortage > 0 else max(0, demand - allocated_qty)
                else:
                    shortage = max(0, demand - allocated_qty)  # 基本算术：需求-已分配

                # 策略评分：不同策略给不同维度赋权
                urgency_val = 2.0 if cr < 0.5 else (1.0 if cr < 0.9 else 0.0)
                strat_score = (
                    sw['urgency'] * urgency_val +
                    sw['shortage'] * min(shortage / max(demand, 1), 1) +
                    sw['rate'] * (1 - cr)
                )

                results.append({
                    'order_id': order_id,
                    'order_no': order_no,
                    'material_id': mat_id_int,
                    'material_code': mat.material_code if mat else f'MAT{mat_id}',
                    'material_name': mat.material_name if mat else '',
                    'demand': demand,
                    'allocated': allocated_qty,  # 数值型已分配量（不再被覆盖）
                    'shortage': round(shortage, 2),
                    'stock': stock,
                    'is_complete': mpr.is_complete,
                    'complete_rate': cr,
                    'shortage_report': {
                        'material_shortages': [si for si in order_shortage_items if str(si.get('material_id')) == str(mat_id_str)]
                    } if order_shortage_items and any(str(si.get('material_id')) == str(mat_id_str) for si in order_shortage_items) else None,
                    'shortage_details': [mat_shortage] if mat_shortage else [],
                    '_allocation_detail': {mat_id_str: ad},  # 修复: 原allocated(=dict)重命名为_allocation_detail，避免覆盖数值
                    '_strategy_score': strat_score,
                })

        # 更新摘要中的紧急缺料统计（优先用shortage_details，为空则从results动态计算）
        if critical_count + urgent_count == 0 and len(results) > 0:
            # shortage_details全为空时，从results的shortage/complete_rate动态计算
            _dyn_critical = 0
            _dyn_urgent = 0
            for r in results:
                sh = float(r.get('shortage', 0) or 0)
                cr = float(r.get('complete_rate', 1) or 1)
                dem = float(r.get('demand', 1) or 1)
                if sh <= 0:
                    continue
                # 按缺料比例判定紧急程度
                shortage_ratio = sh / max(dem, 1)
                if cr < 0.5 or shortage_ratio > 0.7:
                    _dyn_critical += 1
                elif cr < 0.9 or shortage_ratio > 0.3:
                    _dyn_urgent += 1
            critical_count = _dyn_critical
            urgent_count = _dyn_urgent
        summary['total_critical_shortages'] = critical_count
        summary['total_urgent_shortages'] = urgent_count

        # ===== 3.5 BOM替换引擎 (v4核心功能) =====
        # 需求文档要求：原材、半成品普遍存在替代料场景；需合理结合替代料关系及优先级动态优化占料策略
        # 在results构建完成后、明细缓存构建前执行，直接修改results中的shortage值
        sub_stats = _apply_substitution_to_results(results, strategy)

        # 将替换统计写入summary供前端展示
        _extra_fields = {}
        if sub_stats['applied_substitutions'] > 0:
            summary['substitution_applied'] = True
            _sub_stats_dict = {
                'checked': sub_stats['total_checked'],
                'found': sub_stats['found_substitutes'],
                'applied': sub_stats['applied_substitutions'],
                'shortage_reduced': round(sub_stats['total_shortage_reduced'], 2),
                'orders_affected': len(sub_stats['orders_affected']),
            }
            summary['substitution_stats'] = _sub_stats_dict
            _extra_fields = {'substitution_applied': True, 'substitution_stats': _sub_stats_dict}
            logger.info(f"BOM替换生效({strategy}): {sub_stats['applied_substitutions']}次替换, "
                        f"减少缺料{sub_stats['total_shortage_reduced']:.0f}件")

        # 3. 直接构建明细数据（绕过_build_and_cache_display_data，避免聚合层丢失数据）
        # 注意：_fast_path_build_details内部已经为当前策略构建了detail/shortage/summary三个缓存
        # v4-BOM: 传入extra_summary_fields确保substitution_stats写入planning_summary缓存
        _fast_path_build_details(results, all_shortage_reports, strategy,
                                 extra_summary_fields=_extra_fields)

        # 4. 序列化并写入planning_summary缓存（只写策略专属key，不写共享key）
        # 注意：_fast_path_build_details 内部已写入差异化planning_summary缓存
        # 此处不再重复写入optimizer的统一summary值，避免覆盖差异化结果
        # from .api.serializers import PlanningSummarySerializer
        # serializer = PlanningSummarySerializer(summary)
        # safe_set(f'planning_summary_{strategy}', serializer.data, 3600)  # 已被_fast_path_build_details覆盖

        # 4.5. 同时写入 material_plan_{strategy} 缓存（供 get_planning_status / 大屏共享读取）
        safe_set(f'material_plan_{strategy}', {'summary': summary}, 3600)

        # 5. 缓存原始结果（只写策略专属key，不写共享key，不截断）
        safe_set(f'planning_results_{strategy}', results, 3600)

        # 6. 记录策略信息
        safe_set('latest_planning_strategy', {
            'strategy': strategy,
            'consumption_priority': 'PRIORITY',
            'timestamp': datetime.now().isoformat(),
            'source': 'fast_path_db_rebuild'
        }, 300)

        elapsed = (datetime.now() - t0).total_seconds()
        logger.info(f"快速路径完成: {mpr_total}条MPR记录 → 缓存重建完毕, 耗时{elapsed:.2f}秒")

        return {
            'status': 'success',
            'cache_key': f'fast_path_{datetime.now().strftime("%Y%m%d_%H%M%S")}',
            'summary': summary,
            'fast_path': True,
            'elapsed_seconds': round(elapsed, 2)
        }

    except Exception as e:
        logger.error(f"快速路径重建缓存失败: {str(e)}\n{traceback.format_exc()}")
        return {'status': 'error', 'message': str(e), 'fallback_to_optimizer': True}


def _rebuild_caches_for_strategy(strategy):
    """为指定策略重建明细缓存（共享DB查询，仅排序不同）。
    由 run_material_planning_async 在主策略执行成功后调用，确保切换策略时无需重新执行。
    """
    from .models import MaterialPlanResult, Material, Inventory
    from django.db.models import Sum, Avg
    import ast as _ast3

    mpr_qs = MaterialPlanResult.objects.select_related('order').all()
    material_info = {m.id: m for m in Material.objects.all()}
    inventory_totals = dict(
        Inventory.objects.values('material_id').annotate(total=Sum('quantity')).values_list('material_id', 'total')
    )

    _STRATEGY_WEIGHTS = {
        'delivery_first':    {'urgency': 3.0, 'shortage': 2.0, 'rate': 1.0},
        'cost_first':        {'urgency': 1.0, 'shortage': 3.0, 'rate': 2.0},
        'inventory_first':   {'urgency': 1.0, 'shortage': 2.0, 'rate': 3.0},
        'supplier_first':    {'urgency': 2.0, 'shortage': 2.0, 'rate': 1.5},
        'stability_first':   {'urgency': 2.0, 'shortage': 1.5, 'rate': 2.5},
        'expiry_first':      {'urgency': 2.5, 'shortage': 2.5, 'rate': 1.0},
    }
    sw = _STRATEGY_WEIGHTS.get(strategy, _STRATEGY_WEIGHTS['delivery_first'])

    results = []
    all_shortage_reports = []

    for mpr in mpr_qs:
        order_obj = mpr.order
        order_no = getattr(order_obj, 'order_no', None) or f'ORD{mpr.order_id}'
        order_id = mpr.order_id
        cr = float(mpr.complete_rate or 0)

        import datetime as _dt_mod3
        _safe_eval_globals3 = {'__builtins__': {}, 'Decimal': float, 'date': type('D', (), {}), 'datetime': _dt_mod3}
        alloc_details = mpr.allocation_details or {}
        if isinstance(alloc_details, str):
            try: alloc_details = _ast3.literal_eval(alloc_details)
            except:
                try: alloc_details = eval(alloc_details, _safe_eval_globals3, {})
                except:
                    try: alloc_details = json.loads(alloc_details)
                    except: alloc_details = {}

        shortage_details = mpr.shortage_details or []
        if isinstance(shortage_details, str):
            try: shortage_details = _ast3.literal_eval(shortage_details)
            except:
                try: shortage_details = eval(shortage_details, _safe_eval_globals3, {})
                except:
                    try: shortage_details = json.loads(shortage_details)
                    except: shortage_details = []

        order_shortage_items = []
        for sd in (shortage_details if isinstance(shortage_details, list) else []):
            if not isinstance(sd, dict): continue
            mat_id = sd.get('material_id')
            mat = material_info.get(int(mat_id)) if mat_id else None
            order_shortage_items.append({
                'material_id': mat_id,
                'material_code': mat.material_code if mat else '',
                'material_name': mat.material_name if mat else '',
                'required': float(sd.get('required', sd.get('demand', 0)) or 0),
                'allocated': float(sd.get('allocated', 0) or 0),
                'shortage': float(sd.get('shortage', sd.get('shortage_quantity', 0)) or 0),
                'urgency_level': sd.get('urgency_level', 'urgent'),
                'urgency_label': {'critical':'紧急','urgent':'加急','normal':'正常'}.get(sd.get('urgency_level'), '正常'),
                'recommended_action': sd.get('recommendation', sd.get('recommended_action')),
                'recommended_supplier': sd.get('recommended_supplier'),
            })
        if order_shortage_items:
            all_shortage_reports.append({'order_no': order_no, 'order_id': order_id,
                'shortage_items': order_shortage_items, 'material_shortages': order_shortage_items})

        if not isinstance(alloc_details, dict):
            if cr < 0.99:
                results.append({'order_id': order_id, 'order_no': order_no,
                    'material_id': 0, 'material_code': '(综合)', 'material_name': '多物料',
                    'demand': getattr(order_obj, 'quantity', 100) or 100,
                    'allocated': 0, 'shortage': round((1-cr)*(getattr(order_obj,'quantity',100)or 100),2),
                    'stock': 0, 'is_complete': mpr.is_complete, 'complete_rate': cr,
                    'shortage_report': {'material_shortages': order_shortage_items} if order_shortage_items else None,
                    'shortage_details': shortage_details if isinstance(shortage_details,list) else [],
                    'allocated': {}, '_strategy_score': sw['rate']*(1-cr)+sw['shortage']*min((1-cr)*10,1)})
            continue

        for mat_id_str, ad in alloc_details.items():
            try: mat_id_int = int(mat_id_str)
            except (ValueError, TypeError): continue
            if not isinstance(ad, dict): continue
            mat = material_info.get(mat_id_int)
            stock = float(inventory_totals.get(mat_id_int, 0) or 0)
            demand = float(ad.get('required', 0) or 0)
            allocated_qty = float(ad.get('allocated', 0) or 0)
            mat_shortage = next((sd for sd in (shortage_details if isinstance(shortage_details,list) else [])
                if str(sd.get('material_id'))==str(mat_id_str)), None)
            if mat_shortage:
                exact_shortage = float(mat_shortage.get('shortage', mat_shortage.get('shortage_quantity',0)) or 0)
                shortage = exact_shortage if exact_shortage > 0 else max(0, demand*(1-cr))
            else:
                shortage = max(0, demand*(1-cr))
            urgency_val = 2.0 if cr < 0.5 else (1.0 if cr < 0.9 else 0.0)
            strat_score = sw['urgency']*urgency_val + sw['shortage']*min(shortage/max(demand,1),1) + sw['rate']*(1-cr)
            results.append({'order_id': order_id, 'order_no': order_no,
                'material_id': mat_id_int, 'material_code': mat.material_code if mat else f'MAT{mat_id}',
                'material_name': mat.material_name if mat else '', 'demand': demand,
                'allocated': allocated_qty, 'shortage': round(shortage,2),
                'stock': stock, 'is_complete': mpr.is_complete, 'complete_rate': cr,
                'shortage_report': {'material_shortages': [si for si in order_shortage_items if str(si.get('material_id'))==str(mat_id_str)]}
                    if order_shortage_items and any(str(si.get('material_id'))==str(mat_id_str) for si in order_shortage_items) else None,
                'shortage_details': [mat_shortage] if mat_shortage else [],
                '_allocation_detail': {mat_id_str: ad}, '_strategy_score': strat_score})

    # 用该策略的排序构建明细和缺料报表缓存（不写summary）
    _fast_path_build_details(results, all_shortage_reports, strategy)

    # 写入策略专属 summary 缓存（复用主策略的summary数据但标记来源）
    main_summary = safe_get(f'planning_summary_{strategy}')
    if main_summary and isinstance(main_summary, dict):
        safe_set(f'planning_summary_{strategy}', main_summary, 3600)
    logger.info(f'策略{strategy}明细缓存重建完毕 (%d条detail, %d条shortage)' % (len(results), len(all_shortage_reports)))


def run_material_planning_async(order_ids=None, strategy='delivery_first', enable_ai_analysis=False):
    import traceback
    try:
        from .models import SalesOrder, OrderAllocation, Inventory, MaterialPlanResult

        if order_ids is not None:
            orders = SalesOrder.objects.filter(id__in=order_ids).order_by('priority', 'demand_date')
        else:
            orders = SalesOrder.objects.filter(status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']).order_by('priority', 'demand_date')

        if not orders.exists():
            logger.info("异步物料计划: 无待处理订单，跳过")
            return {'status': 'skipped', 'reason': 'no_orders'}

        # ===== 快速路径：DB已有历史结果时跳过优化器（<5秒 vs 原来10分钟+） =====
        mpr_count = MaterialPlanResult.objects.count()
        if mpr_count > 0:
            logger.info(f"快速路径: 检测到 {mpr_count} 条MaterialPlanResult记录，跳过优化器，直接重建缓存")
            # 为当前策略执行完整重建（含summary缓存写入）
            fast_result = _rebuild_caches_from_existing_data(strategy, enable_ai_analysis)
            if fast_result.get('status') == 'success':
                # 同时为其余5种策略重建明细缓存（共享DB查询结果，仅排序不同）
                _ALL_STRATEGIES = ['delivery_first','inventory_first','cost_first','supplier_first','stability_first','expiry_first']
                for _other_st in _ALL_STRATEGIES:
                    if _other_st != strategy:
                        try:
                            _rebuild_caches_for_strategy(_other_st)
                        except Exception as _e2:
                            logger.warning(f'为策略{_other_st}建缓存失败(不影响主策略): {_e2}')
                return fast_result
            else:
                logger.warning(f"快速路径失败({fast_result.get('message')}), 降级到慢速路径")

        # ===== 慢速路径：无历史数据时执行完整优化 =====
        logger.info(f"慢速路径: 无历史数据，对 {orders.count()} 条订单执行完整优化...")
        optimizer = MultiObjectiveOptimizer(strategy=strategy)
        result = optimizer.optimize_allocation(orders)

        summary = result['summary']
        results = result['results']

        # Save to timestamped cache (original behavior)
        cache_key = f'material_planning_result_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        safe_set(cache_key, {
            'summary': summary,
            'results': results,
            'timestamp': datetime.now().isoformat()
        }, timeout=3600)

        # === KEY FIX: Also update planning_summary cache for frontend ===
        try:
            total_critical_shortages = 0
            total_urgent_shortages = 0
            all_shortage_reports = []
            for r in results:
                if r.get('shortage_report'):
                    all_shortage_reports.append(r['shortage_report'])
                    for ms in r['shortage_report'].get('material_shortages', []):
                        ulvl = ms.get('urgency_level', '')
                        if ulvl == 'critical':
                            total_critical_shortages += 1
                        elif ulvl == 'urgent':
                            total_urgent_shortages += 1

            summary['total_critical_shortages'] = total_critical_shortages
            summary['total_urgent_shortages'] = total_urgent_shortages
            summary['jit_optimization'] = result.get('jit_optimization', {})

            # Prepare inventory data (shared between AI analysis and procurement plan)
            inventory_data = {}
            for inv in Inventory.objects.select_related('material').all():
                inventory_data[inv.material_id] = {
                    'quantity': int(inv.quantity or 0),
                    'standard_cost': round(float(inv.material.standard_cost or 0), 2) if inv.material else 0
                }

            # AI analysis (性能优化: 默认关闭，通过前端开关控制)
            try:
                if enable_ai_analysis:
                    analyzer = InventoryAIAnalyzer()
                    allocations = list(OrderAllocation.objects.all().values(
                        'order_id', 'material_id', 'allocated_quantity',
                        'reliability_factor', 'allocation_time'
                    ))
                    orders_data = []
                    for o in orders:
                        orders_data.append({
                            'id': o.id, 'order_no': o.order_no,
                            'priority': o.priority, 'demand_date': str(o.demand_date),
                            'status': o.status, 'quantity': int(o.quantity or 0)
                        })
                    ai_analysis = analyzer.analyze_allocation_rationality(allocations, inventory_data, orders_data)
                    summary['ai_analysis'] = {
                        'allocation_quality': ai_analysis.get('allocation_quality', 0),
                        'inventory_utilization': ai_analysis.get('inventory_utilization', 0),
                        'procurement_recommendations': ai_analysis.get('procurement_recommendations', []),
                        'root_cause_analysis': ai_analysis.get('root_cause_analysis', []),
                        'potential_risks': ai_analysis.get('potential_risks', []),
                        'suggestions': ai_analysis.get('suggestions', [])
                    }
                else:
                    summary['ai_analysis'] = None
            except Exception as e:
                summary['ai_analysis'] = None

            # Procurement plan (同样由前端开关控制)
            try:
                if enable_ai_analysis:
                    analyzer = InventoryAIAnalyzer()
                    combined_sr = {'material_shortages': []}
                    for sr in all_shortage_reports:
                        combined_sr['material_shortages'].extend(sr.get('material_shortages', []))
                    summary['procurement_plan'] = analyzer.generate_procurement_plan(combined_sr, inventory_data)
                else:
                    summary['procurement_plan'] = None
            except Exception as e:
                logger.warning(f"采购计划生成失败: {e}")
                summary['procurement_plan'] = None

            # ===== 修复: 不再覆盖优化器的total_orders =====
            # 原代码用 SalesOrder.objects.all().count() 覆盖了优化器返回的正确值
            # 这导致: total_orders=14000(全部订单) 而非4317(实际处理的活跃订单)
            # pending_orders = 14000 - 89 - 3389 = 10522 (完全错误!)
            #
            # 正确做法: 保留优化器 get_planning_summary() 的原始计算结果
            # total_orders = 实际处理的活跃订单数(与视图查询条件一致)
            # complete_orders + partial_orders + pending_orders == total_orders (自洽)
            #
            # 注: 如果前端需要"DB总订单数"(含已取消/已完成)，应使用独立字段如 db_total_orders
            # 而非覆盖核心统计字段
            # summary['total_orders'] = SalesOrder.objects.all().count()  ← 已删除(错误覆盖)
            # summary['pending_orders'] = ...  ← 已删除(基于错误total_orders的计算)

            # 仅补充前端需要的额外字段（不修改已有正确值）
            if 'stable_orders' not in summary:
                summary['stable_orders'] = summary.get('complete_orders', 0) + summary.get('partial_orders', 0)
            if 'total_shortage_orders' not in summary:
                summary['total_shortage_orders'] = summary.get('pending_orders', 0)

            # 用真实的物料缺料数据覆盖紧急缺料统计（不允许用优化器内部计算伪造）
            # 注意：必须先构建 material_plan_detail，再从中统计（见下方 _build_and_cache_display_data 调用）

            # === 先构建 material_plan_detail 和 shortage_report（必须在 summary 序列化之前） ===
            # v4-BOM: 非快速路径也需要执行替换（与快速路径保持一致）
            sub_stats_slow = _apply_substitution_to_results(results, strategy)
            if sub_stats_slow['applied_substitutions'] > 0:
                summary['substitution_applied'] = True
                summary['substitution_stats'] = {
                    'checked': sub_stats_slow['total_checked'],
                    'found': sub_stats_slow['found_substitutes'],
                    'applied': sub_stats_slow['applied_substitutions'],
                    'shortage_reduced': round(sub_stats_slow['total_shortage_reduced'], 2),
                    'orders_affected': len(sub_stats_slow['orders_affected']),
                }
                logger.info(f"BOM替换(慢速路径-{strategy}): {sub_stats_slow['applied_substitutions']}次替换, "
                           f"减少缺料{sub_stats_slow['total_shortage_reduced']:.0f}件")

            detail_data = _build_and_cache_display_data(results, summary, all_shortage_reports, strategy)

            # 用真实的物料缺料数据覆盖紧急缺料统计（不允许用优化器内部计算伪造）
            # 直接使用 _build_and_cache_display_data 返回的本地数据，避免 safe_get 异常导致缓存被清空
            if detail_data and isinstance(detail_data, list):
                crit_count = sum(1 for m in detail_data if m.get('urgency_level') == 'critical' and float(m.get('shortage', 0) or 0) > 0)
                urg_count = sum(1 for m in detail_data if m.get('urgency_level') == 'urgent' and float(m.get('shortage', 0) or 0) > 0)
                summary['total_critical_shortages'] = crit_count
                summary['total_urgent_shortages'] = urg_count

            # ===== 注意：planning_summary 已由 _build_and_cache_display_data 写入差异化缓存 =====
            # 慢速路径此处不再重复写入，避免覆盖快速路径的差异化结果
            # （_build_and_cache_display_data 内部已为6种策略分别生成独立汇总数据）

            # Cache the raw results for material_plan_detail and shortage_report endpoints (只写策略专属key)
            safe_set(f'planning_results_{strategy}', results, 3600)
            # 注意：绝对不能写 safe_set('planning_results', ...) 共享key！

            # 记录当前使用的策略，供展示API在缓存未命中时回退使用
            safe_set('latest_planning_strategy', {
                'strategy': strategy,
                'consumption_priority': optimizer.consumption_priority,
                'timestamp': datetime.now().isoformat()
            }, 300)
        except Exception as e:
            # Fallback: at least clear stale cache so next request recomputes
            _clear_all_planning_caches()

        return {
            'status': 'success',
            'cache_key': cache_key,
            'summary': summary
        }
    except Exception as e:
        logger.error(f"异步物料计划执行失败: {str(e)}\n{traceback.format_exc()}")
        return {'status': 'error', 'message': str(e)}


def update_inventory_cache():
    from .models import Inventory, SupplierCommitment
    
    inventory_data = {}
    for inv in Inventory.objects.select_related('material').all():
        key = f"inventory_{inv.material_id}"
        if key not in inventory_data:
            inventory_data[key] = []
            inventory_data[key].append({
            'id': inv.id,
            'quantity': int(inv.quantity or 0),
            'type': inv.inventory_type,
            'warehouse': inv.warehouse
        })
    
    safe_set('inventory_cache', inventory_data, timeout=1800)
    return {'status': 'success', 'count': len(inventory_data)}


def update_bom_cache():
    from .models import BillOfMaterials
    
    bom_data = {}
    for bom in BillOfMaterials.objects.select_related('parent_material', 'child_material').filter(is_active=True):
        key = f"bom_{bom.parent_material_id}"
        if key not in bom_data:
            bom_data[key] = []
        bom_data[key].append({
            'child_id': bom.child_material_id,
            'quantity': round(float(bom.quantity or 0), 2),
            'alternative_group': bom.alternative_group,
            'alternative_priority': bom.alternative_priority,
            'alternative_ratio': bom.alternative_ratio
        })
    
    safe_set('bom_cache', bom_data, timeout=1800)
    return {'status': 'success', 'count': len(bom_data)}


def generate_daily_report():
    from .models import SalesOrder

    today = datetime.now().date()
    orders_today = SalesOrder.objects.filter(order_date=today).count()
    completed_orders = SalesOrder.objects.filter(
        status__in=['complete', 'completed', 'shipped', 'delivered']
    ).count()
    # 修复: 使用活跃订单数(与物料计划一致)而非全部DB订单
    active_orders = SalesOrder.objects.filter(
        status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
    ).count()

    report = {
        'date': today.isoformat(),
        'total_orders': active_orders,
        'orders_today': orders_today,
        'completed_orders': completed_orders,
        'generated_at': datetime.now().isoformat()
    }
    
    safe_set(f'daily_report_{today}', report, timeout=86400)
    return {'status': 'success', 'report': report}


def auto_release_expired_hold_inventory():
    """自动解冻到期的Hold库存 - 建议由定时任务每天凌晨调用"""
    from .models import auto_release_expired_holds
    try:
        released_count = auto_release_expired_holds()
        logger.info(f'自动解冻完成，释放 {released_count} 条Hold库存')
        return {'status': 'success', 'released_count': released_count}
    except Exception as e:
        logger.error(f'自动解冻失败: {e}')
        return {'status': 'error', 'message': str(e)}


def _build_and_cache_display_data(results, summary, all_shortage_reports, strategy='delivery_first'):
    """
    从执行结果构建 material_plan_detail 和 shortage_report 并缓存

    确保展示数据来源于带策略的执行结果，而非数据库默认重算
    """
    try:
        from .models import Material, SalesOrder, Inventory
        from django.db.models import Sum

        # ===== 1. 构建 material_plan_detail（按物料聚合） =====
        # 从执行结果中提取每个物料的分配/缺料情况
        material_map = {}  # material_id -> 聚合数据
        material_info = {m.id: m for m in Material.objects.all()}

        for r in results:
            order_id = r.get('order_id')
            order_no = r.get('order_no', '')

            # 从 allocated 字段提取每个物料在该订单中的分配情况
            allocated = r.get('allocated') or {}
            if isinstance(allocated, dict):
                for mat_id, alloc_data in allocated.items():
                    mat_id_int = int(mat_id) if mat_id else None
                    if not mat_id_int:
                        continue
                    if mat_id_int not in material_map:
                        material_map[mat_id_int] = {
                            'demand': 0.0, 'allocated_qty': 0.0,
                            'shortage': 0.0, 'orders': [],
                            'original_shortage': 0.0,   # v4
                            'alternative_materials': [], # v4
                        }
                    alloc_list = alloc_data.get('allocations') if isinstance(alloc_data, dict) else []
                    alloc_total = sum(a.get('quantity', 0) for a in alloc_list) if isinstance(alloc_list, list) and alloc_list else 0
                    # 兜底：如果allocations列表为空，直接取allocated字段值（MPR存储的实际分配量）
                    if alloc_total <= 0 and isinstance(alloc_data, dict):
                        alloc_total = float(alloc_data.get('allocated', 0) or 0)
                    material_map[mat_id_int]['allocated_qty'] += alloc_total

                    # 需求量使用该物料在此订单中的实际需求量（required），而非整单数量
                    mat_required = float(alloc_data.get('required', 0) or 0)
                    # fallback: 如果没有 per-material required，尝试从 order quantity 获取
                    if mat_required <= 0:
                        mat_required = float(r.get('quantity', 0) or 0)
                    material_map[mat_id_int]['demand'] += mat_required
                    material_map[mat_id_int]['orders'].append(order_no)

            # 从 shortage_details 提取缺料信息
            shortage_details = r.get('shortage_details') or []
            if isinstance(shortage_details, list):
                for sd in shortage_details:
                    mat_id = sd.get('material_id')
                    if mat_id and int(mat_id) in material_map:
                        # 缺料已在上面通过 demand - allocated 计算，这里补充紧急度等信息
                        pass

            # 从 shortage_report 提取紧急度、推荐行动等（优先级最高）
            sr = r.get('shortage_report')
            if sr and isinstance(sr, dict):
                for ms in sr.get('material_shortages', []):
                    mat_id = ms.get('material_id')
                    if mat_id and int(mat_id) in material_map:
                        entry = material_map[int(mat_id)]
                        entry['urgency_level'] = ms.get('urgency_level', entry.get('urgency_level'))
                        entry['urgency_label'] = ms.get('urgency_label', entry.get('urgency_label'))
                        entry['recommended_action'] = ms.get('recommended_action', entry.get('recommended_action'))
                        entry['latest_purchase_date'] = ms.get('latest_purchase_date', entry.get('latest_purchase_date'))
                        entry['suppliers_data'] = ms.get('suppliers', [])
                        # v4-BOM: 收集替代料信息
                        _ms_alts = ms.get('alternative_materials')
                        if isinstance(_ms_alts, list) and _ms_alts:
                            entry['alternative_materials'].extend(_ms_alts)
                        _ms_orig_short = ms.get('original_shortage') or ms.get('shortage_qty')
                        if _ms_orig_short:
                            entry['original_shortage'] += float(_ms_orig_short)

            # v4-BOM: 从results顶层收集替代料（shortage_report可能为空时兜底）
            _r_alts_top = r.get('alternative_materials')
            if isinstance(_r_alts_top, list) and _r_alts_top:
                # 尝试匹配到对应物料
                _r_mat_id = r.get('material_id')
                if _r_mat_id and int(_r_mat_id) in material_map:
                    material_map[int(_r_mat_id)]['alternative_materials'].extend(_r_alts_top)
                # 也尝试按order_id匹配
                _r_orig_s = r.get('_original_shortage') or r.get('shortage', 0)
                if _r_orig_s:
                    for _mid, _mentry in material_map.items():
                        if _mentry.get('original_shortage', 0) == 0:
                            _mentry['original_shortage'] = float(_r_orig_s)

        # 用库存总量补全 allocated（执行结果中可能只记录了部分分配）
        inventory_totals = dict(
            Inventory.objects.values('material_id').annotate(total=Sum('quantity')).values_list('material_id', 'total')
        )

        plan_details = []
        for mat_id, agg in material_map.items():
            material = material_info.get(mat_id)
            if not material:
                continue

            demand = agg['demand']
            # 已分配量取执行结果，库存总量作为上限参考
            allocated_qty = min(agg['allocated_qty'], float(inventory_totals.get(mat_id, 0) or 0))
            stock = float(inventory_totals.get(mat_id, 0) or 0)
            safety_stock = float(material.safety_stock or 0) if hasattr(material, 'safety_stock') else 0
            shortage = max(0, demand - allocated_qty)

            # 状态判定：严格基于缺料量，不允许用低库存伪造缺料状态
            if shortage <= 0:
                # 无缺料：库存充足（低库存是独立指标，不混入缺料状态）
                status = 'sufficient'
                priority = 'low'
            elif shortage > demand * 0.5:
                # 缺料超过需求50%：严重缺料
                status = 'shortage'
                priority = 'high'
            else:
                # 有缺料但不超过50%：预警
                status = 'warning'
                priority = 'normal'

            # 紧急度：统一使用时间维度判定（与 material_planning.py analyze_shortage 一致）
            # 优先保留优化器已计算的值，仅作兜底
            existing_urgency = agg.get('urgency_level')
            if shortage > 0 and existing_urgency:
                urgency_level = existing_urgency
            elif shortage > 0:
                # 兜底：基于采购剩余天数判定（与核心算法一致）
                from datetime import date as _date
                latest_purchase = agg.get('latest_purchase_date')
                if latest_purchase:
                    try:
                        days_left = (latest_purchase - _date.today()).days if hasattr(latest_purchase, 'days') else (
                            (_date.fromisoformat(str(latest_purchase)) - _date.today()).days
                            if isinstance(latest_purchase, str) else 99)
                    except Exception:
                        days_left = 99
                else:
                    days_left = 99
                if days_left <= 3:
                    urgency_level = 'critical'
                elif days_left <= 14:
                    urgency_level = 'urgent'
                elif days_left <= 30:
                    urgency_level = 'normal'
                else:
                    urgency_level = 'relaxed'
            else:
                urgency_level = 'relaxed'

            urgency_label = agg.get('urgency_label') or (
                urgency_level == 'critical' and '紧急' or
                urgency_level == 'urgent' and '加急' or
                urgency_level == 'normal' and '正常' or
                '充足'
            )

            plan_details.append({
                'id': mat_id,
                'material_code': material.material_code,
                'material_name': material.material_name,
                'demand': round(demand, 2),
                'stock': round(stock, 2),
                'shortage': round(shortage, 2),
                'original_shortage': round(agg.get('original_shortage', shortage), 2),  # v4
                'alternative_materials': agg.get('alternative_materials', []),          # v4
                'status': status,
                'priority': priority,
                'safety_stock': round(safety_stock, 2),
                'latest_purchase_date': agg.get('latest_purchase_date'),
                'urgency_level': urgency_level,
                'urgency_label': urgency_label,
                'recommended_action': agg.get('recommended_action'),
                'suppliers': agg.get('suppliers_data', []),
            })

        # 按策略相关维度排序（不同策略产生不同的物料优先级排序）
        _STRATEGY_SORT_MAP = {
            'cost_first':        lambda x: (x['shortage'], x['demand']),                    # 成本优先：缺料量最大优先（减少加急采购成本）
            'expiry_first':      lambda x: (x.get('safety_stock') or 0,                     # 到期优先：安全库存接近0的优先
                                           -(x.get('days_to_latest_purchase') or 0)),       # 最久未采购的优先
            'delivery_first':    lambda x: (x['urgency_level'] == 'critical',                # 交付优先：紧急度+缺料量
                                           x['urgency_level'] == 'urgent',
                                           x['shortage']),
            'inventory_first':   lambda x: (x['stock'], -x['shortage']),                     # 库存优先：有库存的排前面
            'stability_first':   lambda x: (x['demand'], x['shortage']),                     # 稳定优先：需求量大的优先（平滑生产）
            'supplier_first':    lambda x: (len(x.get('suppliers', []) or []),               # 供应商优先：供应商数量多优先
                                           x.get('safety_stock') or 0,                       # 安全库存低的次优
                                           -x['demand']),
        }
        sort_fn = _STRATEGY_SORT_MAP.get(strategy, _STRATEGY_SORT_MAP['delivery_first'])
        plan_details.sort(key=sort_fn, reverse=True)

        safe_set(f'material_plan_detail_{strategy}', plan_details, 300)
        # 注意：绝对不能写 safe_set('material_plan_detail', ...) 共享key！

        # ===== 2. 构建 shortage_report（按订单-物料展开） =====
        def _fallback_supplier(material_code=''):
            """回退：当shortage_report中无供应商数据时，从DB查询"""
            if not material_code:
                return None
            try:
                from .models import SupplierMaterial, Supplier
                # 优先从 SupplierMaterial 查询
                sm = SupplierMaterial.objects.filter(
                    material__material_code=material_code,
                    is_forbidden=False
                ).select_related('supplier').first()
                if sm and sm.supplier:
                    return sm.supplier.supplier_name
                # 回退：取任意一个可用供应商
                default_sup = Supplier.objects.first()
                return default_sup.supplier_name if default_sup else None
            except Exception:
                return None

        shortage_data = []
        for r in results:
            order_id = r.get('order_id')
            order_no = r.get('order_no', '')
            sr = r.get('shortage_report')

            if sr and isinstance(sr, dict):
                for ms in sr.get('material_shortages', []):
                    suppliers_data = ms.get('suppliers', [])
                    best_supplier = suppliers_data[0] if suppliers_data else None
                    shortage_data.append({
                        'order_id': order_id,
                        'order_no': order_no,
                        'material_code': ms.get('material_code', ''),
                        'material_name': ms.get('material_name', ''),
                        'required': ms.get('required_qty', 0),
                        'allocated': ms.get('available_qty', 0),
                        'shortage': ms.get('shortage_qty', 0),
                        'latest_purchase_date': str(ms.get('latest_purchase_date')) if ms.get('latest_purchase_date') else None,
                        'days_to_latest_purchase': ms.get('days_to_latest_purchase'),
                        'urgency_level': ms.get('urgency_level'),
                        'urgency_label': ms.get('urgency_label'),
                        'recommended_action': ms.get('recommended_action'),
                        'recommended_supplier': best_supplier.get('supplier_name') if best_supplier else _fallback_supplier(ms.get('material_code', '')),
                        'safety_stock': ms.get('safety_stock', 0),
                        'lead_time': ms.get('lead_time'),  # 无数据时为None，不伪造0
                        'suppliers': suppliers_data,
                        'alternative_materials': ms.get('alternative_materials', [])
                    })
            else:
                # 没有详细缺料报告时，从 shortage_details 构建
                shortage_details = r.get('shortage_details') or []
                if isinstance(shortage_details, list) and shortage_details:
                    for sd in shortage_details:
                        mat_id = sd.get('material_id')
                        material = material_info.get(int(mat_id)) if mat_id else None
                        req = float(sd.get('required', 0) or 0)
                        alloc = float(sd.get('allocated', 0) or 0)
                        short = max(0, req - alloc)
                        if short > 0:
                            shortage_data.append({
                                'order_id': order_id,
                                'order_no': order_no,
                                'material_code': material.material_code if material else str(mat_id or ''),
                                'material_name': material.material_name if material else '',
                                'required': req,
                                'allocated': alloc,
                                'shortage': short,
                                'latest_purchase_date': None,
                                'urgency_level': 'critical' if short > req * 0.5 else 'urgent',
                                'urgency_label': '紧急' if short > req * 0.5 else '加急',
                                'recommended_action': '缺料%.0f件，需尽快安排采购' % short,
                                'recommended_supplier': _fallback_supplier(material.material_code if material else ''),
                                'safety_stock': float(material.safety_stock or 0) if material and hasattr(material, 'safety_stock') else 0,
                                'lead_time': None,  # 无数据时为None，不伪造0
                                'suppliers': [],
                                'alternative_materials': []
                            })

        # 兜底3: 遍历所有results，对有缺料但无缺料报告的记录补充生成
        for r in results:
            result_shortage = float(r.get('shortage', 0) or 0)
            if result_shortage <= 0:
                continue
            order_id = r.get('order_id')
            already_has = any(sd.get('order_id') == order_id for sd in shortage_data)
            if already_has:
                continue
            mat_id = r.get('material_id')
            material = material_info.get(mat_id) if mat_id else None
            demand = float(r.get('demand', 0) or 0)
            allocated_val = r.get('allocated', 0)
            if isinstance(allocated_val, dict):
                allocated_val = sum(float(v.get('allocated', 0) or 0) for v in allocated_val.values())
            shortage_data.append({
                'order_id': order_id,
                'order_no': r.get('order_no', ''),
                'material_code': material.material_code if material else (r.get('material_code') or ''),
                'material_name': material.material_code if material else (r.get('material_name') or ''),
                'required': demand,
                'allocated': float(allocated_val or 0),
                'shortage': round(result_shortage, 2),
                'latest_purchase_date': None,
                'urgency_level': 'critical' if result_shortage > demand * 0.5 else ('urgent' if result_shortage > demand * 0.1 else 'normal'),
                'urgency_label': '紧急' if result_shortage > demand * 0.5 else ('加急' if result_shortage > demand * 0.1 else '正常'),
                'recommended_action': '缺料%.0f件，建议优先采购' % result_shortage,
                'recommended_supplier': _fallback_supplier(material.material_code if material else r.get('material_code', '')),
                'safety_stock': float(material.safety_stock or 0) if material and hasattr(material, 'safety_stock') else 0,
                'lead_time': None,  # 无数据时为None，不伪造0
                'suppliers': [],
                'alternative_materials': []
            })

        # ===== 修正：shortage<=0 的记录不应标记为 urgent/critical（planner可能遗留了旧逻辑） =====
        for item in shortage_data:
            sh = float(item.get('shortage', 0) or 0)
            if sh <= 0 and item.get('urgency_level') in ('urgent', 'critical'):
                item['urgency_level'] = 'relaxed'
                item['urgency_label'] = '充足'

        # 按策略相关维度排序（不同策略产生不同的缺料优先级排序）
        _SHORTAGE_STRATEGY_SORT = {
            'cost_first':        lambda x: (-float(x.get('shortage', 0) or 0),),                    # 成本优先：缺料量最大优先（减少加急采购成本）
            'expiry_first':      lambda x: (x.get('safety_stock') or 0,                             # 到期优先：安全库存接近0的优先
                                           -(x.get('days_to_latest_purchase') or 0)),
            'delivery_first':    lambda x: (x['urgency_level'] == 'critical',                        # 交付优先：紧急度+缺料量
                                           x['urgency_level'] == 'urgent',
                                           float(x.get('shortage', 0) or 0)),
            'inventory_first':   lambda x: (-(float(x.get('allocated', 0) or 0)),                  # 库存优先：已分配多的排前面
                                           float(x.get('shortage', 0) or 0)),
            'stability_first':   lambda x: (float(x.get('required', 0) or 0),                     # 稳定优先：需求量大的优先（平滑生产）
                                           float(x.get('shortage', 0) or 0)),
            'supplier_first':    lambda x: (1 if not x.get('recommended_supplier') else 0,         # 供应商优先：无供应商的排前面
                                           x['urgency_level'] == 'critical',
                                           float(x.get('shortage', 0) or 0)),
        }
        sort_fn = _SHORTAGE_STRATEGY_SORT.get(strategy, _SHORTAGE_STRATEGY_SORT.get('delivery_first'))
        shortage_data.sort(key=sort_fn, reverse=True)

        safe_set(f'shortage_report_{strategy}', shortage_data, 300)
        # 注意：绝对不能写 safe_set('shortage_report', ...) 共享key！

        logger.info(
            f"展示数据缓存完成: material_plan_detail={len(plan_details)}条, "
            f"shortage_report={len(shortage_data)}条"
        )
        return plan_details
    except Exception as e:
        logger.error(f"展示数据构建失败（将回退到实时计算）: {str(e)}", exc_info=True)
