from django.utils.deprecation import MiddlewareMixin


class DisableCSRFMiddleware(MiddlewareMixin):
    """仅对API路径禁用CSRF，其他路径保持CSRF保护"""
    def process_request(self, request):
        # 仅对 /api/ 路径禁用CSRF（API使用Token认证）
        if request.path.startswith('/api/'):
            setattr(request, '_dont_enforce_csrf_checks', True)
