from django.contrib.auth.models import User
from ..models import Notification, SystemLog
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class NotificationManager:
    """通知管理器"""

    @staticmethod
    def create_notification(title, message, notification_type='info', user=None, expires_days=7):
        """创建通知"""
        expires_at = datetime.now() + timedelta(days=expires_days) if expires_days else None
        
        notification = Notification.objects.create(
            title=title,
            message=message,
            notification_type=notification_type,
            user=user,
            expires_at=expires_at
        )
        
        logger.info(f"通知已创建: {title}")
        return notification

    @staticmethod
    def send_info(title, message, user=None):
        """发送信息通知"""
        return NotificationManager.create_notification(title, message, 'info', user)

    @staticmethod
    def send_warning(title, message, user=None):
        """发送警告通知"""
        return NotificationManager.create_notification(title, message, 'warning', user)

    @staticmethod
    def send_error(title, message, user=None):
        """发送错误通知"""
        return NotificationManager.create_notification(title, message, 'error', user)

    @staticmethod
    def send_success(title, message, user=None):
        """发送成功通知"""
        return NotificationManager.create_notification(title, message, 'success', user)

    @staticmethod
    def send_to_all_users(title, message, notification_type='info'):
        """发送通知给所有用户"""
        notifications = []
        for user in User.objects.all():
            notification = NotificationManager.create_notification(
                title, message, notification_type, user
            )
            notifications.append(notification)
        return notifications

    @staticmethod
    def get_unread_count(user):
        """获取未读通知数量"""
        return Notification.objects.filter(user=user, is_read=False).count()

    @staticmethod
    def mark_as_read(notification_ids):
        """标记通知为已读"""
        Notification.objects.filter(id__in=notification_ids).update(is_read=True)

    @staticmethod
    def cleanup_expired():
        """清理过期通知"""
        count = Notification.objects.filter(expires_at__lt=datetime.now()).delete()[0]
        logger.info(f"已清理 {count} 条过期通知")
        return count


class LogManager:
    """日志管理器"""

    @staticmethod
    def log(module, message, level='INFO', exception=None, user=None):
        """记录日志"""
        SystemLog.objects.create(
            module=module,
            level=level,
            message=message,
            exception=str(exception) if exception else None,
            user=user
        )
        
        # 同时记录到Python日志
        log_method = getattr(logger, level.lower(), logger.info)
        log_method(f"[{module}] {message}")

    @staticmethod
    def debug(module, message, user=None):
        """记录调试日志"""
        LogManager.log(module, message, 'DEBUG', user=user)

    @staticmethod
    def info(module, message, user=None):
        """记录信息日志"""
        LogManager.log(module, message, 'INFO', user=user)

    @staticmethod
    def warning(module, message, user=None):
        """记录警告日志"""
        LogManager.log(module, message, 'WARNING', user=user)

    @staticmethod
    def error(module, message, exception=None, user=None):
        """记录错误日志"""
        LogManager.log(module, message, 'ERROR', exception=exception, user=user)

    @staticmethod
    def critical(module, message, exception=None, user=None):
        """记录严重日志"""
        LogManager.log(module, message, 'CRITICAL', exception=exception, user=user)

    @staticmethod
    def get_recent_logs(limit=100, level=None):
        """获取最近的日志"""
        queryset = SystemLog.objects.all().order_by('-created_at')
        if level:
            queryset = queryset.filter(level=level)
        return queryset[:limit]

    @staticmethod
    def get_logs_by_module(module, limit=100):
        """按模块获取日志"""
        return SystemLog.objects.filter(module=module).order_by('-created_at')[:limit]