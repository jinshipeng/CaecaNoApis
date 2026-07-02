from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.utils import timezone
from ..models import Notification, Material, Inventory, SalesOrder
from ..utils.notification_utils import NotificationManager


@login_required
def notification_list(request):
    notifications = Notification.objects.filter(user=request.user)
    notification_type = request.GET.get('type', '')
    if notification_type:
        notifications = notifications.filter(notification_type=notification_type)
    paginator = Paginator(notifications, 15)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    return render(request, 'notification_list.html', {
        'page_obj': page_obj,
        'notifications': page_obj,
        'unread_count': unread_count,
        'current_type': notification_type,
        'page_title': '消息中心',
        'page_subtitle': '查看系统通知和预警消息',
    })


@login_required
def notification_read(request, pk):
    if request.method == 'POST':
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        notification.is_read = True
        notification.save()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error'}, status=400)


@login_required
def notification_read_all(request):
    if request.method == 'POST':
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error'}, status=400)


@login_required
def notification_count(request):
    count = Notification.objects.filter(user=request.user, is_read=False).count()
    return JsonResponse({'count': count})


@login_required
def notification_check_alerts(request):
    now = timezone.now()
    low_stock_materials = Material.objects.filter(is_active=True)
    for material in low_stock_materials:
        total_qty = sum(
            inv.quantity for inv in Inventory.objects.filter(material=material)
        ) or 0
        if material.safety_stock and total_qty < material.safety_stock:
            existing = Notification.objects.filter(
                user=request.user,
                title__contains=f'库存预警: {material.material_code}',
                is_read=False,
            ).exists()
            if not existing:
                gap = material.safety_stock - total_qty
                NotificationManager.send_warning(
                    f'库存预警: {material.material_code}',
                    f'物料 {material.material_name}({material.material_code}) 库存不足，当前库存: {total_qty}件，安全库存: {material.safety_stock}件，缺口: {gap}件',
                    user=request.user,
                )
    overdue_orders = SalesOrder.objects.filter(
        demand_date__lt=now.date(),
        status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing'],
    )
    for order in overdue_orders:
        existing = Notification.objects.filter(
            user=request.user,
            title__contains=f'订单逾期: {order.order_no}',
            is_read=False,
        ).exists()
        if not existing:
            days_overdue = (now.date() - order.demand_date).days
            NotificationManager.send_error(
                f'订单逾期: {order.order_no}',
                f'销售订单 {order.order_no}(客户: {order.customer_name}) 已逾期{days_overdue}天，需求日期: {order.demand_date}',
                user=request.user,
            )
    return JsonResponse({'status': 'success'})
