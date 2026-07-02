from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from ..models import Capacity, Material

@login_required
def capacity_list(request):
    """产能列表视图"""
    query = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    capacities = Capacity.objects.all().order_by('work_center')
    
    if query:
        capacities = capacities.filter(
            Q(work_center__icontains=query) |
            Q(material__material_code__icontains=query)
        )
    
    paginator = Paginator(capacities, 15)
    
    try:
        capacities_page = paginator.page(page)
    except PageNotAnInteger:
        capacities_page = paginator.page(1)
    except EmptyPage:
        capacities_page = paginator.page(paginator.num_pages)
    
    return render(request, 'plan/capacity_list.html', {
        'capacities': capacities_page,
        'query': query,
        'paginator': paginator,
        'page_obj': capacities_page
    })

@login_required
def capacity_add(request):
    """添加产能视图"""
    if request.method == 'POST':
        work_center = request.POST.get('work_center')
        material_id = request.POST.get('material')
        daily_capacity = request.POST.get('daily_capacity')
        weekly_capacity = request.POST.get('weekly_capacity')
        
        material = None
        if material_id:
            material = get_object_or_404(Material, pk=material_id)
        
        Capacity.objects.create(
            work_center=work_center,
            material=material,
            daily_capacity=daily_capacity,
            weekly_capacity=weekly_capacity
        )
        
        messages.success(request, '产能添加成功')
        return redirect('capacity_list')
    
    materials = Material.objects.filter(is_active=True)
    return render(request, 'plan/capacity_form.html', {
        'title': '添加产能',
        'materials': materials
    })

@login_required
def capacity_edit(request, pk):
    """编辑产能视图"""
    capacity = get_object_or_404(Capacity, pk=pk)
    
    if request.method == 'POST':
        capacity.work_center = request.POST.get('work_center')
        material_id = request.POST.get('material')
        
        if material_id:
            capacity.material = get_object_or_404(Material, pk=material_id)
        else:
            capacity.material = None
        
        capacity.daily_capacity = request.POST.get('daily_capacity')
        capacity.weekly_capacity = request.POST.get('weekly_capacity')
        capacity.is_active = request.POST.get('is_active') == 'on'
        capacity.save()
        
        messages.success(request, '产能更新成功')
        return redirect('capacity_list')
    
    materials = Material.objects.filter(is_active=True)
    return render(request, 'plan/capacity_form.html', {
        'title': '编辑产能',
        'capacity': capacity,
        'materials': materials
    })

@login_required
def capacity_delete(request, pk):
    """删除产能视图"""
    capacity = get_object_or_404(Capacity, pk=pk)
    
    if request.method == 'POST':
        capacity.delete()
        messages.success(request, '产能删除成功')
        return redirect('capacity_list')
    
    return render(request, 'plan/capacity_confirm_delete.html', {
        'capacity': capacity
    })