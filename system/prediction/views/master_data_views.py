from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from ..models import Material, Supplier, Customer, BillOfMaterials, Inventory
import json


def check_bom_circular_reference(parent_id, child_id):
    """检查BOM是否存在循环引用"""
    if parent_id == child_id:
        return True

    visited = {parent_id}
    queue = [parent_id]

    while queue:
        current = queue.pop(0)
        children = BillOfMaterials.objects.filter(
            parent_material_id=current, is_active=True
        ).values_list('child_material_id', flat=True)

        for child in children:
            if child == child_id:
                return True
            if child not in visited:
                visited.add(child)
                queue.append(child)

    return False


# ========== 物料管理 ==========

@login_required
def material_list(request):
    """物料列表视图"""
    query = request.GET.get('q', '')
    material_type = request.GET.get('type', '')
    page = request.GET.get('page', 1)
    
    materials = Material.objects.all()
    
    if query:
        materials = materials.filter(
            Q(material_code__icontains=query) |
            Q(material_name__icontains=query)
        )
    
    if material_type:
        materials = materials.filter(material_type=material_type)
    
    materials = materials.order_by('material_code')
    
    paginator = Paginator(materials, 15)
    
    try:
        materials_page = paginator.page(page)
    except PageNotAnInteger:
        materials_page = paginator.page(1)
    except EmptyPage:
        materials_page = paginator.page(paginator.num_pages)
    
    inventory_data = Inventory.objects.values('material_id').annotate(total=Sum('quantity'))
    inventory_dict = {item['material_id']: item['total'] for item in inventory_data}
    
    for material in materials_page:
        material.current_stock = inventory_dict.get(material.id, 0)
    
    return render(request, 'master/material_list.html', {
        'materials': materials_page,
        'query': query,
        'material_type': material_type,
        'material_types': [('raw', '原材料'), ('semi', '半成品'), ('finished', '成品')],
        'paginator': paginator,
        'page_obj': materials_page
    })

@login_required
def material_add(request):
    """添加物料视图"""
    if request.method == 'POST':
        material_code = request.POST.get('material_code')
        material_name = request.POST.get('material_name')
        material_type = request.POST.get('material_type')
        unit = request.POST.get('unit', '件')
        safety_stock = request.POST.get('safety_stock', 0)
        min_order_qty = request.POST.get('min_order_qty', 1)
        lead_time = request.POST.get('lead_time', 7)
        standard_cost = request.POST.get('standard_cost', 0)
        sales_price = request.POST.get('sales_price', 0)
        min_production_qty = request.POST.get('min_production_qty', 1)
        shelf_life = request.POST.get('shelf_life', 0)
        
        if Material.objects.filter(material_code=material_code).exists():
            messages.error(request, '物料代码已存在')
            return redirect('material_add')
        
        Material.objects.create(
            material_code=material_code,
            material_name=material_name,
            material_type=material_type,
            unit=unit,
            safety_stock=safety_stock,
            min_order_qty=min_order_qty,
            lead_time=lead_time,
            standard_cost=standard_cost,
            sales_price=sales_price,
            min_production_qty=min_production_qty,
            shelf_life=shelf_life
        )
        
        messages.success(request, '物料添加成功')
        return redirect('material_list')
    
    return render(request, 'master/material_form.html', {
        'title': '添加物料',
        'material_types': [('raw', '原材料'), ('semi', '半成品'), ('finished', '成品')]
    })

@login_required
def material_edit(request, pk):
    """编辑物料视图"""
    material = get_object_or_404(Material, pk=pk)
    
    if request.method == 'POST':
        material.material_name = request.POST.get('material_name')
        material.material_type = request.POST.get('material_type')
        material.unit = request.POST.get('unit', '件')
        material.safety_stock = request.POST.get('safety_stock', 0)
        material.min_order_qty = request.POST.get('min_order_qty', 1)
        material.lead_time = request.POST.get('lead_time', 7)
        material.standard_cost = request.POST.get('standard_cost', 0)
        material.sales_price = request.POST.get('sales_price', 0)
        material.min_production_qty = request.POST.get('min_production_qty', 1)
        material.shelf_life = request.POST.get('shelf_life', 0)
        material.is_active = request.POST.get('is_active') == 'on'
        material.save()
        
        messages.success(request, '物料更新成功')
        return redirect('material_list')
    
    return render(request, 'master/material_form.html', {
        'title': '编辑物料',
        'material': material,
        'material_types': [('raw', '原材料'), ('semi', '半成品'), ('finished', '成品')]
    })

@login_required
def material_delete(request, pk):
    """删除物料视图"""
    material = get_object_or_404(Material, pk=pk)
    
    if request.method == 'POST':
        material.delete()
        messages.success(request, '物料删除成功')
        return redirect('material_list')
    
    return render(request, 'master/material_confirm_delete.html', {
        'material': material
    })

# ========== 供应商管理 ==========

