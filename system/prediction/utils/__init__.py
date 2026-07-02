# 导入 utils.py 中的所有函数
import importlib.util
import os

# 获取 utils.py 文件路径
utils_file_path = os.path.join(os.path.dirname(__file__), '..', 'utils.py')

# 动态导入 utils.py 模块
spec = importlib.util.spec_from_file_location('prediction.utils', utils_file_path)
utils_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utils_module)

# 将 utils.py 模块的所有属性暴露出来
for name in dir(utils_module):
    if not name.startswith('_'):
        globals()[name] = getattr(utils_module, name)

# 导出所有函数
__all__ = [name for name in dir(utils_module) if not name.startswith('_')]
