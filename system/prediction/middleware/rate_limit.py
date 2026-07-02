import time
import logging
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.limits = {
            'api': {'requests': 10000, 'seconds': 60},
            'login': {'requests': 60, 'seconds': 60},
            'default': {'requests': 5000, 'seconds': 60},
        }
        # 进程内内存降级缓存（当Redis不可用时使用）
        self._memory_cache = {}
        self._redis_available = None  # None=未检测, True=可用, False=不可用

    def _check_redis_available(self) -> bool:
        """检测Redis是否可用"""
        if self._redis_available is not None:
            return self._redis_available
        try:
            cache.set('__health_check__', 'ok', 10)
            result = cache.get('__health_check__')
            self._redis_available = (result == 'ok')
            if not self._redis_available:
                logger.warning('Redis不可用，RateLimit将降级为内存模式')
        except Exception as e:
            self._redis_available = False
            logger.warning(f'Redis连接失败: {e}，RateLimit将降级为内存模式')
        return self._redis_available

    def _get_from_cache(self, key, default):
        """带降级的缓存读取"""
        if self._check_redis_available():
            return cache.get(key, default)
        # 内存降级
        return self._memory_cache.get(key, default)

    def _set_to_cache(self, key, value, timeout):
        """带降级的缓存写入"""
        if self._check_redis_available():
            try:
                cache.set(key, value, timeout)
            except Exception as e:
                logger.warning(f'Redis写入失败，降级到内存: {e}')
                self._redis_available = False
                self._memory_cache[key] = value
        else:
            self._memory_cache[key] = value

    def __call__(self, request):
        # 开发环境禁用限流（避免 429 阻断前端调试）
        from django.conf import settings
        if getattr(settings, 'DEBUG', False):
            return self.get_response(request)

        client_ip = self.get_client_ip(request)
        path = request.path
        limit_key = self.get_limit_key(path)
        limit = self.limits.get(limit_key, self.limits['default'])

        cache_key = f'rate_limit:{client_ip}:{limit_key}'

        try:
            current = self._get_from_cache(cache_key, {'count': 0, 'timestamp': time.time()})

            now = time.time()
            if now - current['timestamp'] > limit['seconds']:
                current = {'count': 1, 'timestamp': now}
            else:
                if current['count'] >= limit['requests']:
                    return JsonResponse({
                        'error': 'Too Many Requests',
                        'message': f'Rate limit exceeded. Try again in {limit["seconds"]} seconds.',
                        'status': 429
                    }, status=429)
                current['count'] += 1

            self._set_to_cache(cache_key, current, limit['seconds'])
        except Exception as e:
            # 任何意外错误都不应阻断请求，记录日志后放行
            logger.warning(f'RateLimit中间件异常，放行请求: {e}')

        response = self.get_response(request)

        try:
            response['X-RateLimit-Limit'] = limit['requests']
            response['X-RateLimit-Remaining'] = max(0, limit['requests'] - current.get('count', 0))
        except Exception:
            pass

        return response

    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR')

    def get_limit_key(self, path):
        if '/api/' in path:
            return 'api'
        if '/login/' in path:
            return 'login'
        return 'default'
