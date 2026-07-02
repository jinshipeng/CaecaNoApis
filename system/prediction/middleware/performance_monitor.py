import time
import logging

logger = logging.getLogger(__name__)

class PerformanceMonitorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = True

    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)
        
        start_time = time.time()
        
        response = self.get_response(request)
        
        duration = time.time() - start_time
        status_code = response.status_code
        method = request.method
        endpoint = self.get_endpoint(request)
        
        # 记录性能日志
        response['X-Request-Time'] = f'{duration:.4f}s'
        
        # 慢请求警告（超过2秒）
        if duration > 2.0:
            logger.warning(f'Slow request: {method} {endpoint} took {duration:.2f}s')
        
        return response

    def get_endpoint(self, request):
        path = request.path
        path_parts = path.split('/')
        clean_parts = []
        for part in path_parts:
            if part and not part.isdigit():
                clean_parts.append(part)
            elif part.isdigit():
                clean_parts.append('{id}')
        return '/'.join(clean_parts)