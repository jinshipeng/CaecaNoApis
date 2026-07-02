from django.db import models
from django.contrib.auth.models import User
from datetime import datetime


class Notification(models.Model):
    NOTIFICATION_TYPE_CHOICES = [
        ('info', '信息'),
        ('warning', '警告'),
        ('error', '错误'),
        ('success', '成功'),
    ]

    title = models.CharField(max_length=200, verbose_name='标题')
    message = models.TextField(verbose_name='消息内容')
    notification_type = models.CharField(
        max_length=20,
        choices=NOTIFICATION_TYPE_CHOICES,
        default='info',
        db_index=True,
        verbose_name='通知类型'
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        db_index=True,
        verbose_name='接收用户'
    )
    is_read = models.BooleanField(default=False, db_index=True, verbose_name='已读')
    link = models.CharField(max_length=500, blank=True, null=True, verbose_name='链接')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='创建时间')
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name='过期时间')

    class Meta:
        verbose_name = '通知消息'
        verbose_name_plural = '通知消息'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read'], name='notif_user_read_idx'),
            models.Index(fields=['notification_type', 'is_read'], name='notif_type_read_idx'),
            models.Index(fields=['user', 'created_at'], name='notif_user_created_idx'),
        ]

    def __str__(self):
        return self.title


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ('create', '创建'),
        ('update', '更新'),
        ('delete', '删除'),
        ('login', '登录'),
        ('logout', '登出'),
        ('export', '导出'),
        ('import', '导入'),
        ('run', '执行'),
        ('other', '其他'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        verbose_name='操作用户'
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True, verbose_name='操作类型')
    module = models.CharField(max_length=100, db_index=True, verbose_name='模块')
    target = models.CharField(max_length=200, blank=True, null=True, verbose_name='操作对象')
    detail = models.TextField(blank=True, null=True, verbose_name='操作详情')
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name='IP地址')
    user_agent = models.CharField(max_length=500, blank=True, null=True, verbose_name='用户代理')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='操作时间')

    class Meta:
        verbose_name = '审计日志'
        verbose_name_plural = '审计日志'
        ordering = ['-created_at']
        db_table = 'prediction_auditlog'
        indexes = [
            models.Index(fields=['user', 'created_at'], name='audit_user_created_idx'),
            models.Index(fields=['action', 'module'], name='audit_action_module_idx'),
            models.Index(fields=['module', 'created_at'], name='audit_module_created_idx'),
        ]

    def __str__(self):
        return f'{self.user} - {self.get_action_display()} - {self.module}'


class SystemLog(models.Model):
    LOG_LEVEL_CHOICES = [
        ('DEBUG', '调试'),
        ('INFO', '信息'),
        ('WARNING', '警告'),
        ('ERROR', '错误'),
        ('CRITICAL', '严重'),
    ]

    module = models.CharField(max_length=100, db_index=True, verbose_name='模块')
    level = models.CharField(
        max_length=20,
        choices=LOG_LEVEL_CHOICES,
        default='INFO',
        db_index=True,
        verbose_name='日志级别'
    )
    message = models.TextField(verbose_name='日志内容')
    exception = models.TextField(null=True, blank=True, verbose_name='异常信息')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='创建时间')
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        verbose_name='操作用户'
    )

    class Meta:
        verbose_name = '系统日志'
        verbose_name_plural = '系统日志'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['level', 'created_at'], name='syslog_level_created_idx'),
            models.Index(fields=['module', 'level'], name='syslog_module_level_idx'),
        ]

    def __str__(self):
        return f"[{self.level}] {self.module}: {self.message[:50]}"


class ErrorLog(models.Model):
    path = models.CharField(max_length=500, db_index=True, verbose_name='请求路径')
    method = models.CharField(max_length=10, db_index=True, verbose_name='请求方法')
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        verbose_name='用户'
    )
    error_type = models.CharField(max_length=100, db_index=True, verbose_name='错误类型')
    error_message = models.TextField(verbose_name='错误信息')
    stack_trace = models.TextField(null=True, blank=True, verbose_name='堆栈跟踪')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '错误日志'
        verbose_name_plural = '错误日志'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['error_type', 'created_at'], name='errorlog_type_created_idx'),
            models.Index(fields=['path', 'method'], name='errorlog_path_method_idx'),
        ]

    def __str__(self):
        return f"{self.error_type}: {self.error_message[:50]}"


class RequestLog(models.Model):
    path = models.CharField(max_length=500, db_index=True, verbose_name='请求路径')
    method = models.CharField(max_length=10, db_index=True, verbose_name='请求方法')
    status_code = models.IntegerField(db_index=True, verbose_name='状态码')
    duration = models.FloatField(verbose_name='响应时间(秒)')
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        verbose_name='用户'
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name='IP地址')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '请求日志'
        verbose_name_plural = '请求日志'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['method', 'status_code'], name='reqlog_method_status_idx'),
            models.Index(fields=['path', 'created_at'], name='reqlog_path_created_idx'),
            models.Index(fields=['status_code', 'created_at'], name='reqlog_status_created_idx'),
        ]

    def __str__(self):
        return f"{self.method} {self.path} - {self.status_code}"