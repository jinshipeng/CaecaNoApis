"""
Django 生产环境设置

基于 settings.py 的生产环境配置，包含增强的安全设置和性能优化。
使用方法: python manage.py runserver --settings=bp_prediction_system.settings_prod
"""

from .settings import *

# ========== 生产环境基础配置 ==========
DEBUG = False
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get('ALLOWED_HOSTS', '').split(',')
    if h.strip()
] or ['localhost', '127.0.0.1']

# 确保生产环境不允许调试模式
if DEBUG:
    raise ValueError("生产环境禁止开启 DEBUG 模式！")

# ========== 增强安全配置 ==========
# HSTS (HTTP Strict Transport Security)
SECURE_HSTS_SECONDS = 31536000  # 1年
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# 安全头
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

# Cookie 设置
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 86400  # 24小时

# X-Frame-Options
X_FRAME_OPTIONS = 'DENY'  # 生产环境完全禁止嵌入

# 安全中间件（确保在列表最前面）
MIDDLEWARE.insert(0, 'django.middleware.security.SecurityMiddleware')

# ========== 数据库配置优化 ==========
# 生产环境使用 PostgreSQL 或 MySQL（示例使用 PostgreSQL）
DATABASES = {
    "default": {
        "ENGINE": os.environ.get('DB_ENGINE', 'django.db.backends.postgresql'),
        "NAME": get_env_variable('DB_NAME'),
        "USER": get_env_variable('DB_USER'),
        "PASSWORD": get_env_variable('DB_PASSWORD'),
        "HOST": get_env_variable('DB_HOST', 'localhost'),
        "PORT": get_env_variable('DB_PORT', '5432'),
        "CONN_MAX_AGE": 60,  # 连接池
        "OPTIONS": {
            "connect_timeout": 10,
            "options": "-c statement_timeout=30000",  # 30秒查询超时
        },
    }
}

# 数据库连接池配置（如果使用 django-db-geventpool 或类似）
# DATABASES['default']['ENGINE'] = 'django_db_geventpool.backends.postgresql_psycopg2'
# DATABASES['default']['OPTIONS'] = {
#     'MAX_CONNS': 20,
#     'REUSE_CONNS': 10,
# }

# ========== 缓存配置（Redis）==========
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": get_env_variable('REDIS_URL', 'redis://127.0.0.1:6379/1'),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "CONNECTION_POOL_KWARGS": {"max_connections": 50},
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
        },
        "KEY_PREFIX": "bp_prediction",
    }
}

# Session 后端使用缓存
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# ========== 静态文件配置 ==========
# 使用 WhiteNoise 或 CDN
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# 或者使用 CDN
# STATIC_URL = f"https://{os.environ.get('CDN_DOMAIN')}/static/"

# ========== 日志配置（生产环境）==========
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'json': {
            '()': 'pythonjsonlogger.jsonlogger.JsonFormatter',
            'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
        },
        'detailed': {
            'format': '[{asctime}] [{levelname}] [{name}:{lineno}] - {message}',
            'datefmt': '%Y-%m-%d %H:%M:%S',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'django.log',
            'maxBytes': 50 * 1024 * 1024,  # 50MB
            'backupCount': 10,
            'formatter': 'detailed',
            'encoding': 'utf-8',
        },
        'error_file': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'errors.log',
            'maxBytes': 50 * 1024 * 1024,  # 50MB
            'backupCount': 10,
            'formatter': 'detailed',
            'encoding': 'utf-8',
        },
        'audit_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'audit.log',
            'maxBytes': 100 * 1024 * 1024,  # 100MB
            'backupCount': 20,
            'formatter': 'detailed',
            'encoding': 'utf-8',
        },
        # Sentry 错误追踪（可选）
        # 'sentry': {
        #     'level': 'ERROR',
        #     'class': 'raven.contrib.django.handlers.SentryHandler',
        # },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['error_file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django.db.backends': {
            'level': 'WARNING',
            'handlers': ['file'],
            'propagate': False,
        },
        'prediction': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'prediction.audit': {
            'handlers': ['audit_file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# 确保 logs 目录存在
import logging
(BASE_DIR / 'logs').mkdir(exist_ok=True)

# ========== REST Framework 生产配置 ==========
REST_FRAMEWORK.update({
    'DEFAULT_THROTTLE_RATES': {
        'anon': '60/hour',
        'user': '500/hour',
    },
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
})

# ========== CORS 生产配置 ==========
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = [
    origin.strip() for origin in os.environ.get(
        'CORS_ALLOWED_ORIGINS',
        ''
    ).split(',') if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

# CSRF 配置
CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in os.environ.get(
        'CSRF_TRUSTED_ORIGINS',
        ''
    ).split(',') if origin.strip()
]

# ========== 性能优化 ==========
# 连接保持时间
CONN_MAX_AGE = 60

# Email 配置（生产环境使用真实邮件服务）
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = get_env_variable('EMAIL_HOST')
EMAIL_PORT = int(get_env_variable('EMAIL_PORT', '587'))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = get_env_variable('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = get_env_variable('EMAIL_HOST_PASSWORD')
DEFAULT_FROM_EMAIL = get_env_variable('DEFAULT_FROM_EMAIL', 'noreply@example.com')

# ========== 监控和健康检查 ==========
# 健康检查端点（可选）
# INSTALLED_APPS.append('health_check')
# INSTALLED_APPS.extend([
#     'health_check.db',
#     'health_check.cache',
# ])

# ========== 文件上传配置 ==========
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024   # 10MB

# Media 文件存储（可使用云存储如 AWS S3、阿里 OSS）
# DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'

# ========== 安全警告 ==========
# 禁用 Django 调试工具栏
if 'debug_toolbar' in INSTALLED_APPS:
    INSTALLED_APPS.remove('debug_toolbar')

if 'debug_toolbar.middleware.DebugToolbarMiddleware' in MIDDLEWARE:
    MIDDLEWARE.remove('debug_toolbar.middleware.DebugToolbarMiddleware')

print("⚠️  生产环境配置已加载")
print(f"✅ DEBUG = {DEBUG}")
print(f"✅ ALLOWED_HOSTS = {ALLOWED_HOSTS}")
print(f"✅ 数据库: {DATABASES['default']['ENGINE']}")
print(f"⚠️  请确保已正确配置所有环境变量")