@login_required
def supplier_list(request):
    """供应商列表视图"""
    query = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    suppliers = Supplier.objects.all()
    
    if query:
        suppliers = suppliers.filter(
            Q(supplier_code__icontains=query) |
            Q(supplier_name__icontains=query) |
            Q(contact_person__icontains=query)
        )
    
    suppliers = suppliers.order_by('supplier_code')
    
    paginator = Paginator(suppliers, 15)
    
    try:
        suppliers_page = paginator.page(page)
    except PageNotAnInteger:
        suppliers_page = paginator.page(1)
    except EmptyPage:
        suppliers_page = paginator.page(paginator.num_pages)
    
    return render(request, 'master/supplier_list.html', {
        'suppliers': suppliers_page,
        'query': query,
        'paginator': paginator,
        'page_obj': suppliers_page
    })

@login_required
def supplier_add(request):
    """添加供应商视图"""
    if request.method == 'POST':
        supplier_code = request.POST.get('supplier_code')
        supplier_name = request.POST.get('supplier_name')
        contact_person = request.POST.get('contact_person')
        phone = request.POST.get('phone')
        email = request.POST.get('email')
        address = request.POST.get('address')
        
        if Supplier.objects.filter(supplier_code=supplier_code).exists():
            messages.error(request, '供应商代码已存在')
            return redirect('supplier_add')
        
        Supplier.objects.create(
            supplier_code=supplier_code,
            supplier_name=supplier_name,
            contact_person=contact_person,
            phone=phone,
            email=email,
            address=address,
            rating=request.POST.get('rating', 'B'),
            delivery_reliability=float(request.POST.get('delivery_reliability', 0.9)),
            normal_lead_time=int(request.POST.get('normal_lead_time', 7))
        )
        
        messages.success(request, '供应商添加成功')
        return redirect('supplier_list')
    
    return render(request, 'master/supplier_form.html', {'title': '添加供应商'})

@login_required
def supplier_edit(request, pk):
    """编辑供应商视图"""
    supplier = get_object_or_404(Supplier, pk=pk)
    
    if request.method == 'POST':
        supplier.supplier_name = request.POST.get('supplier_name')
        supplier.contact_person = request.POST.get('contact_person')
        supplier.phone = request.POST.get('phone')
        supplier.email = request.POST.get('email')
        supplier.address = request.POST.get('address')
        supplier.rating = request.POST.get('rating', supplier.rating or 'B')
        supplier.delivery_reliability = float(request.POST.get('delivery_reliability', supplier.delivery_reliability or 0.9))
        supplier.normal_lead_time = int(request.POST.get('normal_lead_time', supplier.normal_lead_time or 7))
        supplier.is_active = request.POST.get('is_active') == 'on'
        supplier.save()
        
        messages.success(request, '供应商更新成功')
        return redirect('supplier_list')
    
    return render(request, 'master/supplier_form.html', {
        'title': '编辑供应商',
        'supplier': supplier
    })

@login_required
def supplier_delete(request, pk):
    """删除供应商视图"""
    supplier = get_object_or_404(Supplier, pk=pk)
    
    if request.method == 'POST':
        supplier.delete()
        messages.success(request, '供应商删除成功')
        return redirect('supplier_list')
    
    return render(request, 'master/supplier_confirm_delete.html', {
        'supplier': supplier
    })

# ========== 客户管理 ==========

@login_required
def customer_list(request):
    """客户列表视图"""
    query = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    customers = Customer.objects.all()
    
    if query:
        customers = customers.filter(
            Q(customer_code__icontains=query) |
            Q(customer_name__icontains=query) |
            Q(contact_person__icontains=query)
        )
    
    customers = customers.order_by('customer_code')
    
    paginator = Paginator(customers, 15)
    
    try:
        customers_page = paginator.page(page)
    except PageNotAnInteger:
        customers_page = paginator.page(1)
    except EmptyPage:
        customers_page = paginator.page(paginator.num_pages)
    
    return render(request, 'master/customer_list.html', {
        'customers': customers_page,
        'query': query,
        'paginator': paginator,
        'page_obj': customers_page
    })

@login_required
def customer_add(request):
    """添加客户视图"""
    if request.method == 'POST':
        customer_code = request.POST.get('customer_code')
        customer_name = request.POST.get('customer_name')
        contact_person = request.POST.get('contact_person')
        phone = request.POST.get('phone')
        email = request.POST.get('email')
        address = request.POST.get('address')
        credit_limit = request.POST.get('credit_limit', 0)
        
        if Customer.objects.filter(customer_code=customer_code).exists():
            messages.error(request, '客户代码已存在')
            return redirect('customer_add')
        
        Customer.objects.create(
            customer_code=customer_code,
            customer_name=customer_name,
            contact_person=contact_person,
            phone=phone,
            email=email,
            address=address,
            credit_limit=credit_limit,
            customer_type=request.POST.get('customer_type', 'normal'),
            payment_terms=request.POST.get('payment_terms', 'net30'),
            customer_level=request.POST.get('customer_level', 'B'),
            delivery_priority=int(request.POST.get('delivery_priority', 3))
        )
        
        messages.success(request, '客户添加成功')
        return redirect('customer_list')
    
    return render(request, 'master/customer_form.html', {'title': '添加客户'})

