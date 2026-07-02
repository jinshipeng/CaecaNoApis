from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from ..models import Inventory, PurchaseOrder, SalesOrder, SupplierCommitment, Material, Supplier
import json

# ========== 库存管理 ==========

@login_required
def inventory_add(request):
    """添加库存视图"""
    if request.method == 'POST':
        material_id = request.POST.get('material')
        inventory_type = request.POST.get('inventory_type')
        quantity = request.POST.get('quantity')
        warehouse = request.POST.get('warehouse')
        location = request.POST.get('location')
        batch_no = request.POST.get('batch_no')
        expiry_date = request.POST.get('expiry_date')
        
        material = get_object_or_404(Material, pk=material_id)

        # 使用update_or_create避免违反(material, warehouse)唯一约束
        wh = warehouse or '主仓库'
        Inventory.objects.update_or_create(
            material=material,
            warehouse=wh,
            defaults={
                'inventory_type': inventory_type or 'local',
                'quantity': quantity or 0,
                'location': location or '',
                'batch_no': batch_no or '',
                'expiry_date': expiry_date or None,
            }
        )
        
        messages.success(request, '库存添加成功')
        return redirect('inventory_list')
    
    materials = Material.objects.filter(is_active=True)
    return render(request, 'supply/inventory_form.html', {
        'title': '添加库存',
        'materials': materials,
        'inventory_types': [
            ('local', '本地库存'),
            ('transit', '在途库存'),
            ('supplier', '供应商承诺'),
            ('finished', '成品库存'),
            ('semi', '半成品库存')
        ]
    })

@login_required
def inventory_edit(request, pk):
    """编辑库存视图"""
    inventory = get_object_or_404(Inventory, pk=pk)
    
    if request.method == 'POST':
        inventory.quantity = request.POST.get('quantity')
        inventory.warehouse = request.POST.get('warehouse')
        inventory.location = request.POST.get('location')
        inventory.batch_no = request.POST.get('batch_no')
        inventory.expiry_date = request.POST.get('expiry_date')
        inventory.is_hold = request.POST.get('is_hold') == 'on'
        inventory.hold_reason = request.POST.get('hold_reason')
        inventory.hold_until = request.POST.get('hold_until')
        inventory.save()
        
        messages.success(request, '库存更新成功')
        return redirect('inventory_list')
    
    materials = Material.objects.filter(is_active=True)
    return render(request, 'supply/inventory_form.html', {
        'title': '编辑库存',
        'inventory': inventory,
        'materials': materials,
        'inventory_types': [
            ('local', '本地库存'),
            ('transit', '在途库存'),
            ('supplier', '供应商承诺'),
            ('finished', '成品库存'),
            ('semi', '半成品库存')
        ]
    })

@login_required
def inventory_delete(request, pk):
    """删除库存视图"""
    inventory = get_object_or_404(Inventory, pk=pk)
    
    if request.method == 'POST':
        inventory.delete()
        messages.success(request, '库存删除成功')
        return redirect('inventory_list')
    
    return render(request, 'supply/inventory_confirm_delete.html', {
        'inventory': inventory
    })

# ========== 采购订单管理 ==========

@login_required
def purchase_order_list(request):
    """采购订单列表视图"""
    query = request.GET.get('q', '')
    status = request.GET.get('status', '')
    page = request.GET.get('page', 1)
    
    orders = PurchaseOrder.objects.all().order_by('-order_date')
    
    if query:
        orders = orders.filter(
            Q(po_no__icontains=query) |
            Q(supplier__supplier_name__icontains=query) |
            Q(material__material_code__icontains=query)
        )
    
    if status:
        orders = orders.filter(status=status)
    
    paginator = Paginator(orders, 15)
    
    try:
        orders_page = paginator.page(page)
    except PageNotAnInteger:
        orders_page = paginator.page(1)
    except EmptyPage:
        orders_page = paginator.page(paginator.num_pages)
    
    return render(request, 'supply/purchase_order_list.html', {
        'orders': orders_page,
        'query': query,
        'status': status,
        'status_choices': [
            ('draft', '草稿'),
            ('issued', '已下达'),
            ('confirmed', '已确认'),
            ('partial', '部分到货'),
            ('completed', '已完成'),
            ('cancelled', '已取消')
        ],
        'paginator': paginator,
        'page_obj': orders_page
    })

