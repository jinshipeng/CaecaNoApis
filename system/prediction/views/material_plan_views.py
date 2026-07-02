from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.db.models import Q, Count
import json
from datetime import datetime, timedelta
from ..material_planning import MaterialPlanner, MultiObjectiveOptimizer
from ..models import (
    SalesOrder, Material, BillOfMaterials, Inventory, Supplier,
    SupplierCommitment, MaterialPlanResult, PlanLog, SupplierMaterial
)


@login_required
def material_planning_dashboard(request):
    """物料计划仪表盘"""
    try:
        # 修复: 使用活跃订单(与物料计划一致)而非全部订单
        ACTIVE_STATUSES = ['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
        orders = SalesOrder.objects.filter(
            status__in=ACTIVE_STATUSES
        ).order_by('priority', 'demand_date')

        total_orders = orders.count()
        complete_orders = orders.filter(status__in=['complete', 'completed']).count()
        partial_orders = orders.filter(status='partial').count()
        pending_orders = orders.filter(status='pending').count()
        
        stable_orders = MaterialPlanResult.objects.filter(is_complete=True).count()
        
        summary_data = {
            'total_orders': total_orders,
            'complete_orders': complete_orders,
            'partial_orders': partial_orders,
            'pending_orders': pending_orders,
            'complete_rate': (complete_orders / total_orders * 100) if total_orders > 0 else 0,
            'stable_orders': stable_orders,
            'total_materials': Material.objects.count(),
            'total_suppliers': Supplier.objects.count(),
            'total_shortage_orders': total_orders - complete_orders,
        }
        
        recent_plans = MaterialPlanResult.objects.order_by('-created_at')[:10]
        recent_logs = PlanLog.objects.order_by('-created_at')[:10]
        
        return render(request, 'material_plan/dashboard.html', {
            'summary_data': summary_data,
            'orders': orders,
            'recent_plans': recent_plans,
            'recent_logs': recent_logs,
        })
    except Exception as e:
        messages.error(request, f'加载仪表盘失败: {str(e)}')
        return render(request, 'material_plan/dashboard.html', {})


@login_required
def run_material_planning(request):
    """执行物料计划"""
    if request.method == 'POST':
        try:
            # 从POST参数获取策略，默认使用 delivery_first
            strategy = request.POST.get('strategy', 'delivery_first')
            optimizer = MultiObjectiveOptimizer(strategy=strategy)
            # 修复: 添加select_related('material')避免BOM展开时的N+1查询
            orders = SalesOrder.objects.select_related('material').filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
            ).order_by('priority', 'demand_date')
            
            result = optimizer.optimize_allocation(orders)
            
            summary = result['summary']
            results = result['results']

            # 修复: session需要JSON序列化，date/datetime对象无法直接存储
            import json
            from datetime import date, datetime

            def make_json_safe(obj):
                """递归转换date/datetime等不可序列化对象为字符串"""
                if isinstance(obj, (date, datetime)):
                    return obj.isoformat()
                elif isinstance(obj, dict):
                    return {k: make_json_safe(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [make_json_safe(item) for item in obj]
                elif isinstance(obj, (int, float, str, bool)) or obj is None:
                    return obj
                else:
                    return str(obj)

            request.session['planning_summary'] = make_json_safe(summary)
            request.session['planning_results'] = make_json_safe(results)

            # 同步写入Django cache（每个策略独立缓存）
            # 执行时先清除所有旧缓存，避免残留脏数据影响其他策略
            try:
                from prediction.api.serializers import PlanningSummarySerializer
                from prediction.tasks import safe_set
                from prediction.utils.safe_cache import _memory_cache
                from django.core.cache import cache as _dj_cache

                serializer = PlanningSummarySerializer(summary)

                # 先清除该策略的所有旧缓存（防止脏数据残留）
                _strategies = ['delivery_first','inventory_first','cost_first','supplier_first','stability_first','expiry_first','']
                _prefixes = ['planning_results_', 'material_plan_detail_', 'shortage_report_']
                for _p in _prefixes:
                    for _s in _strategies:
                        try:
                            _dj_cache.cache.delete(_p + _s)
                        except Exception:
                            pass
                        _memory_cache.delete(_p + _s)

                # 只写入summary（格式正确），不写入planning_results原始数据
                # 原因：optimizer输出格式与rebuild函数期望的格式不同
                # detail/report由API端点在读取时从DB实时构建（含策略差异化排序）
                safe_set(f'planning_summary_{strategy}', serializer.data, 3600)
            except Exception as cache_err:
                # cache写入失败不影响主流程（session已保存）
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f'planning结果写入cache失败(不影响session): {cache_err}')

            messages.success(request, f'物料计划执行完成！齐套率: {summary.get("complete_rate", 0):.1%}')

            return redirect('/plan/material/result/')
        except Exception as e:
            messages.error(request, f'执行物料计划失败: {str(e)}')
            return redirect('/plan/material/')

    return redirect('/plan/material/')


@login_required
def material_planning_result(request):
    """物料计划结果展示"""
    summary = request.session.get('planning_summary', {})
    results = request.session.get('planning_results', [])
    
    # 添加齐套率百分比
    complete_rate = summary.get('avg_complete_rate', 0)
    summary['complete_rate_percent'] = complete_rate * 100
    
    # 获取物料名称映射
    material_names = {m.id: m.material_name for m in Material.objects.all()}
    
    for result in results:
        # 添加每个订单的齐套率百分比
        result_complete_rate = result.get('complete_rate', 0)
        result['complete_rate_percent'] = result_complete_rate * 100
        
        for material_id in result.get('requirements', {}):
            result['requirements'][material_id] = {
                'qty': result['requirements'][material_id],
                'name': material_names.get(material_id, material_id)
            }
    
    return render(request, 'material_plan/result.html', {
        'summary': summary,
        'results': results,
    })


@login_required
def order_list(request):
    """订单列表"""
    orders = SalesOrder.objects.all().order_by('priority', 'demand_date')
    
    return render(request, 'material_plan/order_list.html', {
        'orders': orders,
    })


@login_required
def order_detail(request, order_id):
    """订单详情"""
    try:
        order = SalesOrder.objects.get(id=order_id)
        plan_result = MaterialPlanResult.objects.filter(order=order).first()
        allocations = order.orderallocation_set.all()
        
        material_names = {m.id: m.material_name for m in Material.objects.all()}
        
        return render(request, 'material_plan/order_detail.html', {
            'order': order,
            'plan_result': plan_result,
            'allocations': allocations,
            'material_names': material_names,
        })
    except SalesOrder.DoesNotExist:
        messages.error(request, '订单不存在')
        return redirect('order_list')


@login_required
def inventory_list(request):
    """库存列表"""
    from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
    
    query = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    inventories = Inventory.objects.all().order_by('material__material_code')
    
    if query:
        inventories = inventories.filter(
            Q(material__material_code__icontains=query) |
            Q(material__material_name__icontains=query) |
            Q(batch_no__icontains=query)
        )
    
    paginator = Paginator(inventories, 15)
    
    try:
        inventories_page = paginator.page(page)
    except PageNotAnInteger:
        inventories_page = paginator.page(1)
    except EmptyPage:
        inventories_page = paginator.page(paginator.num_pages)
    
    return render(request, 'material_plan/inventory_list.html', {
        'inventories': inventories_page,
        'paginator': paginator,
        'page_obj': inventories_page,
        'query': query,
    })


@login_required
def bom_list(request):
    """BOM列表"""
    boms = BillOfMaterials.objects.all().order_by('parent_material__material_code')
    
    return render(request, 'material_plan/bom_list.html', {
        'boms': boms,
    })


@login_required
def shortage_report(request):
    """缺料报表"""
    plan_results = MaterialPlanResult.objects.filter(is_complete=False)
    
    shortage_data = []
    material_names = {m.id: m.material_name for m in Material.objects.all()}
    
    forbidden_materials = set()
    for sm in SupplierMaterial.objects.filter(is_forbidden=True).select_related('material', 'supplier'):
        forbidden_materials.add(sm.material_id)
    
    for result in plan_results:
        if result.shortage_details:
            try:
                details = json.loads(result.shortage_details)
                for shortage in details:
                    material_id = shortage.get('material_id')
                    shortage_data.append({
                        'order_no': result.order.order_no,
                        'material_code': material_id,
                        'material_name': material_names.get(material_id, material_id),
                        'required': shortage.get('required'),
                        'allocated': shortage.get('allocated'),
                        'shortage': shortage.get('shortage'),
                        'is_forbidden': material_id in forbidden_materials,
                    })
            except (KeyError, TypeError, AttributeError):
                pass
    
    forbidden_material_list = []
    materials_map = {m.id: m for m in Material.objects.filter(id__in=forbidden_materials)}
    order_counts = dict(
        SalesOrder.objects.filter(material_id__in=forbidden_materials, status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing'])
        .values('material_id').annotate(cnt=Count('id')).values_list('material_id', 'cnt')
    )
    for mat_id in forbidden_materials:
        material = materials_map.get(mat_id)
        if material:
            forbidden_material_list.append({
                'material_id': mat_id,
                'material_code': material.material_code,
                'material_name': material.material_name,
                'affected_orders': order_counts.get(mat_id, 0),
            })
    
    total_shortage = sum(item.get('shortage', 0) for item in shortage_data)
    
    return render(request, 'material_plan/shortage_report.html', {
        'shortage_data': shortage_data,
        'forbidden_materials': forbidden_material_list,
        'total_shortage': total_shortage,
    })


@login_required
def export_planning_result(request):
    """导出物料计划结果"""
    results = request.session.get('planning_results', [])
    
    if not results:
        response = HttpResponse(content_type='text/plain')
        response['Content-Disposition'] = 'attachment; filename="error.txt"'
        response.write('没有可导出的物料计划数据'.encode('utf-8'))
        return response
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="物料计划结果.csv"'
    
    response.write('\ufeff'.encode('utf-8'))
    response.write('订单编号,齐套率,状态\n'.encode('utf-8'))
    
    for result in results:
        status = '齐套' if result['is_complete'] else '部分齐套' if result['complete_rate'] > 0 else '未齐套'
        line = f"{result['order_no']},{result['complete_rate']:.2f},{status}\n"
        response.write(line.encode('utf-8'))
    
    return response


@login_required
def planning_logs(request):
    """计划日志"""
    logs = PlanLog.objects.all().order_by('-created_at')
    return render(request, 'material_plan/logs.html', {'logs': logs})