@login_required
def customer_edit(request, pk):
    """编辑客户视图"""
    customer = get_object_or_404(Customer, pk=pk)
    
    if request.method == 'POST':
        customer.customer_name = request.POST.get('customer_name')
        customer.contact_person = request.POST.get('contact_person')
        customer.phone = request.POST.get('phone')
        customer.email = request.POST.get('email')
        customer.address = request.POST.get('address')
        customer.credit_limit = request.POST.get('credit_limit', 0)
        customer.customer_type = request.POST.get('customer_type', customer.customer_type or 'normal')
        customer.payment_terms = request.POST.get('payment_terms', customer.payment_terms or 'net30')
        customer.customer_level = request.POST.get('customer_level', customer.customer_level or 'B')
        customer.delivery_priority = int(request.POST.get('delivery_priority', customer.delivery_priority or 3))
        customer.is_active = request.POST.get('is_active') == 'on'
        customer.save()
        
        messages.success(request, '客户更新成功')
        return redirect('customer_list')
    
    return render(request, 'master/customer_form.html', {
        'title': '编辑客户',
        'customer': customer
    })

@login_required
def customer_delete(request, pk):
    """删除客户视图"""
    customer = get_object_or_404(Customer, pk=pk)
    
    if request.method == 'POST':
        customer.delete()
        messages.success(request, '客户删除成功')
        return redirect('customer_list')
    
    return render(request, 'master/customer_confirm_delete.html', {
        'customer': customer
    })

# ========== BOM管理 ==========

@login_required
def bom_add(request):
    """添加BOM视图"""
    if request.method == 'POST':
        parent_material_id = request.POST.get('parent_material')
        child_material_id = request.POST.get('child_material')
        quantity = request.POST.get('quantity')
        unit = request.POST.get('unit', '件')
        bom_level = request.POST.get('bom_level', 1)
        alternative_group = request.POST.get('alternative_group')
        alternative_priority = request.POST.get('alternative_priority', 1)
        alternative_ratio = request.POST.get('alternative_ratio', 1.0)

        if check_bom_circular_reference(int(parent_material_id), int(child_material_id)):
            messages.error(request, 'BOM存在循环引用，无法创建')
            return redirect('bom_add')

        parent_material = get_object_or_404(Material, pk=parent_material_id)
        child_material = get_object_or_404(Material, pk=child_material_id)

        BillOfMaterials.objects.create(
            parent_material=parent_material,
            child_material=child_material,
            quantity=quantity,
            unit=unit,
            bom_level=bom_level,
            alternative_group=alternative_group,
            alternative_priority=alternative_priority,
            alternative_ratio=alternative_ratio
        )
        
        messages.success(request, 'BOM添加成功')
        return redirect('bom_list')
    
    materials = Material.objects.filter(is_active=True)
    return render(request, 'master/bom_form.html', {
        'title': '添加BOM',
        'materials': materials
    })

@login_required
def bom_edit(request, pk):
    """编辑BOM视图"""
    bom = get_object_or_404(BillOfMaterials, pk=pk)

    if request.method == 'POST':
        child_material_id = request.POST.get('child_material')
        bom.quantity = request.POST.get('quantity')
        bom.unit = request.POST.get('unit', '件')
        bom.bom_level = request.POST.get('bom_level', 1)
        bom.alternative_group = request.POST.get('alternative_group')
        bom.alternative_priority = request.POST.get('alternative_priority', 1)
        bom.alternative_ratio = request.POST.get('alternative_ratio', 1.0)
        bom.is_active = request.POST.get('is_active') == 'on'

        if child_material_id and check_bom_circular_reference(bom.parent_material_id, int(child_material_id)):
            messages.error(request, 'BOM存在循环引用，无法更新')
            return redirect('bom_edit', pk=pk)

        if child_material_id:
            bom.child_material = get_object_or_404(Material, pk=child_material_id)

        bom.save()
        
        messages.success(request, 'BOM更新成功')
        return redirect('bom_list')
    
    materials = Material.objects.filter(is_active=True)
    return render(request, 'master/bom_form.html', {
        'title': '编辑BOM',
        'bom': bom,
        'materials': materials
    })

@login_required
def bom_delete(request, pk):
    """删除BOM视图"""
    bom = get_object_or_404(BillOfMaterials, pk=pk)
    
    if request.method == 'POST':
        bom.delete()
        messages.success(request, 'BOM删除成功')
        return redirect('bom_list')
    
    return render(request, 'master/bom_confirm_delete.html', {
        'bom': bom
    })