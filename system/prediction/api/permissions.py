from rest_framework import permissions
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

class IsAdminUser(permissions.BasePermission):
    """管理员权限"""
    def has_permission(self, request, view):
        return request.user and request.user.is_superuser

class IsManagerUser(permissions.BasePermission):
    """经理权限"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        return request.user.groups.filter(name='manager').exists()

class IsReadOnly(permissions.BasePermission):
    """只读权限"""
    def has_permission(self, request, view):
        return request.method in permissions.SAFE_METHODS

class HasModelPermission(permissions.BasePermission):
    """基于模型的细粒度权限控制"""
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        model_name = getattr(view, 'model_name', None)
        if not model_name:
            return True
        
        action_map = {
            'GET': 'view',
            'POST': 'add',
            'PUT': 'change',
            'PATCH': 'change',
            'DELETE': 'delete'
        }
        
        action = action_map.get(request.method)
        if not action:
            return True
        
        try:
            content_type = ContentType.objects.get(model=model_name.lower())
            permission = Permission.objects.get(
                content_type=content_type,
                codename=f'{action}_{model_name.lower()}'
            )
            return request.user.has_perm(f'{content_type.app_label}.{permission.codename}')
        except (ContentType.DoesNotExist, Permission.DoesNotExist):
            return True

class OrderManagementPermission(permissions.BasePermission):
    """订单管理权限"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        allowed_groups = ['manager', 'order_manager', 'planning_manager']
        return request.user.groups.filter(name__in=allowed_groups).exists()

class InventoryManagementPermission(permissions.BasePermission):
    """库存管理权限"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        allowed_groups = ['manager', 'inventory_manager', 'warehouse_staff']
        return request.user.groups.filter(name__in=allowed_groups).exists()

class SupplierManagementPermission(permissions.BasePermission):
    """供应商管理权限"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        allowed_groups = ['manager', 'procurement_manager', 'supplier_manager']
        return request.user.groups.filter(name__in=allowed_groups).exists()

class PlanningPermission(permissions.BasePermission):
    """物料计划权限"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        allowed_groups = ['manager', 'planning_manager', 'production_manager']
        return request.user.groups.filter(name__in=allowed_groups).exists()

class SystemManagementPermission(permissions.BasePermission):
    """系统管理权限"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        allowed_groups = ['manager', 'system_admin']
        return request.user.groups.filter(name__in=allowed_groups).exists()

class ReadOnlyOrAdmin(permissions.BasePermission):
    """只读或管理员权限"""
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user and request.user.is_superuser