@login_required
def purchase_order_add(request):
    """添加采购订单视图"""
    if request.method == 'POST':
        po_no = request.POST.get('po_no')
        supplier_id = request.POST.get('supplier')
        material_id = request.POST.get('material')
        quantity = request.POST.get('quantity')
        unit_price = request.POST.get('unit_price')
        order_date = request.POST.get('order_date')
        delivery_date = request.POST.get('delivery_date')
        remarks = request.POST.get('remarks')
        
        if PurchaseOrder.objects.filter(po_no=po_no).exists():
            messages.error(request, '采购订单号已存在')
            return redirect('purchase_order_add')
        
        supplier = get_object_or_404(Supplier, pk=supplier_id)
        material = get_object_or_404(Material, pk=material_id)
        
        PurchaseOrder.objects.create(
            po_no=po_no,
            supplier=supplier,
            material=material,
            quantity=quantity,
            unit_price=unit_price,
            order_date=order_date,
            delivery_date=delivery_date,
            remarks=remarks
        )
        
        messages.success(request, '采购订单添加成功')
        return redirect('purchase_order_list')
    
    suppliers = Supplier.objects.filter(is_active=True)
    materials = Material.objects.filter(is_active=True)
    return render(request, 'supply/purchase_order_form.html', {
        'title': '添加采购订单',
        'suppliers': suppliers,
        'materials': materials
    })

@login_required
def purchase_order_edit(request, pk):
    """编辑采购订单视图"""
    order = get_object_or_404(PurchaseOrder, pk=pk)
    
    if request.method == 'POST':
        order.quantity = request.POST.get('quantity')
        order.unit_price = request.POST.get('unit_price')
        order.delivery_date = request.POST.get('delivery_date')
        order.actual_delivery_date = request.POST.get('actual_delivery_date')
        order.status = request.POST.get('status')
        order.remarks = request.POST.get('remarks')
        order.save()
        
        messages.success(request, '采购订单更新成功')
        return redirect('purchase_order_list')
    
    suppliers = Supplier.objects.filter(is_active=True)
    materials = Material.objects.filter(is_active=True)
    return render(request, 'supply/purchase_order_form.html', {
        'title': '编辑采购订单',
        'order': order,
        'suppliers': suppliers,
        'materials': materials,
        'status_choices': [
            ('draft', '草稿'),
            ('issued', '已下达'),
            ('confirmed', '已确认'),
            ('partial', '部分到货'),
            ('completed', '已完成'),
            ('cancelled', '已取消')
        ]
    })

@login_required
def purchase_order_delete(request, pk):
    """删除采购订单视图"""
    order = get_object_or_404(PurchaseOrder, pk=pk)
    
    if request.method == 'POST':
        order.delete()
        messages.success(request, '采购订单删除成功')
        return redirect('purchase_order_list')
    
    return render(request, 'supply/purchase_order_confirm_delete.html', {
        'order': order
    })

# ========== 销售订单管理 ==========

@login_required
def sales_order_list(request):
    """销售订单列表视图"""
    query = request.GET.get('q', '')
    status = request.GET.get('status', '')
    page = request.GET.get('page', 1)
    
    orders = SalesOrder.objects.all().order_by('priority', 'demand_date')
    
    if query:
        orders = orders.filter(
            Q(order_no__icontains=query) |
            Q(customer_name__icontains=query) |
            Q(material__material_code__icontains=query)
        )
    
    if status:
        orders = orders.filter(status=status)
    
    paginator = Paginator(orders, 15)
    
    try:
        orders_page = paginator.page(page)
    except PageNotAnInteger:
        orders_page = paginator.page(1)
    except EmptyPage:
        orders_page = paginator.page(paginator.num_pages)
    
    return render(request, 'supply/sales_order_list.html', {
        'orders': orders_page,
        'query': query,
        'status': status,
        'status_choices': [
            ('pending', '待处理'),
            ('allocated', '已占料'),
            ('partial', '部分齐套'),
            ('complete', '完全齐套'),
            ('shipped', '已发货'),
            ('delivered', '已交付'),
            ('cancelled', '已取消')
        ],
        'paginator': paginator,
        'page_obj': orders_page
    })

