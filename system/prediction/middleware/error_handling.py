import logging
import traceback
import time
import uuid
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.template import loader
from django.utils.deprecation import MiddlewareMixin
from django.db import DatabaseError, IntegrityError
from django.core.exceptions import PermissionDenied, ValidationError

logger = logging.getLogger(__name__)


class ErrorHandlingMiddleware(MiddlewareMixin):
    """全局异常处理中间件 - 增强版"""
    
    # 异常类型与HTTP状态码映射
    EXCEPTION_STATUS_CODES = {
        'PermissionDenied': 403,
        'ValidationError': 400,
        'DatabaseError': 500,
        'IntegrityError': 409,
        'ValueError': 400,
        'KeyError': 400,
        'TypeError': 400,
        'IndexError': 404,
        'DoesNotExist': 404,
    }
    
    # 敏感信息过滤字段（不记录到日志）
    SENSITIVE_FIELDS = ['password', 'token', 'secret', 'key', 'credit_card']
    
    def process_exception(self, request, exception):
        """处理所有未捕获的异常"""
        
        # 生成请求追踪ID
        trace_id = str(uuid.uuid4())[:8]
        
        # 记录请求上下文
        self._log_exception(request, exception, trace_id)
        
        # 根据请求类型返回不同的响应格式
        if self._is_api_request(request):
            return self._handle_api_error(request, exception, trace_id)
        else:
            return self._handle_page_error(request, exception, trace_id)
    
    def _is_api_request(self, request):
        """判断是否为API请求"""
        path = request.path.lower()
        api_indicators = ['/api/', '/api-', 'application/json', 'text/xml']
        return any(indicator in path for indicator in api_indicators[:2]) or \
               request.content_type in api_indicators[2:]
    
    def _log_exception(self, request, exception, trace_id):
        """记录异常日志（脱敏处理）"""
        exc_type = type(exception).__name__
        status_code = self.EXCEPTION_STATUS_CODES.get(exc_type, 500)
        
        # 构建安全的请求数据（过滤敏感信息）
        safe_request_data = self._sanitize_request_data(request)
        
        # 日志级别根据状态码确定
        log_method = logger.error if status_code >= 500 else logger.warning
        
        log_method(
            f"[{trace_id}] {exc_type}: {str(exception)} | "
            f"Path: {request.path} | "
            f"Method: {request.method} | "
            f"User: {request.user if request.user.is_authenticated else 'Anonymous'} | "
            f"Status: {status_code} | "
            f"Data: {safe_request_data}",
            extra={
                'trace_id': trace_id,
                'exception_type': exc_type,
                'path': request.path,
                'method': request.method,
                'status_code': status_code,
            }
        )
        
        # 对于服务器错误，记录完整堆栈
        if status_code >= 500:
            logger.error(f"[{trace_id}] Full traceback:\n{traceback.format_exc()}")
    
    def _sanitize_request_data(self, request):
        """清理敏感数据"""
        data = {}
        
        try:
            # GET参数
            if request.GET:
                data['GET'] = dict(request.GET)
            
            # POST数据
            if request.method == 'POST' and hasattr(request, 'POST'):
                post_data = dict(request.POST)
                for field in self.SENSITIVE_FIELDS:
                    if field in post_data:
                        post_data[field] = '***REDACTED***'
                data['POST'] = post_data
            
            # JSON body
            if hasattr(request, 'body') and request.body:
                try:
                    import json
                    body = json.loads(request.body)
                    for field in self.SENSITIVE_FIELDS:
                        if field in body:
                            body[field] = '***REDACTED***'
                    data['BODY'] = body
                except:
                    pass
                    
        except Exception as e:
            logger.warning(f'Failed to sanitize request data: {e}')
        
        return data
    
    def _handle_api_error(self, request, exception, trace_id):
        """处理API请求的错误响应"""
        exc_type = type(exception).__name__
        status_code = self.EXCEPTION_STATUS_CODES.get(exc_type, 500)
        
        response_data = {
            'success': False,
            'error': {
                'type': exc_type,
                'message': self._get_user_friendly_message(exception),
                'code': status_code,
                'trace_id': trace_id
            },
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ')
        }
        
        return JsonResponse(response_data, status=status_code)
    
    def _handle_page_error(self, request, exception, trace_id):
        """处理页面请求的错误响应"""
        exc_type = type(exception).__name__
        status_code = self.EXCEPTION_STATUS_CODES.get(exc_type, 500)
        
        # 尝试渲染自定义错误页面
        try:
            template = loader.get_template(f'errors/{status_code}.html')
            context = {
                'exception': exception,
                'exception_type': exc_type,
                'message': self._get_user_friendly_message(exception),
                'trace_id': trace_id,
                'status_code': status_code
            }
            return HttpResponse(template.render(context, request), status=status_code)
        except Exception:
            pass
        
        # 使用默认错误页面
        error_pages = {
            404: self._render_404,
            403: self._render_403,
            500: self._render_500
        }
        
        renderer = error_pages.get(status_code, self._render_500)
        return renderer(request, exception, trace_id, status_code)
    
    def _get_user_friendly_message(self, exception):
        """获取用户友好的错误消息"""
        messages = {
            'PermissionDenied': '您没有权限执行此操作',
            'ValidationError': '提交的数据验证失败',
            'DatabaseError': '数据库操作失败，请稍后重试',
            'IntegrityError': '数据完整性冲突',
            'DoesNotExist': '请求的资源不存在',
        }
        
        exc_type = type(exception).__name__
        base_message = messages.get(exc_type, '系统发生未知错误')
        
        # 开发环境显示详细错误信息
        if settings.DEBUG:
            return f"{base_message}: {str(exception)}"
        
        return base_message
    
    def _render_404(self, request, exception, trace_id, status_code=404):
        """渲染404页面"""
        html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>404 - 页面不存在</title>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    font-family: 'Microsoft YaHei', sans-serif;
                    background: linear-gradient(135deg, #010610 0%, #050d1a 100%);
                    color: #e2e8f0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                    text-align: center;
                }}
                .container {{
                    max-width: 600px;
                    padding: 40px;
                }}
                h1 {{
                    font-size: 120px;
                    background: linear-gradient(135deg, #60a5fa, #8b5cf6);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    text-shadow: 0 0 60px rgba(96, 165, 250, 0.5);
                    margin-bottom: 20px;
                }}
                h2 {{
                    font-size: 24px;
                    margin-bottom: 20px;
                    color: #ffffff;
                }}
                p {{
                    color: #94a3b8;
                    line-height: 1.6;
                    margin-bottom: 30px;
                }}
                a {{
                    display: inline-block;
                    padding: 12px 30px;
                    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
                    color: white;
                    text-decoration: none;
                    border-radius: 25px;
                    transition: all 0.3s ease;
                }}
                a:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 10px 30px rgba(59, 130, 246, 0.4);
                }}
                .trace-id {{
                    font-family: monospace;
                    font-size: 12px;
                    color: #64748b;
                    margin-top: 20px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>404</h1>
                <h2>页面不存在</h2>
                <p>抱歉，您访问的页面不存在或已被移除。</p>
                <a href="/">返回首页</a>
                <div class="trace-id">Trace ID: {trace_id}</div>
            </div>
        </body>
        </html>
        '''
        return HttpResponse(html, status=status_code)
    
    def _render_403(self, request, exception, trace_id, status_code=403):
        """渲染403页面"""
        html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>403 - 禁止访问</title>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    font-family: 'Microsoft YaHei', sans-serif;
                    background: linear-gradient(135deg, #010610 0%, #050d1a 100%);
                    color: #e2e8f0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                    text-align: center;
                }}
                .container {{
                    max-width: 600px;
                    padding: 40px;
                }}
                h1 {{
                    font-size: 120px;
                    background: linear-gradient(135deg, #f59e0b, #ef4444);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    text-shadow: 0 0 60px rgba(245, 158, 11, 0.5);
                    margin-bottom: 20px;
                }}
                h2 {{
                    font-size: 24px;
                    margin-bottom: 20px;
                    color: #ffffff;
                }}
                p {{
                    color: #94a3b8;
                    line-height: 1.6;
                    margin-bottom: 30px;
                }}
                a {{
                    display: inline-block;
                    padding: 12px 30px;
                    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
                    color: white;
                    text-decoration: none;
                    border-radius: 25px;
                    transition: all 0.3s ease;
                }}
                a:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 10px 30px rgba(59, 130, 246, 0.4);
                }}
                .trace-id {{
                    font-family: monospace;
                    font-size: 12px;
                    color: #64748b;
                    margin-top: 20px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>403</h1>
                <h2>禁止访问</h2>
                <p>您没有权限访问此页面。请联系管理员获取相应权限。</p>
                <a href="/">返回首页</a>
                <div class="trace-id">Trace ID: {trace_id}</div>
            </div>
        </body>
        </html>
        '''
        return HttpResponse(html, status=status_code)
    
    def _render_500(self, request, exception, trace_id, status_code=500):
        """渲染500页面"""
        html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>500 - 服务器错误</title>
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                body {{
                    font-family: 'Microsoft YaHei', sans-serif;
                    background: linear-gradient(135deg, #010610 0%, #050d1a 100%);
                    color: #e2e8f0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                    text-align: center;
                }}
                .container {{
                    max-width: 600px;
                    padding: 40px;
                }}
                h1 {{
                    font-size: 120px;
                    background: linear-gradient(135deg, #ef4444, #dc2626);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    text-shadow: 0 0 60px rgba(239, 68, 68, 0.5);
                    margin-bottom: 20px;
                }}
                h2 {{
                    font-size: 24px;
                    margin-bottom: 20px;
                    color: #ffffff;
                }}
                p {{
                    color: #94a3b8;
                    line-height: 1.6;
                    margin-bottom: 30px;
                }}
                a {{
                    display: inline-block;
                    padding: 12px 30px;
                    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
                    color: white;
                    text-decoration: none;
                    border-radius: 25px;
                    transition: all 0.3s ease;
                }}
                a:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 10px 30px rgba(59, 130, 246, 0.4);
                }}
                .trace-id {{
                    font-family: monospace;
                    font-size: 12px;
                    color: #64748b;
                    margin-top: 20px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>500</h1>
                <h2>服务器内部错误</h2>
                <p>服务器遇到了一个意外情况，无法完成您的请求。我们的技术团队已收到通知。</p>
                <a href="/">返回首页</a>
                <div class="trace-id">Trace ID: {trace_id}</div>
            </div>
        </body>
        </html>
        '''
        return HttpResponse(html, status=status_code)