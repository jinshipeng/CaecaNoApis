"""
安全缓存包装器 - Redis不可用时自动降级为进程内内存缓存

解决django-redis在Redis服务未启动时导致500错误的问题。
所有业务代码应通过此模块访问缓存，而非直接使用django.core.cache。
"""

import time
import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

# 进程内LRU内存缓存（作为Redis不可用时的降级方案）
class _MemoryLRUCache:
    """线程安全的内存LRU缓存"""

    def __init__(self, max_size=1000):
        self._cache = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            if key in self._cache:
                # 移到末尾（最近访问）
                self._cache.move_to_end(key)
                return self._cache[key][0]  # 返回value（忽略timeout）
            return default

    def set(self, key, value, timeout=None):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            elif len(self._cache) >= self._max_size:
                # 淘汰最久未访问的
                self._cache.popitem(last=False)
            self._cache[key] = (value, time.time() + timeout if timeout else None)

    def delete(self, key):
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()

    def __contains__(self, key):
        return key in self._cache


# 全局内存降级缓存实例
_memory_cache = _MemoryLRUCache(max_size=2000)

_redis_available = None  # None=未检测, True=可用, False=不可用


def _check_redis():
    """检测Redis是否可用（带缓存结果）"""
    global _redis_available
    if _redis_available is not None:
        return _redis_available

    from django.core.cache import cache
    try:
        test_key = '__safe_cache_health__'
        cache.set(test_key, 'ok', 5)
        result = cache.get(test_key)
        _redis_available = (result == 'ok')
        if not _redis_available:
            logger.warning('[SafeCache] Redis不可用，已切换到内存降级模式')
    except Exception as e:
        _redis_available = False
        logger.warning(f'[SafeCache] Redis连接失败: {e}，已切换到内存降级模式')
    return _redis_available


def safe_get(key, default=None):
    """
    安全的缓存读取，Redis不可用时自动降级到内存

    Args:
        key: 缓存键
        default: 默认值

    Returns:
        缓存值或default
    """
    if _check_redis():
        try:
            import django.core.cache as cache_module
            return cache_module.cache.get(key, default)
        except Exception as e:
            logger.debug(f'[SafeCache] Redis读取失败，降级: {e}')
            _redis_available = False
    return _memory_cache.get(key, default)


def safe_set(key, value, timeout=None):
    """
    安全的缓存写入，Redis不可用时自动降级到内存

    Args:
        key: 缓存键
        value: 缓存值
        timeout: 过期时间（秒）
    """
    if _check_redis():
        try:
            import django.core.cache as cache_module
            cache_module.cache.set(key, value, timeout)
            return
        except Exception as e:
            logger.debug(f'[SafeCache] Redis写入失败，降级: {e}')
            _redis_available = False
    _memory_cache.set(key, value, timeout)


def safe_delete(key):
    """安全的缓存删除"""
    if _check_redis():
        try:
            import django.core.cache as cache_module
            cache_module.cache.delete(key)
            return
        except Exception:
            _redis_available = False
    _memory_cache.delete(key)


def safe_get_many(keys):
    """批量安全读取"""
    result = {}
    if _check_redis():
        try:
            import django.core.cache as cache_module
            raw = cache_module.cache.get_many(keys)
            result.update(raw)
            # 找出未命中的key
            missing = [k for k in keys if k not in result]
            for k in missing:
                val = _memory_cache.get(k)
                if val is not None:
                    result[k] = val
            return result
        except Exception:
            _redis_available = False
    for k in keys:
        val = _memory_cache.get(k)
        if val is not None:
            result[k] = val
    return result


def safe_set_many(data, timeout=None):
    """批量安全写入"""
    if _check_redis():
        try:
            import django.core.cache as cache_module
            cache_module.cache.set_many(data, timeout)
            return
        except Exception:
            _redis_available = False
    for k, v in data.items():
        _memory_cache.set(k, v, timeout)


def is_redis_available() -> bool:
    """返回Redis当前是否可用"""
    return _check_redis()


def reset_redis_check():
    """重置Redis可用性状态（下次调用时重新检测）"""
    global _redis_available
    _redis_available = None
