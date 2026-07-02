from django.core.cache import cache
import time
import hashlib
import logging
from typing import Any, Optional, Dict, List, Union
from ..utils.safe_cache import safe_get, safe_set, safe_delete

logger = logging.getLogger(__name__)

class CacheManager:
    """
    缓存管理器，提供更高级的缓存功能
    """
    
    # 缓存键前缀
    PREFIXES = {
        'dashboard': 'dashboard_',
        'scheduling': 'scheduling_',
        'data': 'data_'
    }
    
    # 默认过期时间（秒）
    DEFAULT_EXPIRY = {
        'dashboard': 60 * 60,  # 1小时
        'scheduling': 20 * 60,  # 20分钟
        'data': 15 * 60  # 15分钟
    }
    
    @classmethod
    def generate_key(cls, prefix: str, *args, **kwargs) -> str:
        """
        生成唯一的缓存键
        
        参数：
        - prefix: 缓存键前缀
        - args: 位置参数，用于生成缓存键
        - kwargs: 关键字参数，用于生成缓存键
        
        返回值：
        - str: 生成的缓存键
        """
        # 使用前缀
        key_parts = [prefix]
        
        # 添加位置参数
        for arg in args:
            if isinstance(arg, (str, int, float, bool)):
                key_parts.append(str(arg))
            elif isinstance(arg, dict):
                # 对字典进行排序，确保相同内容生成相同的键
                sorted_items = sorted(arg.items())
                key_parts.append(str(sorted_items))
            elif isinstance(arg, list):
                # 对列表进行排序，确保相同内容生成相同的键
                sorted_list = sorted(arg)
                key_parts.append(str(sorted_list))
        
        # 添加关键字参数
        if kwargs:
            sorted_kwargs = sorted(kwargs.items())
            key_parts.append(str(sorted_kwargs))
        
        # 使用MD5哈希生成固定长度的键
        key_string = '_'.join(key_parts)
        hash_part = hashlib.md5(key_string.encode('utf-8')).hexdigest()
        
        return f"{prefix}_{hash_part}"
    
    @classmethod
    def set(cls, key: str, value: Any, expiry: Optional[int] = None) -> bool:
        """
        设置缓存
        
        参数：
        - key: 缓存键
        - value: 缓存值
        - expiry: 过期时间（秒），如果为None则使用默认值
        
        返回值：
        - bool: 设置是否成功
        """
        try:
            if expiry is None:
                # 根据键前缀确定默认过期时间
                for prefix_name, prefix in cls.PREFIXES.items():
                    if key.startswith(prefix):
                        expiry = cls.DEFAULT_EXPIRY.get(prefix_name, 30 * 60)
                        break
                else:
                    expiry = 30 * 60  # 默认30分钟
            
            safe_set(key, value, expiry)
            # logger.debug(f"缓存设置成功: {key}, 过期时间: {expiry}秒")
            return True
        except Exception as e:
            logger.error(f"缓存设置失败: {key}, 错误: {str(e)}")
            return False
    
    @classmethod
    def get(cls, key: str) -> Optional[Any]:
        """
        获取缓存
        
        参数：
        - key: 缓存键
        
        返回值：
        - Any: 缓存值，如果不存在则返回None
        """
        try:
            value = safe_get(key)
            # if value is not None:
            #     logger.debug(f"缓存获取成功: {key}")
            # else:
            #     logger.debug(f"缓存不存在: {key}")
            return value
        except Exception as e:
            logger.error(f"缓存获取失败: {key}, 错误: {str(e)}")
            return None
    
    @classmethod
    def delete(cls, key: str) -> bool:
        """
        删除缓存
        
        参数：
        - key: 缓存键
        
        返回值：
        - bool: 删除是否成功
        """
        try:
            safe_delete(key)
            # logger.debug(f"缓存删除成功: {key}")
            return True
        except Exception as e:
            logger.error(f"缓存删除失败: {key}, 错误: {str(e)}")
            return False
    
    @classmethod
    def delete_pattern(cls, pattern: str) -> bool:
        """
        删除匹配模式的所有缓存
        
        参数：
        - pattern: 缓存键模式，支持通配符
        
        返回值：
        - bool: 删除是否成功
        """
        try:
            # 注意：Django的cache.delete_pattern可能不被所有后端支持
            if hasattr(cache, 'delete_pattern'):
                cache.delete_pattern(pattern)
                # logger.debug(f"缓存模式删除成功: {pattern}")
                return True
            else:
                logger.warning(f"当前缓存后端不支持delete_pattern: {pattern}")
                return False
        except Exception as e:
            logger.error(f"缓存模式删除失败: {pattern}, 错误: {str(e)}")
            return False
    
    @classmethod
    def clear(cls, prefix: Optional[str] = None) -> bool:
        """
        清除缓存
        
        参数：
        - prefix: 前缀，如果为None则清除所有缓存
        
        返回值：
        - bool: 清除是否成功
        """
        try:
            if prefix:
                # 清除指定前缀的缓存
                pattern = f"{prefix}*"
                return cls.delete_pattern(pattern)
            else:
                # 清除所有缓存
                cache.clear()
                # logger.debug("所有缓存已清除")
                return True
        except Exception as e:
            logger.error(f"缓存清除失败: {str(e)}")
            return False
    
    @classmethod
    def get_or_set(cls, key: str, default: callable, expiry: Optional[int] = None) -> Any:
        """
        获取缓存，如果不存在则设置
        
        参数：
        - key: 缓存键
        - default: 默认值生成函数
        - expiry: 过期时间（秒）
        
        返回值：
        - Any: 缓存值或默认值
        """
        value = cls.get(key)
        if value is None:
            value = default()
            cls.set(key, value, expiry)
        return value
    
    @classmethod
    def get_cache_stats(cls) -> Dict[str, Any]:
        """
        获取缓存统计信息
        
        返回值：
        - Dict: 缓存统计信息
        """
        stats = {
            'time': time.time(),
            'cache_backend': cache.__class__.__name__
        }
        
        # 尝试获取缓存后端的统计信息
        if hasattr(cache, 'stats'):
            try:
                stats.update(cache.stats())
            except Exception as e:
                logger.error(f"获取缓存统计信息失败: {str(e)}")
        
        return stats
    
    @classmethod
    def set_dashboard_data(cls, data: Dict[str, Any]) -> bool:
        """
        设置仪表盘数据缓存
        
        参数：
        - data: 仪表盘数据
        
        返回值：
        - bool: 设置是否成功
        """
        key = cls.generate_key(cls.PREFIXES['dashboard'], 'data')
        return cls.set(key, data, cls.DEFAULT_EXPIRY['dashboard'])
    
    @classmethod
    def get_dashboard_data(cls) -> Optional[Dict[str, Any]]:
        """
        获取仪表盘数据缓存
        
        返回值：
        - Dict: 仪表盘数据
        """
        key = cls.generate_key(cls.PREFIXES['dashboard'], 'data')
        return cls.get(key)
    
    @classmethod
    def set_scheduling_result(cls, product: str, params: Dict, data: Dict) -> bool:
        """
        设置排产结果缓存
        
        参数：
        - product: 产品代码
        - params: 排产参数
        - data: 排产结果数据
        
        返回值：
        - bool: 设置是否成功
        """
        key = cls.generate_key(cls.PREFIXES['scheduling'], product, params)
        return cls.set(key, data, cls.DEFAULT_EXPIRY['scheduling'])
    
    @classmethod
    def get_scheduling_result(cls, product: str, params: Dict) -> Optional[Dict]:
        """
        获取排产结果缓存
        
        参数：
        - product: 产品代码
        - params: 排产参数
        
        返回值：
        - Dict: 排产结果数据
        """
        key = cls.generate_key(cls.PREFIXES['scheduling'], product, params)
        return cls.get(key)

# 全局缓存管理器实例
cache_manager = CacheManager()
