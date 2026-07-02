import logging
from django.utils.deprecation import MiddlewareMixin
from django.urls import resolve
from prediction.models import AuditLog

logger = logging.getLogger(__name__)


class AuditLogMiddleware(MiddlewareMixin):

    SKIP_PATHS = ['/static/', '/admin/', '/favicon.ico', '/captcha/']

    ACTION_MAP = {
        'POST': 'create',
        'PUT': 'update',
        'PATCH': 'update',
        'DELETE': 'delete',
    }

    MODULE_MAP = {
        'material': '物料管理',
        'supplier': '供应商管理',
        'customer': '客户管理',
        'bom': 'BOM管理',
        'inventory': '库存管理',
        'purchase': '采购订单',
        'sales': '销售订单',
        'commitment': '供应商承诺',
        'capacity': '产能管理',
        'scheduling': '排产计划',
        'import_data': '数据导入',
        'data_init': '数据初始化',
        'plan': '生产计划',
        'report': '报表中心',
        'audit': '审计日志',
    }

    def _should_skip(self, path):
        for skip_path in self.SKIP_PATHS:
            if path.startswith(skip_path):
                return True
        return False

    def _get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip

    def _get_module(self, path):
        path_parts = path.strip('/').split('/')
        for part in path_parts:
            for key, value in self.MODULE_MAP.items():
                if key in part:
                    return value
        return '系统'

    def _get_target(self, request, path):
        target = ''
        if hasattr(request, 'resolver_match') and request.resolver_match:
            target = request.resolver_match.view_name or ''
        path_parts = path.strip('/').split('/')
        if len(path_parts) > 1:
            target = '/'.join(path_parts[-2:])
        return target[:200] if target else path[:200]

    def _get_detail(self, request, action):
        method = request.method
        detail_parts = [f'{method}请求']
        if action == 'login':
            detail_parts = ['用户登录']
        elif action == 'logout':
            detail_parts = ['用户登出']
        elif action == 'create':
            detail_parts = ['创建操作']
        elif action == 'update':
            detail_parts = ['更新操作']
        elif action == 'delete':
            detail_parts = ['删除操作']

        # 优先从JSON请求体提取关键字段（DRF API使用JSON）
        try:
            body_data = getattr(request, 'data', None) or {}
            if not body_data and hasattr(request, 'body') and request.body:
                import json
                try:
                    body_data = json.loads(request.body)
                except (json.JSONDecodeError, TypeError):
                    body_data = {}

            safe_fields = ['name', 'code', 'title', 'status', 'number',
                           'material_code', 'supplier_code', 'customer_code',
                           'order_no', 'po_no', 'material_name', 'quantity']
            if isinstance(body_data, dict):
                for field in safe_fields:
                    val = body_data.get(field)
                    if val is not None:
                        detail_parts.append(f'{field}={val}')
                        break
        except Exception:
            pass

        # 回退到表单数据（传统Django表单提交）
        if len(detail_parts) <= 1 and hasattr(request, 'POST'):
            for field in ['name', 'code', 'title', 'status', 'number',
                          'material_code', 'supplier_code', 'customer_code']:
                if field in request.POST:
                    detail_parts.append(f'{field}={request.POST[field]}')
                    break
        return ' '.join(detail_parts)[:500]

    def _create_audit_log(self, request, action, module=None, target=None, detail=None):
        try:
            user = request.user if request.user.is_authenticated else None
            AuditLog.objects.create(
                user=user,
                action=action,
                module=module or self._get_module(request.path),
                target=target or self._get_target(request, request.path),
                detail=detail or self._get_detail(request, action),
                ip_address=self._get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            )
        except Exception as e:
            logger.error(f'Failed to create audit log: {e}')

    def process_request(self, request):
        if self._should_skip(request.path):
            return None

        if request.method == 'GET':
            return None

        if request.path.strip('/') == 'login' and request.method == 'POST':
            return None

        return None

    def process_response(self, request, response):
        if self._should_skip(request.path):
            return response

        if request.method == 'GET':
            return response

        if request.path.strip('/') == 'login' and request.method == 'POST':
            if response.status_code in (200, 302) and request.user.is_authenticated:
                self._create_audit_log(
                    request,
                    action='login',
                    module='系统',
                    target='登录',
                    detail=f'用户 {request.user.username} 登录系统'
                )
            return response

        if request.path.strip('/') == 'logout':
            if request.user.is_authenticated:
                self._create_audit_log(
                    request,
                    action='logout',
                    module='系统',
                    target='登出',
                    detail=f'用户 {request.user.username} 登出系统'
                )
            return response

        action = self.ACTION_MAP.get(request.method, 'other')
        path = request.path.lower()

        if 'export' in path:
            action = 'export'
        elif 'import' in path or 'batch_import' in path:
            action = 'import'
        elif 'run' in path:
            action = 'run'

        if response.status_code < 400:
            self._create_audit_log(request, action=action)

        return response