@login_required
def sales_order_add(request):
    """添加销售订单视图"""
    if request.method == 'POST':
        order_no = request.POST.get('order_no')
        customer_name = request.POST.get('customer_name')
        material_id = request.POST.get('material')
        quantity = request.POST.get('quantity')
        demand_date = request.POST.get('demand_date')
        priority = request.POST.get('priority', 1)
        shipping_method = request.POST.get('shipping_method', 'sea')
        shipping_days = request.POST.get('shipping_days', 45)
        
        if SalesOrder.objects.filter(order_no=order_no).exists():
            messages.error(request, '销售订单号已存在')
            return redirect('sales_order_add')
        
        material = get_object_or_404(Material, pk=material_id)
        
        SalesOrder.objects.create(
            order_no=order_no,
            customer_name=customer_name,
            material=material,
            quantity=quantity,
            unit_price=float(request.POST.get('unit_price', 0)),
            demand_date=demand_date,
            priority=priority,
            order_date=request.POST.get('order_date'),
            shipping_method=shipping_method,
            shipping_days=shipping_days
        )
        
        messages.success(request, '销售订单添加成功')
        return redirect('sales_order_list')
    
    materials = Material.objects.filter(is_active=True, material_type='finished')
    return render(request, 'supply/sales_order_form.html', {
        'title': '添加销售订单',
        'materials': materials,
        'shipping_methods': [
            ('sea', '海运'),
            ('air', '空运'),
            ('land', '陆运')
        ]
    })

@login_required
def sales_order_edit(request, pk):
    """编辑销售订单视图"""
    order = get_object_or_404(SalesOrder, pk=pk)
    
    if request.method == 'POST':
        order.customer_name = request.POST.get('customer_name')
        order.quantity = request.POST.get('quantity')
        order.unit_price = float(request.POST.get('unit_price', order.unit_price or 0))
        order.demand_date = request.POST.get('demand_date')
        order.priority = request.POST.get('priority', 1)
        order.order_date = request.POST.get('order_date')
        order.shipping_method = request.POST.get('shipping_method', 'sea')
        order.shipping_days = request.POST.get('shipping_days', 45)
        order.status = request.POST.get('status')
        order.save()
        
        messages.success(request, '销售订单更新成功')
        return redirect('sales_order_list')
    
    materials = Material.objects.filter(is_active=True, material_type='finished')
    return render(request, 'supply/sales_order_form.html', {
        'title': '编辑销售订单',
        'order': order,
        'materials': materials,
        'shipping_methods': [
            ('sea', '海运'),
            ('air', '空运'),
            ('land', '陆运')
        ],
        'status_choices': [
            ('pending', '待处理'),
            ('allocated', '已占料'),
            ('partial', '部分齐套'),
            ('complete', '完全齐套'),
            ('shipped', '已发货'),
            ('delivered', '已交付'),
            ('cancelled', '已取消')
        ]
    })

@login_required
def sales_order_delete(request, pk):
    """删除销售订单视图"""
    order = get_object_or_404(SalesOrder, pk=pk)
    
    if request.method == 'POST':
        order.delete()
        messages.success(request, '销售订单删除成功')
        return redirect('sales_order_list')
    
    return render(request, 'supply/sales_order_confirm_delete.html', {
        'order': order
    })

# ========== 供应商承诺管理 ==========

@login_required
def commitment_list(request):
    """供应商承诺列表视图"""
    query = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    commitments = SupplierCommitment.objects.all().order_by('-delivery_date')
    
    if query:
        commitments = commitments.filter(
            Q(supplier__supplier_name__icontains=query) |
            Q(material__material_code__icontains=query) |
            Q(order_no__icontains=query)
        )
    
    paginator = Paginator(commitments, 15)
    
    try:
        commitments_page = paginator.page(page)
    except PageNotAnInteger:
        commitments_page = paginator.page(1)
    except EmptyPage:
        commitments_page = paginator.page(paginator.num_pages)
    
    return render(request, 'supply/commitment_list.html', {
        'commitments': commitments_page,
        'query': query,
        'paginator': paginator,
        'page_obj': commitments_page
    })