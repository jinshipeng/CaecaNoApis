import time
import logging
from django.conf import settings

logger = logging.getLogger('prediction.request')

class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.threshold = getattr(settings, 'SLOW_REQUEST_THRESHOLD', 2.0)

    def __call__(self, request):
        start_time = time.time()

        logger.debug(
            f"--> {request.method} {request.path} "
            f"| User: {request.user if request.user.is_authenticated else 'anonymous'}"
        )

        response = self.get_response(request)

        duration = (time.time() - start_time) * 1000

        log_level = logging.WARNING if duration > self.threshold * 1000 else logging.DEBUG
        logger.log(
            log_level,
            f"<-- {request.method} {request.path} "
            f"| {response.status_code} | {duration:.1f}ms"
        )

        return response
