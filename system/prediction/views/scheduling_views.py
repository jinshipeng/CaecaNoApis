from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods
import numpy as np
from ..scheduling import Scheduler, AdvancedScheduler
from .. import utils
from ..models import PlanLog


# ============================================================
# 高级排程引擎 API（AdvancedScheduler）
# ============================================================

@require_http_methods(["GET", "POST"])
@login_required
def api_generate_schedule(request):
    """
    高级生产排程API — 使用AdvancedScheduler生成完整排程计划

    GET: 获取当前排程结果
    POST: 触发重新排程

    返回:
        {
            'success': True,
            'schedule': [...],       # 排程详情列表
            'summary': {...},        # 统计摘要
            'bottlenecks': [...],    # 瓶颈工作中心
            'unallocated': [...]     # 未排产订单
        }
    """
    try:
        scheduler = AdvancedScheduler()
        scheduler.load_production_resources()

        horizon_days = int(request.GET.get('horizon_days', 30)) if request.method == 'GET' else int(request.POST.get('horizon_days', 30))

        result = scheduler.generate_production_schedule(horizon_days=horizon_days)

        return JsonResponse({
            'success': True,
            'data': result
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_http_methods(["GET"])
@login_required
def api_gantt_data(request):
    """
    甘特图数据API — 返回前端甘特图组件所需的数据格式

    返回: list of {id, text, start_date, end_date, progress, work_center, ...}
    """
    try:
        scheduler = AdvancedScheduler()
        # 如果已有排程结果则直接返回甘特图数据
        if not scheduler.schedule_results:
            scheduler.load_production_resources()
            scheduler.generate_production_schedule()

        gantt_data = scheduler.get_gantt_data()

        return JsonResponse({
            'success': True,
            'data': gantt_data,
            'count': len(gantt_data)
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_http_methods(["POST"])
@login_required
def api_whatif_schedule(request):
    """
    排程What-If模拟API

    POST参数:
        scenario_type: urgent_insert / capacity_reduce / order_cancel / delay_order
        parameters: 场景参数字典
    """
    try:
        data = request.POST if request.content_type.startswith('multipart') else request.data if hasattr(request, 'data') else request.POST
        scenario_type = data.get('scenario_type', '')
        parameters = dict(data.getlist('parameters[]')) if hasattr(data, 'getlist') else {}

        scheduler = AdvancedScheduler()
        scheduler.load_production_resources()
        # 先运行基准排程
        scheduler.generate_production_schedule()

        result = scheduler.simulate_what_if(scenario_type, parameters)

        PlanLog.objects.create(
            log_type='INFO',
            message=f'排程What-If模拟完成: 场景={scenario_type}'
        )

        return JsonResponse({
            'success': True,
            'scenario_type': scenario_type,
            'result': result
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_http_methods(["POST"])
@login_required
def api_optimize_schedule(request):
    """
    多目标排程优化API

    POST参数:
        objectives: 逗号分隔的目标列表 (delivery, utilization, changeover)
    """
    try:
        objectives = request.POST.get('objectives', 'delivery,utilization,changeover').split(',')

        scheduler = AdvancedScheduler()
        scheduler.load_production_resources()

        result = scheduler.optimize_with_objectives(objectives)

        return JsonResponse({
            'success': True,
            'optimized_result': result
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@require_http_methods(["POST"])
@login_required
def api_backfill_material_plan(request):
    """
    排程结果反哺物料计划API

    将排程结果回写到 MaterialPlanResult 和 PlanLog
    """
    try:
        scheduler = AdvancedScheduler()
        scheduler.load_production_resources()
        schedule_result = scheduler.generate_production_schedule()

        backfill_result = scheduler.backfill_material_plan(schedule_result)

        return JsonResponse({
            'success': True,
            'backfill_result': backfill_result
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
def scheduling(request):
    """排产计划视图函数"""
    utils.clear_messages(request)

    if request.method == 'POST':
        selected_product = request.POST.get('product', '')
        production_capacity = int(request.POST.get('production_capacity', 100))
        initial_inventory = int(request.POST.get('initial_inventory', 50))
        lead_time = int(request.POST.get('lead_time', 1))
        weeks = int(request.POST.get('weeks', 4))

        demand_inputs = []
        for i in range(1, weeks + 1):
            key = f'demand_{i}'
            demand_val = request.POST.get(key, 0)
            try:
                demand_inputs.append(float(demand_val))
            except (ValueError, TypeError):
                demand_inputs.append(0)

        if not selected_product:
            messages.error(request, '请选择商品')
            return redirect('scheduling')

        if sum(demand_inputs) == 0:
            messages.error(request, '请输入至少一个周期的需求')
            return redirect('scheduling')

        scheduler = Scheduler(
            production_capacity=production_capacity,
            initial_inventory=initial_inventory,
            lead_time=lead_time
        )

        schedule, total_cost = scheduler.optimize_schedule(demand_inputs, weeks=weeks)

        production_plan = schedule['生产计划'].tolist()

        service_level, total_shortage, total_demand = scheduler.calculate_service_level(
            demand_inputs, production_plan, initial_inventory
        )

        service_level_percent = service_level * 100

        schedule_list = schedule.to_dict('records')

        for item in schedule_list:
            for key, value in item.items():
                if isinstance(value, np.integer):
                    item[key] = int(value)
                elif isinstance(value, np.floating):
                    item[key] = float(value)

        if isinstance(total_cost, np.integer):
            total_cost = int(total_cost)
        elif isinstance(total_cost, np.floating):
            total_cost = float(total_cost)

        request.session['current_schedule'] = schedule_list
        request.session['selected_product'] = selected_product
        request.session['scheduling_params'] = {
            'production_capacity': production_capacity,
            'initial_inventory': initial_inventory,
            'lead_time': lead_time,
            'weeks': weeks,
            'total_cost': total_cost,
            'service_level': service_level_percent,
            'total_shortage': total_shortage,
            'total_demand': total_demand,
        }

        utils.log_user_operation(
            request.user,
            '生成排产计划成功',
            f'商品: {selected_product}'
        )

        return redirect('scheduling')

    response = render(request, 'scheduling.html', {
        'schedule': request.session.get('current_schedule', []),
        'selected_product': request.session.get('selected_product', ''),
        'scheduling_params': request.session.get('scheduling_params', {}),
    })

    utils.clear_messages(request)
    return response


@login_required
def export_schedule(request):
    """导出排产计划视图函数"""
    try:
        schedule = request.session.get('current_schedule', [])
        selected_product = request.session.get('selected_product', '')

        if not schedule:
            response = HttpResponse(content_type='text/plain')
            response['Content-Disposition'] = 'attachment; filename="error.txt"'
            response.write('没有可导出的排产计划数据'.encode('utf-8'))
            return response

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = (
            'attachment; filename="排产计划_'
            '{selected_product}.csv"'
        ).format(selected_product=selected_product)

        response.write('\ufeff'.encode('utf-8'))
        response.write('商品代码,时间,生产计划\n'.encode('utf-8'))

        for row in schedule:
            line = f"{selected_product},第{row['周次']}周,{row['生产计划']}\n"
            response.write(line.encode('utf-8'))

        return response
    except KeyError as e:
        response = HttpResponse(content_type='text/plain')
        response['Content-Disposition'] = 'attachment; filename="error.txt"'
        response.write(f'数据格式错误: {str(e)}'.encode('utf-8'))
        return response
    except Exception as e:
        response = HttpResponse(content_type='text/plain')
        response['Content-Disposition'] = 'attachment; filename="error.txt"'
        response.write(f'导出失败: {str(e)}'.encode('utf-8'))
        return response
