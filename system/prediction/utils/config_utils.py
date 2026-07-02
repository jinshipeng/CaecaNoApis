"""系统配置管理工具"""
from django.db import models
from django.core.cache import cache
from datetime import datetime
import json
import logging
from .safe_cache import safe_get, safe_set

logger = logging.getLogger(__name__)


class SystemConfig:
    
    CONFIG_KEYS = [
        'inventory_consumption_priority',
        'default_shipping_days',
        'safety_stock_factor',
        'shortage_alert_threshold',
        'email_notification_enabled',
        'daily_report_recipients',
        'auto_run_planning',
        'planning_time_window',
    ]
    
    DEFAULT_CONFIG = {
        'inventory_consumption_priority': 'FIFO',
        'default_shipping_days': 45,
        'safety_stock_factor': 0.2,
        'shortage_alert_threshold': 0.2,
        'email_notification_enabled': False,
        'daily_report_recipients': [],
        'auto_run_planning': False,
        'planning_time_window': '02:00',
    }

    @staticmethod
    def get(key, default=None):
        cache_key = f'sysconfig_{key}'
        value = safe_get(cache_key)
        
        if value is None:
            try:
                from ..models.base_models import SystemConfigModel
                config = SystemConfigModel.objects.get(key=key)
                value = json.loads(config.value) if config.value else None
                safe_set(cache_key, value, timeout=3600)
            except Exception:
                value = SystemConfig.DEFAULT_CONFIG.get(key, default)
        
        return value

    @staticmethod
    def set(key, value):
        if key not in SystemConfig.CONFIG_KEYS:
            raise ValueError(f"Invalid config key: {key}")
        
        cache_key = f'sysconfig_{key}'
        
        try:
            from ..models.base_models import SystemConfigModel
            config, created = SystemConfigModel.objects.update_or_create(
                key=key,
                defaults={'value': json.dumps(value), 'updated_at': datetime.now()}
            )
            safe_set(cache_key, value, timeout=3600)
            logger.info(f"System config updated: {key} = {value}")
            return True
        except Exception as e:
            logger.error(f"Failed to update system config: {key}, error: {str(e)}")
            return False

    @staticmethod
    def get_all():
        configs = {}
        for key in SystemConfig.CONFIG_KEYS:
            configs[key] = SystemConfig.get(key)
        return configs

    @staticmethod
    def reset_to_defaults():
        try:
            for key, value in SystemConfig.DEFAULT_CONFIG.items():
                SystemConfig.set(key, value)
            logger.info("All system configs reset to defaults")
            return True
        except Exception as e:
            logger.error(f"Failed to reset system config: {str(e)}")
            return False


class HealthChecker:

    @staticmethod
    def check_database():
        try:
            from django.db import connection
            with connection.cursor():
                pass
            return {'status': 'healthy', 'message': 'Database connection OK'}
        except Exception as e:
            return {'status': 'unhealthy', 'message': f'Database connection failed: {str(e)}'}

    @staticmethod
    def check_cache():
        try:
            safe_set('health_check_test', 'test', timeout=5)
            value = safe_get('health_check_test')
            if value == 'test':
                return {'status': 'healthy', 'message': 'Cache connection OK'}
            return {'status': 'unhealthy', 'message': 'Cache read failed'}
        except Exception as e:
            return {'status': 'degraded', 'message': f'Cache unavailable: {str(e)}'}

    @staticmethod
    def check_models():
        from ..models import Material, SalesOrder, Inventory
        
        try:
            Material.objects.count()
            SalesOrder.objects.count()
            Inventory.objects.count()
            return {'status': 'healthy', 'message': 'All models OK'}
        except Exception as e:
            return {'status': 'unhealthy', 'message': f'Model check failed: {str(e)}'}

    @staticmethod
    def check_all():
        checks = {
            'database': HealthChecker.check_database(),
            'cache': HealthChecker.check_cache(),
            'models': HealthChecker.check_models(),
        }
        
        overall_status = 'healthy'
        for check_name, result in checks.items():
            if result['status'] == 'unhealthy':
                overall_status = 'unhealthy'
                break
            elif result['status'] == 'degraded' and overall_status == 'healthy':
                overall_status = 'degraded'
        
        return {
            'status': overall_status,
            'checks': checks,
            'timestamp': datetime.now().isoformat()
        }

    @staticmethod
    def get_system_info():
        import platform
        import django
        
        return {
            'platform': platform.system(),
            'platform_version': platform.version(),
            'python_version': platform.python_version(),
            'django_version': django.VERSION,
            'timestamp': datetime.now().isoformat()
        }