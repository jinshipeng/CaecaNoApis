# Utils package initialization
# 直接导入utils.py模块
import sys
import os

# 添加当前目录到Python路径，以便能够导入utils.py
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 直接导入utils.py文件
import importlib.util
utils_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'utils.py')
spec = importlib.util.spec_from_file_location('prediction.utils', utils_path)
utils_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utils_module)

# 从utils.py模块中导入所有函数
clear_messages = utils_module.clear_messages
get_cache_key = utils_module.get_cache_key
get_recent_file = utils_module.get_recent_file
read_file_safely = utils_module.read_file_safely
validate_file_size = utils_module.validate_file_size
validate_file_type = utils_module.validate_file_type
get_temp_file_path = utils_module.get_temp_file_path
convert_numpy_types = utils_module.convert_numpy_types
ensure_directory = utils_module.ensure_directory
log_user_operation = utils_module.log_user_operation
handle_file_upload = utils_module.handle_file_upload

# 导入cache_utils中的cache_manager
from .cache_utils import cache_manager

# 导出所有函数
__all__ = [
    'clear_messages',
    'get_cache_key',
    'get_recent_file',
    'read_file_safely',
    'validate_file_size',
    'validate_file_type',
    'get_temp_file_path',
    'convert_numpy_types',
    'ensure_directory',
    'log_user_operation',
    'handle_file_upload',
    'cache_manager'
]
