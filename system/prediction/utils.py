import os
import tempfile
import pandas as pd
from django.contrib import messages
from django.core.cache import cache
import logging

logger = logging.getLogger(__name__)

def clear_messages(request):
    try:
        storage = messages.get_messages(request)
        list(storage)
        storage.used = True
        if 'django_messages' in request.session:
            del request.session['django_messages']
        request.session.modified = True
    except Exception:
        pass

def get_cache_key(prefix, *args):
    key_parts = [prefix]
    for arg in args:
        if isinstance(arg, (str, int, float, bool)):
            key_parts.append(str(arg))
        elif isinstance(arg, dict):
            sorted_items = sorted(arg.items())
            key_parts.append(str(sorted_items))
        elif isinstance(arg, list):
            sorted_list = sorted(arg)
            key_parts.append(str(sorted_list))
    return '_'.join(key_parts)

def get_recent_file(directory, prefix):
    try:
        files = [f for f in os.listdir(directory) if f.startswith(prefix)]
        if not files:
            return None
        latest_file = max(files, key=lambda x: os.path.getmtime(os.path.join(directory, x)))
        return os.path.join(directory, latest_file)
    except Exception as e:
        logger.error(f"获取最近文件失败: {str(e)}")
        return None

def read_file_safely(file_path):
    try:
        if file_path.endswith('.csv'):
            return pd.read_csv(file_path, encoding='utf-8-sig')
        else:
            try:
                return pd.read_excel(file_path, engine='openpyxl')
            except Exception:
                return pd.read_csv(file_path, encoding='utf-8-sig')
    except Exception as e:
        logger.error(f"读取文件失败: {str(e)}")
        raise ValueError(f"文件读取失败: {str(e)}")

def validate_file_size(file, max_size=10 * 1024 * 1024):
    if file.size > max_size:
        return False, "文件大小超过限制（最大10MB）"
    return True, None

def validate_file_type(file):
    if file.name.endswith('.xlsx') or file.name.endswith('.xls') or file.name.endswith('.csv'):
        return True, None
    return False, "只支持Excel文件 (.xlsx, .xls) 和 CSV文件 (.csv)"

def get_temp_file_path(prefix, extension=''):
    temp_dir = tempfile.gettempdir()
    if extension:
        if not extension.startswith('.'):
            extension = '.' + extension
    return os.path.join(temp_dir, f'{prefix}{extension}')

def convert_numpy_types(data):
    import numpy as np
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, np.integer):
                data[key] = int(value)
            elif isinstance(value, np.floating):
                data[key] = float(value)
            elif isinstance(value, (list, dict)):
                convert_numpy_types(value)
    elif isinstance(data, list):
        for item in data:
            convert_numpy_types(item)
    return data

def ensure_directory(directory):
    if not os.path.exists(directory):
        try:
            os.makedirs(directory)
        except Exception as e:
            logger.error(f"创建目录失败: {str(e)}")
            raise

def log_user_operation(user, operation, details=None):
    if details:
        logger.info(f"用户: {user.username}, 操作: {operation}, 详情: {details}")
    else:
        logger.info(f"用户: {user.username}, 操作: {operation}")

def clear_messages_decorator(func):
    def wrapper(request, *args, **kwargs):
        clear_messages(request)
        response = func(request, *args, **kwargs)
        clear_messages(request)
        return response
    return wrapper

def handle_file_upload(file):
    valid, message = validate_file_size(file)
    if not valid:
        return None, None, message, 'error'

    valid, message = validate_file_type(file)
    if not valid:
        return None, None, message, 'error'

    try:
        if file.name.endswith('.csv'):
            df = pd.read_csv(file, encoding='utf-8-sig')
        else:
            df = pd.read_excel(file)

        if df.empty:
            return None, None, '文件为空，没有数据可导入。', 'warning'

        temp_file = get_temp_file_path(f'imported_{os.path.basename(file.name)}')
        df.to_csv(temp_file, index=False, encoding='utf-8-sig')

        return df, temp_file, f'文件 {file.name} 上传成功！共导入 {len(df)} 条数据。', 'success'
    except Exception as e:
        return None, None, f'文件处理失败：{str(e)}', 'error'