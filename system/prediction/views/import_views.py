from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.conf import settings
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, generics
import csv
import os
import logging
import re
from ..utils.safe_cache import safe_delete

logger = logging.getLogger(__name__)


def _retry_db_operation(func, max_retries=3, delay=0.1):
    """数据库操作重试包装器，处理SQLite的'database is locked'错误"""
    import time
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            err_msg = str(e).lower()
            if 'locked' in err_msg or 'database' in err_msg:
                if attempt < max_retries - 1:
                    time.sleep(delay * (attempt + 1))
                    continue
            raise
from datetime import datetime, timedelta
from ..models import (
    Material, Supplier, BillOfMaterials, Inventory, SalesOrder, WorkCenter,
    SupplierMaterial, ImportHistory, Customer, DeliveryChange
)
from ..utils.field_regex import (
    smart_recognizer, recognize_csv_columns, auto_detect_data_type,
    get_field_mapping, detect_config_sub_type
)

# ==================== 全局映射字典（必须在函数定义之前） ====================

SUPPLIER_RATING_MAPPING = {
    'A级': 'A',
    'B级': 'B',
    'C级': 'C',
    'D级': 'D',
    'A': 'A',
    'B': 'B',
    'C': 'C',
    'D': 'D',
}

ORDER_STATUS_MAPPING = {
    '待排产': 'pending',
    '待处理': 'pending',
    '待确认': 'pending',
    '已确认': 'confirmed',
    '生产中': 'in_production',
    '已占料': 'allocated',
    '部分完成': 'partial',
    '部分齐套': 'partial',
    '已完成': 'complete',
    '完全齐套': 'complete',
    '已发货': 'shipped',
    '已交付': 'delivered',
    '已取消': 'cancelled',
    '急单-紧急补货': 'pending',
    '急单-插单': 'pending',
    'pending': 'pending',
    'confirmed': 'confirmed',
    'in_production': 'in_production',
    'allocated': 'allocated',
    'partial': 'partial',
    'complete': 'complete',
    'completed': 'complete',
    'processing': 'processing',
    'shipped': 'shipped',
    'delivered': 'delivered',
    'cancelled': 'cancelled',
}

SHIPPING_METHOD_MAPPING = {
    '海运': 'sea',
    '空运': 'air',
    '陆运': 'land',
    '快递': 'express',
    'sea': 'sea',
    'air': 'air',
    'express': 'express',
}

PRIORITY_MAPPING = {
    '紧急': 1,
    '加急': 2,
    '高': 3,
    '普通': 4,
    '低': 5,
    'critical': 1,
    'urgent': 2,
    'high': 3,
    'normal': 4,
    'low': 5,
}


def read_data_file(file_path_or_content, is_content=False):
    """
    通用文件读取函数，支持CSV和Excel文件

    参数:
        file_path_or_content: 文件路径或文件内容字节
        is_content: 如果为True则file_path_or_content是字节内容，否则是文件路径

    返回:
        (rows, columns) - 数据行列表和列名列表
    """
    import pandas as pd
    from io import BytesIO

    df = None

    if is_content:
        content = file_path_or_content
        file_ext = '.xlsx'
    else:
        file_path = file_path_or_content
        file_ext = os.path.splitext(file_path)[1].lower()

    try:
        if file_ext in ['.xlsx', '.xls']:
            if is_content:
                df = pd.read_excel(BytesIO(content))
            else:
                df = pd.read_excel(file_path)
        else:
            encodings = ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'latin1']
            for encoding in encodings:
                try:
                    if is_content:
                        df = pd.read_csv(BytesIO(content), encoding=encoding)
                    else:
                        df = pd.read_csv(file_path, encoding=encoding)
                    break
                except Exception:
                    continue
    except Exception as e:
        raise Exception(f'文件读取失败: {str(e)}')

    if df is None:
        raise Exception('无法解析文件，请检查文件格式')

    df = df.fillna('')
    rows = []
    for _, row in df.iterrows():
        row_dict = {}
        for col in df.columns:
            value = row[col]
            if value is None or pd.isna(value):
                row_dict[col] = ''
            else:
                row_dict[col] = str(value).strip()
        rows.append(row_dict)

    columns = list(df.columns)
    return rows, columns


def sanitize_rows(rows):
    """清理行数据中的None值，确保所有值都是字符串，并过滤全空行"""
    cleaned = []
    for row in rows:
        clean_row = {}
        has_data = False
        for key, value in row.items():
            if value is None:
                clean_row[key] = ''
            elif isinstance(value, str):
                clean_row[key] = value
            else:
                clean_row[key] = str(value)
            # 检查是否有非空数据
            if clean_row.get(key) and clean_row[key].strip():
                has_data = True
        # 跳过全空行（CSV末尾的空行等）
        if has_data:
            cleaned.append(clean_row)
    return cleaned


def safe_str(val, default=''):
    """安全获取字符串值，处理 None 和非字符串类型"""
    if val is None:
        return default
    return str(val).strip()


def get_csv_files_for_batch():
    """获取批量导入的CSV文件列表"""
    base_path = getattr(settings, 'CSV_IMPORT_BASE_PATH', None) or os.path.join(settings.BASE_DIR.parent, '数据集')
    csv_files = []
    
    # 文件名到导入类型的映射
    file_type_map = {
        '原材料': 'material',
        '物料': 'material',
        'material': 'material',
        '成品': 'bom',
        'bom': 'bom',
        'BOM': 'bom',
        '库存': 'inventory',
        'inventory': 'inventory',
        '订单': 'order',
        'order': 'order',
        '产线': 'workcenter',
        '工作中心': 'workcenter',
        'workcenter': 'workcenter',
        '供应商': '供应商承诺',
        '供应商承诺': '供应商承诺',
        '客户': 'customer',
        'customer': 'customer',
        '采购': 'purchase',
        'purchase': 'purchase',
        '配置': 'config',
        'config': 'config',
        '系统配置': 'config'
    }
    
    # 类型名称映射（用于显示）
    type_name_map = {
        'material': '物料数据',
        '供应商承诺': '供应商数据',
        'customer': '客户数据',
        'bom': 'BOM数据',
        'inventory': '库存数据',
        'order': '订单数据',
        'purchase': '采购订单数据',
        'workcenter': '工作中心数据',
        'config': '系统配置数据'  # 工厂日历/调拨/优先级规则
    }

    # 颜色映射
    type_color_map = {
        'material': 'text-blue-400',
        '供应商承诺': 'text-green-400',
        'customer': 'text-purple-400',
        'bom': 'text-yellow-400',
        'inventory': 'text-orange-400',
        'order': 'text-red-400',
        'purchase': 'text-pink-400',
        'workcenter': 'text-cyan-400',
        'config': 'text-gray-400'  # 系统配置
    }
    
    if os.path.exists(base_path):
        for file_name in os.listdir(base_path):
            if file_name.lower().endswith('.csv'):
                import_type = 'material'  # 默认类型
                type_name = '未识别'
                color = 'text-gray-400'
                
                # 根据文件名推断类型
                for keyword, itype in file_type_map.items():
                    if keyword in file_name:
                        import_type = itype
                        type_name = type_name_map.get(itype, '未识别')
                        color = type_color_map.get(itype, 'text-gray-400')
                        break
                
                csv_files.append({
                    'name': file_name,
                    'type': import_type,
                    'type_name': type_name,
                    'color': color,
                    'path': os.path.join(base_path, file_name),
                    'exists': os.path.exists(os.path.join(base_path, file_name))
                })
    
    # 按依赖关系排序：物料→客户/供应商→BOM→库存→订单→采购
    TYPE_ORDER = {'material': 0, '供应商承诺': 1, 'customer': 1, 'bom': 2,
                  'inventory': 3, 'order': 4, 'purchase': 5,
                  'workcenter': 6, 'config': 7, 'substitute': 8}
    csv_files.sort(key=lambda x: (TYPE_ORDER.get(x['type'], 99), x['name']))
    return csv_files


class RefreshCSVListView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """刷新CSV文件列表（AJAX）"""
        csv_files = get_csv_files_for_batch()
        return Response({'files': csv_files})


class ImportDataView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        """CSV数据导入视图"""
        import_type = request.data.get('import_type')
        file = request.FILES.get('csv_file')
        clean_import = request.data.get('clean_import', 'false').lower() == 'true'
        
        if not file:
            return Response({'status': 'error', 'message': '请选择CSV文件'}, status=status.HTTP_400_BAD_REQUEST)

        # 文件大小限制（最大 50MB）
        MAX_UPLOAD_SIZE = 50 * 1024 * 1024
        if file.size > MAX_UPLOAD_SIZE:
            return Response({'status': 'error', 'message': f'文件过大（{file.size / 1024 / 1024:.1f}MB），限制 50MB 以内'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # 安全修复：清理文件名，防止路径穿越攻击
            # 只保留字母、数字、中文、下划线、连字符、点和空格
            safe_filename = re.sub(r'[^\w\u4e00-\u9fff\-.\s]', '', file.name)
            # 移除可能的路径穿越字符
            safe_filename = safe_filename.replace('..', '').replace('/', '').replace('\\', '')
            if not safe_filename or len(safe_filename) > 255:
                safe_filename = f'upload_{datetime.now().strftime("%Y%m%d%H%M%S")}.csv'

            file_path = os.path.join(settings.MEDIA_ROOT, 'imports', safe_filename)
            # 二次验证：确保路径在允许的目录内
            real_path = os.path.realpath(file_path)
            allowed_dir = os.path.realpath(os.path.join(settings.MEDIA_ROOT, 'imports'))
            if not real_path.startswith(allowed_dir):
                return Response({'status': 'error', 'message': '非法文件名'}, status=status.HTTP_400_BAD_REQUEST)

            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            with open(file_path, 'wb+') as destination:
                for chunk in file.chunks():
                    destination.write(chunk)

            try:
                result = import_csv_data(file_path, import_type, clean_import)
            finally:
                # 确保异常时也能清理临时文件
                if os.path.exists(file_path):
                    os.remove(file_path)

            # 导入成功后清除相关缓存（含各策略子缓存），确保前端立即看到最新数据
            if result.get('status') in ('success', 'partial'):
                safe_delete('screen_data_v2')
                safe_delete('inventory_cache')
                safe_delete('bom_cache')
                from ..tasks import _clear_all_planning_caches
                _clear_all_planning_caches()
                # 尝试清除物料计划引擎缓存（兼容不同缓存后端）
                try:
                    # Redis/Memcached 支持 keys 模式匹配
                    for key in list(cache.keys('mrp_*')):
                        safe_delete(key)
                except (AttributeError, TypeError):
                    # LocMemCache 不支持 keys()，跳过即可（MRP缓存有TTL会自动过期）
                    pass

            # 保存导入历史记录到数据库
            try:
                # 从返回结果中获取实际的导入类型（处理auto自动检测模式）
                actual_import_type = import_type
                if not actual_import_type or actual_import_type == 'auto':
                    auto_detected = result.get('auto_detected', {})
                    actual_import_type = (
                        auto_detected.get('type')
                        or auto_detected.get('main_type')
                        or 'material'
                    )
                # 兜底：确保类型在合法 choices 范围内
                VALID_IMPORT_TYPES = {
                    'material', 'supplier', '供应商承诺', 'customer', 'bom',
                    'inventory', 'order', 'purchase', 'workcenter', 'config',
                    'delivery_change', 'factory_calendar', 'factory_transfer',
                    'priority_rule', 'engineering_change',
                    'factory_calendar_transfer', 'config_rules_ecn', 'substitute',
                }
                if actual_import_type not in VALID_IMPORT_TYPES:
                    actual_import_type = 'material'
                    logger.warning(f'导入类型 {actual_import_type} 不在合法范围，已回退为 material')

                ImportHistory.objects.create(
                    import_type=actual_import_type,
                    file_name=file.name,
                    status=result.get('status', 'error'),
                    imported_count=result.get('imported', 0),
                    updated_count=result.get('updated', 0),
                    error_count=len(result.get('errors', [])),
                    error_details='\n'.join(result.get('errors', [])[:50]) if result.get('errors') else None,
                    imported_by=request.user if request.user.is_authenticated else None
                )
            except Exception as e:
                logger.warning(f'保存导入历史失败: {e}')

            return Response(result)
        except Exception as e:
            import traceback
            logger.error(f"数据导入异常: {e}\n{traceback.format_exc()}")
            return Response({'status': 'error', 'message': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

def import_csv_data(file_path, import_type, clean_import=False):
    """导入CSV/Excel数据 - 使用pandas加速大文件"""
    results = {
        'status': 'success',
        'message': '导入成功',
        'imported': 0,
        'updated': 0,
        'errors': []
    }

    file_ext = os.path.splitext(file_path)[1].lower()
    rows = None

    if file_ext in ['.xlsx', '.xls']:
        try:
            rows, _ = read_data_file(file_path, is_content=False)
        except Exception as e:
            results['status'] = 'error'
            results['message'] = f'Excel文件读取失败: {str(e)}'
            return results
    else:
        # 使用pandas读取CSV（比csv模块快5-10倍，尤其大文件）
        try:
            import pandas as pd
            encodings = ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'latin1']
            df = None
            for encoding in encodings:
                try:
                    df = pd.read_csv(file_path, encoding=encoding, dtype=str)
                    # 将所有NaN替换为空字符串
                    df = df.fillna('')
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue

            if df is None:
                results['status'] = 'error'
                results['message'] = '无法解析文件编码，请确保文件为UTF-8或GBK格式'
                return results

            if df.empty:
                results['status'] = 'error'
                results['message'] = '文件为空或没有数据行'
                return results

            # 高效清理列名：只处理一次列名，不逐行处理
            clean_columns = []
            for col in df.columns:
                if col is None:
                    continue
                c = str(col).strip()
                if c.startswith('\ufeff'):
                    c = c[1:]
                c = ''.join(c.split())  # 去除空白
                if c:
                    clean_columns.append(c)
            df.columns = clean_columns

            # 转换为字典列表（pandas的to_dict('records')比手动循环快很多）
            rows = df.to_dict('records')

        except Exception as e:
            # pandas失败时回退到原始方法
            encodings = ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'latin1']
            for encoding in encodings:
                try:
                    with open(file_path, 'r', encoding=encoding) as f:
                        reader = csv.DictReader(f)
                        rows = list(reader)
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue

            if rows is None:
                results['status'] = 'error'
                results['message'] = f'文件读取失败: {str(e)}'
                return results

            # 回退模式下的清理（较慢但兼容性好）
            cleaned_rows = []
            for row in rows:
                cleaned_row = {}
                for key, value in row.items():
                    if key is None:
                        continue
                    clean_key = str(key).strip()
                    if clean_key.startswith('\ufeff'):
                        clean_key = clean_key[1:]
                    clean_key = ''.join(clean_key.split())
                    if not clean_key:
                        continue
                    cleaned_row[clean_key] = value
                cleaned_rows.append(cleaned_row)
            rows = cleaned_rows

    if not rows:
        results['status'] = 'error'
        results['message'] = '文件无有效数据'
        return results

    rows = sanitize_rows(rows)

    # 自动检测导入类型（如果未指定或指定为auto）
    if not import_type or import_type == 'auto':
        columns = list(rows[0].keys()) if rows else []
        detected_type, confidence = auto_detect_data_type(columns)

        # 强制修正：基于首列+列集特征覆盖误检（优先级：库存>BOM>物料>采购>客户）
        col_set = set(str(c).strip() for c in columns)
        first_col = str(columns[0]).strip() if columns else ''

        # 库存表检测（必须在物料之前，因为库存首列也是"物料ID"）
        if any(k in col_set for k in ['在库数量', 'Hold数量', '可用数量', '库位', '保质期到期日']):
            detected_type, confidence = 'inventory', 0.99
        # BOM表检测（首列可能是"父项物料"/"成品ID"等）
        elif any(k in col_set for k in ['父项物料', '子项物料', '用量', '成品ID']):
            detected_type, confidence = 'bom', 0.99
        # 物料表：首列为"物料ID"/"物料代码"（但排除ECN等配置子表）
        elif first_col in ('物料ID', '物料代码', '物料编号'):
            # 检查是否实际是工程变更表（也有物料ID列但有ECN编号/变更类型等特征列）
            col_set_upper = {str(c).strip().upper() for c in columns}
            ecn_markers = {'ECN编号', 'ECN_NO', '变更类型', 'CHANGE_TYPE', '变更原因', '关联产品', 'ECN类别'}
            if ecn_markers & {c.upper() for c in columns}:
                detected_type, confidence = 'config', 0.85
            else:
                detected_type, confidence = 'material', 0.99
        # 采购订单：首列为"采购订单号"/"采购单号"
        elif first_col in ('采购订单号', '采购单号', '采购订单ID'):
            detected_type, confidence = 'purchase', 0.99
        # 客户表：首列为"客户ID"/"客户代码"
        elif first_col in ('客户ID', '客户代码', '客户编号'):
            detected_type, confidence = 'customer', 0.99
        # 供应商表：首列为"供应商ID"/列集含供应商名称/评级/联系人等特征
        elif first_col in ('供应商ID', '供应商代码', '供应商编号') or \
             any(k in col_set for k in ['供应商名称', '供应商评级', '交付可靠率', '准时交付率', '产能等级']):
            detected_type, confidence = 'supplier', 0.99
        # 产线表：首列为"产线编号"/列集含工作中心/日产能等核心特征（排除日历/调拨等含班次类型的文件）
        elif first_col in ('产线编号', '工作中心代码', 'WorkCenter') or \
             (any(k in col_set for k in ['工作中心名称', '日产能上限', '维护状态']) and
              '数据类型' not in col_set):
            detected_type, confidence = 'workcenter', 0.99
        # 订单表：首列为"订单ID"/"订单编号"/"销售订单号"（必须在交期变更之前！）
        elif first_col in ('订单ID', '订单编号', '销售订单号', 'SO-'):
            detected_type, confidence = 'order', 0.99
        # 工程变更表：有ECN编号/变更类型等特征列（优先于通用检测）
        elif any(k in col_set for k in ['ECN编号', 'ECN_no', '变更类型', '关联产品', 'ECN类别']):
            detected_type, confidence = 'config', 0.90
        # 交期变更记录表：有订单号+变更类型+原/新交付日期
        elif any(k in col_set for k in ['变更类型', '原交付日期', '新交付日期', '变更天数']):
            if any(k in col_set for k in ['订单号', '关联订单号', 'order_no']):
                detected_type, confidence = 'delivery_change', 0.98

        # 特殊处理：如果是系统配置类型，进一步检测子类型
        if detected_type == 'config' or confidence < 0.5:
            sub_type, sub_confidence, details = detect_config_sub_type(columns, rows[:10])
            logger.info(f'自动检测结果: type={detected_type}({confidence:.2%}), config_sub_type={sub_type}({sub_confidence:.2%})')

            if sub_type in ('factory_calendar', 'factory_transfer', 'priority_rule', 'engineering_change',
                             'factory_calendar_transfer', 'config_rules_ecn'):
                import_type = sub_type
                results['auto_detected'] = {
                    'main_type': 'config',
                    'config_sub_type': sub_type,
                    'sub_confidence': sub_confidence,
                    'details': details
                }
            elif sub_type in ('delivery_change',):
                import_type = 'delivery_change'
                results['auto_detected'] = {
                    'type': 'delivery_change',
                    'confidence': sub_confidence,
                    'details': details
                }
            elif sub_type != 'unknown':
                import_type = 'config'
                results['auto_detected'] = {
                    'main_type': detected_type,
                    'config_sub_type': sub_type,
                    'sub_confidence': sub_confidence,
                    'details': details
                }
            else:
                import_type = detected_type
                results['auto_detected'] = {
                    'type': detected_type,
                    'confidence': confidence
                }
        else:
            import_type = detected_type
            results['auto_detected'] = {
                'type': detected_type,
                'confidence': confidence
            }
        logger.info(f'自动识别导入类型: {import_type}')

    # 使用高性能批量导入（性能提升5-10倍）
    hp_result = import_data_high_performance(rows, import_type, clean_import)

    # 合并结果：保留 auto_detected 等元信息，不被高性能函数返回值覆盖
    if 'auto_detected' in results:
        hp_result['auto_detected'] = results['auto_detected']
    results = hp_result

    return results

def import_material_data(rows):
    """导入物料数据（支持合并后的统一物料文件）"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            material_code = row.get('物料ID', row.get('material_code', '')).strip()
            if not material_code:
                errors.append(f'第{i}行：物料代码不能为空')
                continue

            # 获取类型（支持多种列名格式，包括合并后的"物料类型"列）
            material_type_raw = row.get('物料类型', row.get('类型(原材料/半成品)', row.get('类型', row.get('material_type', 'raw')))).strip()
            # 清理类型值，去除括号内容
            if '(' in material_type_raw:
                material_type = material_type_raw.split('(')[0].strip()
            else:
                material_type = material_type_raw

            # 类型映射
            type_mapping = {
                '原材料': 'raw',
                '半成品': 'semi',
                '成品': 'finished',
                'raw': 'raw',
                'semi': 'semi',
                'semi_finished': 'semi',
                'finished': 'finished'
            }
            material_type = type_mapping.get(material_type.lower(), 'raw')

            # 获取标准成本（支持多种列名格式）
            cost_str = row.get('标准成本', row.get('单价(元)', row.get('标准成本(元)', row.get('standard_cost', '0')))).strip()
            standard_cost = round(float(cost_str.replace(',', '')), 2) if cost_str else 0

            # 获取安全库存（如果CSV中没有则根据类型设置默认值）
            safety_stock_str = row.get('安全库存', row.get('safety_stock', '')).strip()
            if safety_stock_str:
                safety_stock = int(float(safety_stock_str.replace(',', '')))
            else:
                # 根据最小起订量自动计算安全库存（默认为起订量的2倍）
                min_order = int(row.get('最小起订量', row.get('min_order_qty', '100')).strip() or '100')
                safety_stock = min_order * 2

            # 获取保质期（支持多种格式）
            shelf_life_str = row.get('保质期(天)', row.get('保质期(天,0=无)', row.get('保质期', row.get('shelf_life', '0')))).strip()
            shelf_life = int(shelf_life_str.replace(',', '').replace('天', '').replace('0=无', '0')) if shelf_life_str else 0

            # 获取供应商相关属性（01_物料.csv中的扩展字段）
            supplier_lead_time = int(row.get('正常交期(天)', row.get('normal_lead_time', '7')).strip() or '7')
            delivery_reliability = float(row.get('交付可靠率', row.get('delivery_reliability', '0.9')).strip() or 0.9)
            supplier_rating_raw = safe_str(row.get('供应商评级'))

            defaults = {
                'material_name': row.get('物料名称', row.get('material_name', '')).strip(),
                'material_type': material_type,
                'unit': row.get('单位', row.get('unit', '件')).strip(),
                'shelf_life': shelf_life,
                'min_order_qty': int(row.get('最小起订量', row.get('min_order_qty', '1')) or '1'),
                'lead_time': int(row.get('采购提前期(天)', row.get('采购提前期', row.get('lead_time', '7'))) or '7'),
                'standard_cost': standard_cost,
                'sales_price': round(float(row.get('销售价格', row.get('销售价格(元)', row.get('sales_price', '0'))).replace(',', '') or '0'), 2),
                'safety_stock': safety_stock,
                'min_production_qty': int(row.get('最小生产批量', row.get('min_production_qty', '1')) or '1'),
                'is_active': True  # 默认启用
            }
            
            obj, created = Material.objects.update_or_create(
                material_code=material_code,
                defaults=defaults
            )
            
            if created:
                imported += 1
            else:
                updated += 1
                
            if '主供应商' in row and row['主供应商'].strip():
                supplier_code = row['主供应商'].strip()
                supplier = Supplier.objects.filter(supplier_code=supplier_code).first()

                if not supplier:
                    supplier_name = row.get('主供应商名称', row.get('supplier_name', supplier_code))
                    supplier, _ = Supplier.objects.get_or_create(
                        supplier_code=supplier_code,
                        defaults={
                            'supplier_name': supplier_name.strip(),
                            'contact_person': row.get('联系人', ''),
                            'phone': row.get('联系电话', ''),
                            'email': row.get('邮箱', ''),
                            'address': row.get('地址', ''),
                            'rating': SUPPLIER_RATING_MAPPING.get(supplier_rating_raw, 'B'),
                            'delivery_reliability': delivery_reliability,
                            'normal_lead_time': supplier_lead_time,
                            'is_active': True
                        }
                    )
                else:
                    # 更新现有供应商的属性（使用物料文件中的值）
                    Supplier.objects.filter(pk=supplier.pk).update(
                        rating=SUPPLIER_RATING_MAPPING.get(supplier_rating_raw, supplier.rating or 'B'),
                        delivery_reliability=delivery_reliability,
                        normal_lead_time=supplier_lead_time
                    )
                
                SupplierMaterial.objects.update_or_create(
                    material=obj,
                    supplier=supplier,
                    defaults={
                        'unit_price': float(row.get('单价(元)', '0')),
                        'min_order_qty': int(row.get('最小起订量', '1')),
                        'lead_time': int(row.get('采购提前期(天)', '7')),
                        'is_forbidden': False
                    }
                )

            # 导入备用供应商
            if '备用供应商' in row and row['备用供应商'].strip():
                backup_code = row['备用供应商'].strip()
                backup_supplier = Supplier.objects.filter(supplier_code=backup_code).first()
                if backup_supplier:
                    SupplierMaterial.objects.update_or_create(
                        material=obj,
                        supplier=backup_supplier,
                        defaults={
                            'unit_price': float(row.get('单价(元)', '0')),
                            'min_order_qty': int(row.get('最小起订量', '1')),
                            'lead_time': int(row.get('采购提前期(天)', '7')),
                            'is_forbidden': False
                        }
                    )
                    
        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')
    
    return {
        'status': 'success' if not errors else 'partial',
        'message': f'物料数据导入完成',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }

def import_supplier_data(rows):
    """导入供应商数据（支持合并后的统一供应商文件，跳过承诺行）"""
    imported = 0
    updated = 0
    errors = []
    skipped = 0

    # 测试/无效数据关键词列表
    INVALID_PATTERNS = ['sdf', 'test', 'xxx', 'aaa', '111', 'temp', 'demo', 'admin']

    for i, row in enumerate(rows, 1):
        try:
            # 如果有数据类型列，只处理供应商信息行
            data_type = row.get('数据类型', '').strip()
            if data_type and data_type != '供应商信息':
                continue  # 跳过非供应商信息行（如供应商承诺行）

            supplier_code = row.get('供应商ID', row.get('supplier_code', '')).strip()
            supplier_name = row.get('供应商名称', row.get('supplier_name', '')).strip()

            if not supplier_code:
                errors.append(f'第{i}行：供应商代码不能为空')
                continue

            # 数据验证：跳过明显的测试/无效数据
            code_lower = supplier_code.lower()
            name_lower = supplier_name.lower()

            is_invalid = (
                len(supplier_code) < 3 or
                code_lower in INVALID_PATTERNS or
                any(p in code_lower for p in INVALID_PATTERNS) or
                (supplier_name and name_lower in INVALID_PATTERNS) or
                (supplier_name and any(p in name_lower for p in INVALID_PATTERNS)) or
                supplier_code == supplier_name  # 代码和名称相同且过短
            )

            if is_invalid:
                skipped += 1
                continue  # 跳过无效数据，不报错
            
            defaults = {
                'supplier_name': row.get('供应商名称', row.get('supplier_name', '')).strip(),
                'contact_person': row.get('联系人', row.get('contact_person', '')).strip(),
                'phone': row.get('联系电话', row.get('phone', '')).strip(),
                'email': row.get('邮箱', row.get('email', '')).strip(),
                'address': row.get('地址', row.get('address', '')).strip(),
                'rating': SUPPLIER_RATING_MAPPING.get(safe_str(row.get('供应商评级')), 'B'),
                'delivery_reliability': float(row.get('交付可靠率', row.get('delivery_reliability', '0.9'))),
                'normal_lead_time': int(row.get('正常交期(天)', row.get('normal_lead_time', '7'))),
                'is_active': row.get('状态', row.get('is_active', '1')) != '0'
            }
            
            obj, created = Supplier.objects.update_or_create(
                supplier_code=supplier_code,
                defaults=defaults
            )
            
            if created:
                imported += 1
            else:
                updated += 1
                
        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')
    
    message = f'供应商数据导入完成'
    if skipped > 0:
        message += f'（已跳过 {skipped} 条无效/测试数据）'

    return {
        'status': 'success' if not errors else 'partial',
        'message': message,
        'imported': imported,
        'updated': updated,
        'skipped': skipped,
        'errors': errors
    }

def import_bom_data(rows):
    """导入BOM数据（支持无序导入 - 自动创建缺失的物料）"""
    imported = 0
    updated = 0
    errors = []
    warnings = []
    auto_created_parents = 0
    auto_created_children = 0

    # 调试：打印前3行的所有键值对
    if rows:
        logger.debug(f"BOM导入 - 总行数: {len(rows)}, 列名: {list(rows[0].keys())}")

    for i, row in enumerate(rows, 1):
        try:
            parent_code = row.get('成品ID', row.get('parent_code', '')).strip()
            child_code = row.get('构成原材料ID', row.get('child_code', '')).strip()
            parent_name = row.get('成品名称', row.get('parent_name', '')).strip()
            child_name = row.get('原材料名称', row.get('child_name', '')).strip()

            if not parent_code or not child_code:
                errors.append(f'第{i}行：父物料或子物料代码不能为空')
                continue

            # 获取或自动创建父物料（成品）
            parent_material = Material.objects.filter(material_code=parent_code).first()
            if not parent_material:
                standard_cost = round(float(row.get('标准成本(元)', row.get('standard_cost', '0'))), 2)
                sales_price = round(float(row.get('销售价格(元)', row.get('销售 价格(元)', row.get('sales_price', '0')))), 2)

                # 安全处理带括号的列名
                shelf_life_raw = str(row.get('保质期(天)', row.get('保质期(天', row.get('shelf_life', '0'))))
                shelf_life_val = shelf_life_raw.replace(')', '').replace('0=无', '0') or '0'
                shelf_life = int(shelf_life_val)

                lead_time_raw = str(row.get('生产提前期(天)', row.get('生产提前期(天', row.get('lead_time', '6'))))
                lead_time_val = lead_time_raw.replace(')', '') or '6'
                lead_time = int(lead_time_val)

                min_production_qty = int(row.get('最小生产批量', row.get('min_production_qty', '1')))

                parent_material, _ = Material.objects.get_or_create(
                    material_code=parent_code,
                    defaults={
                        'material_name': parent_name if parent_name else parent_code,
                        'material_type': 'finished',
                        'unit': safe_str(row.get('单位'), '台'),
                        'standard_cost': standard_cost,
                        'sales_price': sales_price,
                        'shelf_life': shelf_life,
                        'lead_time': lead_time,
                        'min_order_qty': min_production_qty,
                        'min_production_qty': min_production_qty,
                        'is_active': True
                    }
                )
                auto_created_parents += 1

            # 获取或自动创建子物料（原材料/零部件）
            child_material = Material.objects.filter(material_code=child_code).first()
            if not child_material:
                child_material, _ = Material.objects.get_or_create(
                    material_code=child_code,
                    defaults={
                        'material_name': child_name if child_name else child_code,
                        'material_type': 'raw',
                        'unit': safe_str(row.get('单位'), '件'),
                        'is_active': True
                    }
                )
                auto_created_children += 1

            # 读取并校验可替代物料列（逗号分隔的物料代码）
            alt_raw = row.get('可替代物料', row.get('替代组', row.get('alternative_group', ''))).strip()
            alt_codes = [c.strip() for c in alt_raw.split(',') if c.strip()] if alt_raw else []
            if alt_codes:
                # 校验每个替代物料代码是否存在于物料表中
                valid_codes = []
                for code in alt_codes:
                    if Material.objects.filter(material_code=code).exists():
                        valid_codes.append(code)
                    else:
                        warnings.append(f'BOM第{i}行：替代物料「{code}」不存在于物料表中，已忽略')
                alternative_group = ','.join(valid_codes) if valid_codes else ''
            else:
                alternative_group = ''

            defaults = {
                'quantity': int(round(float(row.get('单位用量', row.get('quantity', '1'))))),
                'unit': row.get('单位', row.get('unit', '件')).strip(),
                'bom_level': int(row.get('BOM层级', row.get('结构层级', '1'))),
                'usage_ratio': round(float(row.get('用量占比(%)', row.get('usage_ratio', '0'))), 1),
                'scrap_rate': round(float(row.get('报废率', row.get('scrap_rate', '0'))), 3),
                'alternative_group': alternative_group,
                'alternative_priority': int(row.get('优先级(1最高)', row.get('alternative_priority', '1'))),
                'alternative_ratio': round(float(row.get('替代比例', row.get('替代用料比例', row.get('alternative_ratio', '1.0')))), 2),
                'is_active': True
            }

            obj, created = BillOfMaterials.objects.update_or_create(
                parent_material=parent_material,
                child_material=child_material,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    message = f'BOM数据导入完成'
    if auto_created_parents > 0 or auto_created_children > 0:
        message += f'（自动创建了 {auto_created_parents} 个成品物料和 {auto_created_children} 个零部件）'

    return {
        'status': 'success' if not errors else 'partial',
        'message': message,
        'imported': imported,
        'updated': updated,
        'auto_created': auto_created_parents + auto_created_children,
        'errors': errors[:50],
        'warnings': warnings[:50]
    }

def import_inventory_data(rows, clean_import=False):
    """导入库存数据（支持无序导入 - 自动创建缺失的物料）

    在途库存处理：在途行不单独建记录，合并到该物料库存量最小的仓库
    """
    imported = 0
    updated = 0
    errors = []
    auto_created_materials = 0

    # 清空模式：删除所有现有库存记录，避免残留旧数据
    if clean_import:
        deleted_count, _ = Inventory.objects.all().delete()

    # ---- 两阶段收集：先分离本地和在途行 ----
    local_rows = []
    transit_by_material = {}  # mat_id -> [累计qty, 累计hold]

    for i, row in enumerate(rows, 1):
        try:
            material_code = row.get('物料ID', row.get('material_code', '')).strip()
            if not material_code:
                material_code = row.get('成品ID', row.get('product_id', '')).strip()
            if not material_code:
                errors.append(f'第{i}行：物料代码不能为空')
                continue

            # 获取或自动创建物料（智能判断类型）
            material = Material.objects.filter(material_code=material_code).first()
            if not material:
                material_name = row.get('物料名称', row.get('material_name', '')).strip()
                unit = row.get('单位', row.get('unit', '件')).strip()

                # 根据列名特征判断物料类型
                mat_type = 'raw'
                if '成品' in material_name or '产品' in material_name:
                    mat_type = 'finished'
                elif '半成品' in material_name:
                    mat_type = 'semi'

                safety_stock = int(float(row.get('安全库存', row.get('safety_stock', '0'))))
                standard_cost = round(float(row.get('标准成本(元)', row.get('standard_cost', '0'))), 2)

                material, _ = Material.objects.get_or_create(
                    material_code=material_code,
                    defaults={
                        'material_name': material_name if material_name else material_code,
                        'material_type': mat_type,
                        'unit': unit if unit else '件',
                        'safety_stock': safety_stock if safety_stock > 0 else None,
                        'standard_cost': standard_cost if standard_cost > 0 else 0,
                        'is_active': True
                    }
                )
                auto_created_materials += 1

            expiry_date_str = row.get('保质期到期日', row.get('expiry_date', ''))
            expiry_date = None
            if expiry_date_str.strip():
                date_str = expiry_date_str.strip().split(' ')[0]  # 去掉时间部分
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y/%m/%d %H:%M', '%Y-%m-%d %H:%M:%S']:
                    try:
                        expiry_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            hold_until_str = row.get('预计解Hold日期', row.get('预计解Hold日期', ''))
            hold_until = None
            if hold_until_str.strip():
                date_str = hold_until_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y/%m/%d %H:%M', '%Y-%m-%d %H:%M:%S']:
                    try:
                        hold_until = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            data_date_str = row.get('数据日期', row.get('data_date', ''))
            data_date = None
            if data_date_str.strip():
                date_str = data_date_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d', '%Y/%m/%d %H:%M', '%Y-%m-%d %H:%M:%S']:
                    try:
                        data_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            # 智能识别Hold状态
            hold_qty_str = str(row.get('Hold数量', row.get('冻结数量', '0'))).strip()
            hold_quantity = int(float(hold_qty_str)) if hold_qty_str and hold_qty_str != '' else 0
            is_hold = hold_quantity > 0

            quantity = int(float(row.get('在库数量', row.get('quantity', '0'))))

            if quantity < 0:
                errors.append(f'第{i}行：在库数量不能为负数（{quantity}）')
                continue

            warehouse = row.get('仓库', row.get('warehouse', '')).strip() or '主仓库'
            location = row.get('库位', row.get('location', '')).strip()
            inventory_type_raw = row.get('库存类型', row.get('inventory_type', '本地')).strip().lower()

            # 库存类型标准化（CSV中文 → 模型英文值）
            inv_type_map = {
                '本地': 'local', '在库': 'local', 'local': 'local',
                '在途': 'transit', '运输中': 'transit', 'transit': 'transit',
                '供应商承诺': 'supplier_committed', '供应商': 'supplier_committed',
                'supplier_committed': 'supplier_committed',
            }
            inventory_type = inv_type_map.get(inventory_type_raw, inventory_type_raw) or 'local'

            # ===== 在途类型：收集待合并，不直接创建记录 =====
            if inventory_type in ('transit', '在途'):
                mid = material.id
                if mid not in transit_by_material:
                    transit_by_material[mid] = [0, 0]
                transit_by_material[mid][0] += quantity
                transit_by_material[mid][1] += hold_quantity
                continue

            # ===== 本地类型：收集 =====
            local_rows.append({
                'row_idx': i,
                'material': material,
                'warehouse': warehouse,
                'quantity': quantity,
                'hold_quantity': hold_quantity,
                'expiry_date': expiry_date,
                'location': location,
                'batch_no': row.get('批次号', row.get('batch_no', '')).strip(),
                'is_hold': is_hold,
                'hold_reason': row.get('Hold原因', row.get('冻结原因', '')).strip(),
                'hold_until': hold_until,
                'data_date': data_date,
                'row': row,
            })

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    # ---- 阶段1：处理所有本地行 ----
    mat_wh_qty = {}

    for lr in local_rows:
        try:
            wh_key = lr['warehouse']
            defaults = {
                'quantity': lr['quantity'],
                'hold_quantity': lr['hold_quantity'],
                'available_quantity': max(0, lr['quantity'] - lr['hold_quantity']),
                'inventory_type': '本地',
                'expiry_date': lr['expiry_date'],
                'location': lr['location'],
                'batch_no': lr['batch_no'],
                'is_hold': lr['is_hold'],
                'hold_reason': lr['hold_reason'],
                'hold_until': lr['hold_until'],
                'data_date': lr['data_date'],
                'safety_stock_lower': int(float(lr['row'].get('安全库存下限', lr['row'].get('safety_stock_lower', '0')))) if lr['row'].get('安全库存下限') or lr['row'].get('safety_stock_lower') else 0,
                'target_level': int(float(lr['row'].get('目标水位', lr['row'].get('target_level', '0')))) if lr['row'].get('目标水位') or lr['row'].get('target_level') else 0,
                'max_stock_upper': int(float(lr['row'].get('库存上限', lr['row'].get('库存 上限', lr['row'].get('max_stock_upper', '0'))))) if lr['row'].get('库存上限') or lr['row'].get('库存 上限') or lr['row'].get('max_stock_upper') else 0,
                'is_restricted': lr['row'].get('是否禁用', lr['row'].get('is_restricted', '')) in ('是', 'True', 'true', '1'),
                'restricted_reason': lr['row'].get('禁用原因', lr['row'].get('restricted_reason', '')).strip(),
            }

            obj, created = Inventory.objects.update_or_create(
                material=lr['material'],
                warehouse=wh_key,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

            mat_wh_qty[(lr['material'].id, wh_key)] = lr['quantity']

        except Exception as e:
            errors.append(f'第{lr["row_idx"]}行：{str(e)}')

    # ---- 阶段2：将在途合并到最小仓库 ----
    if transit_by_material:
        for mat_id, (t_qty, t_hold) in transit_by_material.items():
            if t_qty <= 0:
                continue
            wh_qtys = [(wh, q) for (mid, wh), q in mat_wh_qty.items() if mid == mat_id and q > 0]
            if not wh_qtys:
                continue
            wh_qtys.sort(key=lambda x: x[1])
            target_wh = wh_qtys[0][0]
            obj = Inventory.objects.filter(material_id=mat_id, warehouse=target_wh).first()
            if obj:
                obj.quantity = (obj.quantity or 0) + t_qty
                obj.hold_quantity = (obj.hold_quantity or 0) + t_hold
                obj.available_quantity = max(0, obj.quantity - obj.hold_quantity - (obj.locked_quantity or 0))
                obj.save(update_fields=['quantity', 'hold_quantity', 'available_quantity', 'updated_at'])
                updated += 1

    message = f'库存数据导入完成'
    if clean_import:
        message = f'库存数据已清空重新导入（清除旧记录 {deleted_count} 条）'
    if auto_created_materials > 0:
        message += f'（自动创建了 {auto_created_materials} 个缺失的物料）'
    if transit_by_material:
        message += f'（{len(transit_by_material)}个物料的在途库存已合并到最少仓库）'

    return {
        'status': 'success' if not errors else 'partial',
        'message': message,
        'imported': imported,
        'updated': updated,
        'auto_created': auto_created_materials,
        'errors': errors
    }

def import_order_data(rows):
    """导入订单数据（产品ID必须已存在于物料表中）"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            order_no = row.get('订单ID', row.get('order_no', '')).strip()
            if not order_no:
                errors.append(f'第{i}行：订单号不能为空')
                continue

            material_code = row.get('产品ID', row.get('成品ID', row.get('material_code', ''))).strip()
            if not material_code:
                errors.append(f'第{i}行：成品ID不能为空')
                continue

            # 校验成品物料是否已存在（不再自动创建，确保数据一致性）
            material = Material.objects.filter(material_code=material_code).first()
            if not material:
                errors.append(f'第{i}行：产品ID「{material_code}」在物料表中不存在，请先在物料表中维护该产品')
                continue

            # 【修复】CSV实际列名为'需求交付日 期'(中间含空格)，需精确匹配
            demand_date_str = row.get('需求交付日 期', row.get('需求交付日期', row.get('要求交期', row.get('demand_date', ''))))
            demand_date = datetime.now().date() + timedelta(days=7)
            if demand_date_str.strip():
                date_str = demand_date_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        demand_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            order_date_str = row.get('订单日期', row.get('下单日期', row.get('order_date', '')))
            order_date = None
            if order_date_str.strip():
                date_str = order_date_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        order_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            total_amount = round(float(row.get('总金额', row.get('total_amount', '0'))), 2)
            quantity = int(row.get('订单数量', row.get('quantity', '0')))
            unit_price_raw = round(float(row.get('单价', row.get('unit_price', '0'))), 2)
            unit_price = unit_price_raw if unit_price_raw > 0 else round(total_amount / quantity, 2) if quantity > 0 else 0

            # 优先级：支持中文和英文
            priority_raw = safe_str(row.get('优先级', row.get('优先级(1最高-5最低)', row.get('priority', '4'))))
            priority_val = PRIORITY_MAPPING.get(priority_raw.lower() if priority_raw.isascii() else priority_raw, 4)
            try:
                priority_val = int(priority_val)
            except (ValueError, TypeError):
                priority_val = 4

            # 交付优先级（客户要求的交付优先级，数值越小越优先）
            delivery_priority = int(float(safe_str(row.get('交付优先级')) or '5')) or 5

            # 运输天数映射
            shipping_method_raw = safe_str(row.get('运输方式', row.get('备注(空运/海运)', row.get('shipping_method', '海运'))))
            shipping_method = SHIPPING_METHOD_MAPPING.get(shipping_method_raw, 'sea')
            shipping_days_map = {'sea': 45, 'air': 3, 'express': 1, 'land': 7}

            defaults = {
                'material': material,
                'customer_name': row.get('客户名称', row.get('customer_name', '')).strip(),
                'quantity': quantity,
                'unit_price': unit_price,
                'total_amount': total_amount or (quantity * unit_price),
                'order_date': order_date,
                'demand_date': demand_date,
                'priority': priority_val,
                'status': ORDER_STATUS_MAPPING.get(safe_str(row.get('状态', row.get('status', 'pending'))), 'pending'),
                'shipping_method': shipping_method,
                'shipping_days': shipping_days_map.get(shipping_method, 45),
                'is_forecast': '预测' in safe_str(row.get('备注', '')),
            }

            obj, created = SalesOrder.objects.update_or_create(
                order_no=order_no,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    message = f'订单数据导入完成'

    return {
        'status': 'success' if not errors else 'partial',
        'message': message,
        'imported': imported,
        'updated': updated,
        'errors': errors
    }

def import_workcenter_data(rows):
    """导入工作中心数据（增强容错处理）"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            work_center_code = row.get('产线ID', row.get('work_center_code', '')).strip()
            if not work_center_code:
                errors.append(f'第{i}行：产线ID不能为空')
                continue

            # 数值安全转换函数
            def safe_float(val, default=0):
                try:
                    return float(str(val).replace(',', '')) if val else default
                except (ValueError, TypeError):
                    return default

            def safe_int(val, default=0):
                try:
                    return int(float(str(val).replace(',', ''))) if val else default
                except (ValueError, TypeError):
                    return default

            # 状态智能识别
            status_raw = str(row.get('状态', row.get('is_active', '1'))).strip().lower()
            is_active = status_raw not in ['0', 'false', 'no', '禁用', '停用', 'inactive', 'disabled']

            defaults = {
                'work_center_name': row.get('产线名称', row.get('work_center_name', work_center_code)).strip(),
                'available_products': row.get('可生产产品', row.get('available_products', '')).strip(),
                'daily_available_hours': safe_float(row.get('每日可用工时', row.get('daily_available_hours'))),
                'shift_count': safe_int(row.get('班次数', row.get('shift_count')), 1),
                'hours_per_shift': safe_float(row.get('每班工时', row.get('hours_per_shift')), 8),
                'production_days_per_week': safe_int(row.get('每周生产天数', row.get('production_days_per_week')), 5),
                'planned_headcount': safe_int(row.get('定编人数', row.get('planned_headcount'))),
                'actual_headcount': safe_int(row.get('在岗人数', row.get('actual_headcount'))),
                'daily_capacity_limit': safe_int(row.get('日产能上限', row.get('daily_capacity_limit'))),
                'changeover_time': safe_float(row.get('换线时间(小时/次)', row.get('changeover_time'))),
                'planned_maintenance_hours': safe_float(row.get('计划维护停机时长', row.get('计划维护停机时长(小时)', row.get('planned_maintenance_hours')))),
                'is_active': is_active
            }

            def parse_date_field(date_str):
                if not date_str or not str(date_str).strip():
                    return None
                date_str = str(date_str).strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        return datetime.strptime(date_str, fmt).date()
                    except ValueError:
                        continue
                return None

            maintenance_start = row.get('维护生效日期', row.get('maintenance_start_date', ''))
            maintenance_end = row.get('维护失效日期', row.get('maintenance_end_date', ''))
            start_date = parse_date_field(maintenance_start)
            end_date = parse_date_field(maintenance_end)

            # 校验：失效日期不能早于生效日期
            if start_date and end_date and end_date < start_date:
                errors.append(f'第{i}行：维护失效日期({end_date})不能早于生效日期({start_date})，请检查源数据')
                continue

            defaults['maintenance_start_date'] = start_date
            defaults['maintenance_end_date'] = end_date

            obj, created = WorkCenter.objects.update_or_create(
                work_center_code=work_center_code,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'工作中心数据导入完成（共 {len(rows)} 条记录）',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }

def import_customer_data(rows):
    """导入客户数据"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            customer_code = (safe_str(row.get('客户ID')) or safe_str(row.get('customer_code'))
                             or safe_str(row.get('客户编号')) or safe_str(row.get('编号'))
                             or safe_str(row.get('代码')) or safe_str(row.get('Code'))
                             or safe_str(row.get('ID')) or safe_str(row.get('客户代码')))
            if not customer_code:
                errors.append(f'第{i}行：客户代码不能为空')
                continue

            customer_name = (safe_str(row.get('客户名称')) or safe_str(row.get('customer_name'))
                            or safe_str(row.get('名称')) or safe_str(row.get('name'))
                            or safe_str(row.get('客户')))
            if not customer_name:
                errors.append(f'第{i}行：客户名称不能为空')
                continue

            credit_limit_str = (safe_str(row.get('信用额度')) or safe_str(row.get('credit_limit'))
                                or safe_str(row.get('额度')) or '100000')
            credit_limit = round(float(credit_limit_str.replace(',', '')), 2) if credit_limit_str else 100000.0

            is_active_val = (safe_str(row.get('是否启用')) or safe_str(row.get('状态'))
                             or safe_str(row.get('is_active')) or safe_str(row.get('启用')) or '1').lower()
            is_active = is_active_val not in ['0', 'false', '否', '禁用', 'inactive']

            # 客户类型映射
            customer_type_raw = (safe_str(row.get('客户类型')) or safe_str(row.get('customer_type'))
                                or safe_str(row.get('类型')) or '其他')
            type_mapping = {'海外': 'overseas', '电商': 'ecommerce', '运营商': 'operator',
                           '工程': 'engineering', '零售': 'retail', '集采': 'centralized'}
            customer_type = type_mapping.get(customer_type_raw, customer_type_raw)

            # 客户等级
            level_raw = (safe_str(row.get('客户等级')) or safe_str(row.get('customer_level'))
                        or safe_str(row.get('等级')) or 'normal')
            level_mapping = {'VIP': 'vip', 'vip': 'vip', '重要': 'important',
                            '普通': 'normal', '一般': 'normal',
                            'S级': 'vip', 'A级': 'important', 'B级': 'normal', 'C级': 'normal',
                            'S': 'vip', 'A': 'important', 'B': 'normal', 'C': 'normal'}
            customer_level = level_mapping.get(level_raw, level_raw.lower() if level_raw else 'normal')

            # 付款条件
            payment_terms = (safe_str(row.get('付款条件')) or safe_str(row.get('payment_terms'))
                            or safe_str(row.get('付款方式')) or '月结30天')

            # 交付优先级
            delivery_priority = int(float(safe_str(row.get('交付优先级')) or safe_str(row.get('delivery_priority'))
                                   or '5')) or 5

            defaults = {
                'customer_name': customer_name,
                'contact_person': (safe_str(row.get('联系人')) or safe_str(row.get('contact_person'))
                                   or safe_str(row.get('联系人姓名')) or safe_str(row.get('联络人'))),
                'phone': (safe_str(row.get('电话')) or safe_str(row.get('phone'))
                           or safe_str(row.get('联系电话')) or safe_str(row.get('手机'))),
                'email': (safe_str(row.get('邮箱')) or safe_str(row.get('email'))
                           or safe_str(row.get('电子邮箱')) or safe_str(row.get('邮件'))),
                'address': (safe_str(row.get('地址')) or safe_str(row.get('address'))
                             or safe_str(row.get('详细地址')) or safe_str(row.get('住址'))),
                'credit_limit': credit_limit,
                'customer_type': customer_type,
                'payment_terms': payment_terms,
                'customer_level': customer_level,
                'delivery_priority': delivery_priority,
                'is_active': is_active
            }

            obj, created = Customer.objects.update_or_create(
                customer_code=customer_code,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    message = f'客户数据导入完成'
    return {
        'status': 'success' if not errors else 'partial',
        'message': message,
        'imported': imported,
        'updated': updated,
        'errors': errors
    }

class BatchImportAllView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """批量导入所有CSV文件 - 优化版：共享预加载、详细日志"""
        overall_start = time.time()
        csv_files = get_csv_files_for_batch()
        clean_import = request.query_params.get('clean_import', 'false').lower() == 'true'

        results = []
        # 共享预加载：只做一次，所有导入函数共用
        _shared_materials = None
        _shared_suppliers = None
        _shared_customers = None

        for file_info in csv_files:
            file_path = file_info['path']
            import_type = file_info['type']
            file_name = file_info['name']

            if os.path.exists(file_path):
                try:
                    file_start = time.time()
                    result = import_csv_data(file_path, import_type, clean_import)
                    file_elapsed = time.time() - file_start
                    result['file'] = file_name
                    result['file_elapsed'] = round(file_elapsed, 2)
                    results.append(result)
                    logger.info(f'[一键导入] {file_name}: {result.get("status")} 耗时={file_elapsed:.2f}s 新增={result.get("imported",0)} 更新={result.get("updated",0)}')
                except Exception as e:
                    logger.error(f'[一键导入] {file_name} 异常: {e}')
                    results.append({
                        'file': file_name,
                        'status': 'error',
                        'message': str(e)
                    })
            else:
                results.append({
                    'file': file_name,
                    'status': 'error',
                    'message': '文件不存在'
                })

        total_elapsed = time.time() - overall_start
        logger.info(f'[一键导入] 全部完成! 总文件={len(csv_files)} 总耗时={total_elapsed:.2f}s')

        # 清除缓存确保前端立即看到最新数据（含各策略子缓存）
        safe_delete('screen_data_v2')
        safe_delete('inventory_cache')
        safe_delete('bom_cache')
        from ..tasks import _clear_all_planning_caches
        _clear_all_planning_caches()
        try:
            for key in list(cache.keys('mrp_*')):
                safe_delete(key)
        except (AttributeError, TypeError):
            pass

        return Response({'results': results, 'total_elapsed': round(total_elapsed, 2)})


class ImportHistoryListView(generics.ListAPIView):
    """获取导入历史记录列表"""
    permission_classes = [IsAuthenticated]
    serializer_class = None

    def get_queryset(self):
        return ImportHistory.objects.all().order_by('-created_at')

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        history_list = []
        for item in queryset:
            from django.utils.timezone import localtime
            local_time = localtime(item.created_at)
            history_list.append({
                'id': item.id,
                'type': item.get_import_type_display(),
                'filename': item.file_name,
                'status': item.get_status_display(),
                'count': item.imported_count + item.updated_count,
                'time': local_time.strftime('%Y-%m-%d %H:%M:%S'),
                'imported_by': item.imported_by.username if item.imported_by else '系统'
            })
        return Response({
            'count': len(history_list),
            'results': history_list
        })


class FieldRecognitionView(APIView):
    """字段识别API - 使用正则表达式自动识别CSV字段"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        识别CSV文件中的字段

        请求参数:
        - file: CSV文件
        - import_type: (可选) 导入类型，如果不提供则自动检测

        返回:
        - columns: 列识别结果
        - detected_type: 检测到的数据类型
        - field_mapping: 字段映射关系
        - validation: 必填字段验证结果
        """
        file = request.FILES.get('file')
        if not file:
            return Response({
                'status': 'error',
                'message': '请上传CSV文件'
            }, status=status.HTTP_400_BAD_REQUEST)

        # 文件大小限制：50MB
        MAX_FILE_SIZE = 50 * 1024 * 1024
        if file.size > MAX_FILE_SIZE:
            return Response({
                'status': 'error',
                'message': f'文件过大（{file.size/1024/1024:.1f}MB），限制50MB'
            }, status=status.HTTP_400_BAD_REQUEST)

        import_type = request.data.get('import_type', '').strip()

        try:
            # 读取文件（支持CSV和Excel）
            content = file.read()
            rows, columns = read_data_file(content, is_content=True)

            if not columns:
                return Response({
                    'status': 'error',
                    'message': '无法解析文件，请检查文件格式'
                }, status=status.HTTP_400_BAD_REQUEST)

            # 如果未指定类型，自动检测
            if not import_type:
                detected_type, confidence = auto_detect_data_type(columns)
                import_type = detected_type
            else:
                detected_type = import_type
                confidence = 1.0

            # 执行字段识别
            recognition_result = recognize_csv_columns(columns, import_type)

            # 生成字段映射
            field_mapping = get_field_mapping(recognition_result)

            # 验证必填字段
            validation = smart_recognizer.validate_required_fields(
                recognition_result, import_type
            )

            # 统计匹配情况
            matched_count = sum(1 for v in recognition_result.values() if v['matched'])
            total_count = len(columns)

            return Response({
                'status': 'success',
                'detected_type': detected_type,
                'type_confidence': round(confidence, 3),
                'columns': recognition_result,
                'field_mapping': field_mapping,
                'validation': validation,
                'statistics': {
                    'total_columns': total_count,
                    'matched_columns': matched_count,
                    'unmatched_columns': total_count - matched_count,
                    'match_rate': round(matched_count / total_count * 100, 1) if total_count > 0 else 0
                }
            })

        except Exception as e:
            return Response({
                'status': 'error',
                'message': f'字段识别失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SmartImportPreviewView(APIView):
    """智能导入预览 - 显示正则识别后的数据映射效果"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """
        预览智能导入的数据（前几行）

        请求参数:
        - file: CSV文件
        - import_type: 导入类型
        - preview_rows: 预览行数（默认5）
        """
        file = request.FILES.get('file')
        import_type = request.data.get('import_type', '')
        preview_rows = int(request.data.get('preview_rows', 5))

        if not file or not import_type:
            return Response({
                'status': 'error',
                'message': '请提供文件和导入类型'
            }, status=status.HTTP_400_BAD_REQUEST)

        # 文件大小限制：50MB
        MAX_FILE_SIZE = 50 * 1024 * 1024
        if file.size > MAX_FILE_SIZE:
            return Response({
                'status': 'error',
                'message': f'文件过大（{file.size/1024/1024:.1f}MB），限制50MB'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            content = file.read()
            all_rows, columns = read_data_file(content, is_content=True)

            if not columns:
                return Response({'status': 'error', 'message': '无法读取文件'})

            # 字段识别
            recognition_result = recognize_csv_columns(columns, import_type)
            field_mapping = get_field_mapping(recognition_result)

            # 转换数据为字典列表，使用系统字段名
            preview_data = []
            for idx, row in enumerate(all_rows[:preview_rows]):
                mapped_row = {}
                for original_col, value in row.items():
                    sys_key = field_mapping.get(original_col)
                    if sys_key:
                        mapped_row[sys_key] = value
                    else:
                        # 未识别的列保留原名
                        mapped_row[original_col] = value

                preview_data.append({
                    'row_number': idx + 2,  # +2 因为跳过表头且从1开始计数
                    'data': mapped_row,
                    'original_data': row
                })

            return Response({
                'status': 'success',
                'import_type': import_type,
                'field_mapping': field_mapping,
                'recognition_details': recognition_result,
                'preview_data': preview_data,
                'total_rows_previewed': len(preview_data),
                'columns_info': {
                    'original_columns': columns,
                    'mapped_columns': list(field_mapping.values()),
                    'unmapped_columns': [c for c in columns if c not in field_mapping]
                }
            })

        except Exception as e:
            return Response({
                'status': 'error',
                'message': f'预览失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def import_purchase_data(rows):
    """导入采购订单数据"""
    from prediction.models import PurchaseOrder
    imported = 0
    updated = 0
    errors = []

    status_map = {
        # 中文输入
        '草稿': 'draft', '待处理': 'pending', '已下达': 'issued',
        '已确认': 'confirmed', '生产中': 'in_production',
        '部分到货': 'partial', '已完成': 'completed', '已取消': 'cancelled',
        '已发货': 'shipped', '部分发货': 'partial_shipped', '进行中': 'processing',
        # 英文输入（兼容）
        'draft': 'draft', 'pending': 'pending', 'issued': 'issued',
        'confirmed': 'confirmed', 'in_production': 'in_production',
        'partial': 'partial', 'completed': 'completed', 'cancelled': 'cancelled',
        'shipped': 'shipped', 'partial_shipped': 'partial_shipped', 'processing': 'processing'
    }

    for i, row in enumerate(rows, 1):
        try:
            po_no = (safe_str(row.get('采购订单号')) or safe_str(row.get('po_no'))
                     or safe_str(row.get('订单号')) or safe_str(row.get('订单编号'))
                     or safe_str(row.get('PO编号')) or safe_str(row.get('ID')))
            if not po_no:
                errors.append(f'第{i}行：采购订单号不能为空')
                continue

            supplier_code = (safe_str(row.get('供应商代码')) or safe_str(row.get('supplier_code'))
                            or safe_str(row.get('供应商ID')) or safe_str(row.get('supplier_id'))
                            or safe_str(row.get('供应商编号')))
            if not supplier_code:
                errors.append(f'第{i}行：供应商代码不能为空')
                continue

            material_code = (safe_str(row.get('物料代码')) or safe_str(row.get('material_code'))
                           or safe_str(row.get('物料ID')) or safe_str(row.get('material_id'))
                           or safe_str(row.get('物料编号')))
            if not material_code:
                errors.append(f'第{i}行：物料代码不能为空')
                continue

            supplier = Supplier.objects.filter(supplier_code=supplier_code).first()
            if not supplier:
                errors.append(f'第{i}行：供应商 {supplier_code} 不存在')
                continue

            material = Material.objects.filter(material_code=material_code).first()
            if not material:
                errors.append(f'第{i}行：物料 {material_code} 不存在')
                continue

            quantity = int(float(safe_str(row.get('订单数量')) or safe_str(row.get('quantity')) or '0'))
            if quantity <= 0:
                errors.append(f'第{i}行：订单数量必须大于0')
                continue

            unit_price = round(float(safe_str(row.get('单价')) or safe_str(row.get('unit_price'))
                             or str(float(material.standard_cost or 0))), 2)
            total_amount = round(float(safe_str(row.get('总金额')) or safe_str(row.get('total_amount'))
                              or quantity * unit_price), 2)

            order_date_str = safe_str(row.get('下单日期')) or safe_str(row.get('order_date'))
            order_date = None
            if order_date_str:
                date_str = order_date_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        order_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue
            if not order_date:
                order_date = datetime.now().date()

            # 交期天数：优先使用CSV中的"交期天数"，其次用供应商正常交期，默认7天
            delivery_days_raw = safe_str(row.get('交期天数')) or safe_str(row.get('交期'))
            delivery_days = supplier.normal_lead_time or 7
            if delivery_days_raw:
                try:
                    delivery_days = int(float(delivery_days_raw))
                except (ValueError, TypeError):
                    pass

            delivery_date_str = safe_str(row.get('预计交付日期')) or safe_str(row.get('delivery_date')) or safe_str(row.get('交期日期'))
            delivery_date = None
            if delivery_date_str:
                date_str = delivery_date_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        delivery_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue
            if not delivery_date:
                delivery_date = order_date + timedelta(days=delivery_days)

            actual_delivery_str = safe_str(row.get('实际交付日期')) or safe_str(row.get('actual_delivery'))
            actual_delivery = None
            if actual_delivery_str:
                date_str = actual_delivery_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        actual_delivery = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            status_raw = (safe_str(row.get('状态')) or safe_str(row.get('status'))
                        or safe_str(row.get('订单状态')) or 'confirmed').strip()
            status = status_map.get(status_raw, status_map.get(status_raw.lower(), 'confirmed'))

            defaults = {
                'supplier': supplier,
                'material': material,
                'quantity': quantity,
                'unit_price': unit_price,
                'total_amount': total_amount,
                'order_date': order_date,
                'delivery_date': delivery_date,
                'status': status,
            }
            if actual_delivery:
                defaults['actual_delivery_date'] = actual_delivery

            obj, created = PurchaseOrder.objects.update_or_create(po_no=po_no, defaults=defaults)
            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def smart_import_with_regex(file_path, import_type, clean_import=False):
    """使用正则表达式增强的智能导入函数

    Args:
        file_path: 文件路径
        import_type: 导入类型
        clean_import: 是否清空现有数据后重新导入（默认False）
    """
    results = {
        'status': 'success',
        'message': '智能导入成功',
        'imported': 0,
        'updated': 0,
        'errors': [],
        'field_mapping': {},
        'recognition_stats': {}
    }

    try:
        rows, columns = read_data_file(file_path, is_content=False)

        if not rows:
            results['status'] = 'error'
            results['message'] = '文件为空'
            return results

        rows = sanitize_rows(rows)
        recognition_result = recognize_csv_columns(columns, import_type)
        field_mapping = get_field_mapping(recognition_result)

        results['field_mapping'] = field_mapping
        results['recognition_stats'] = {
            'total_columns': len(columns),
            'matched': sum(1 for v in recognition_result.values() if v['matched']),
            'unmatched': sum(1 for v in recognition_result.values() if not v['matched'])
        }

        # 使用识别结果转换数据格式
        transformed_rows = []
        for row in rows:
            transformed_row = {}
            for original_key, value in row.items():
                sys_key = field_mapping.get(original_key)
                if sys_key:
                    transformed_row[sys_key] = value
                else:
                    # 保留未识别的字段
                    transformed_row[original_key] = value
            transformed_rows.append(transformed_row)

        # 根据类型调用对应的导入函数
        if import_type == 'material':
            results = import_material_data_smart(transformed_rows, field_mapping)
        elif import_type in ('supplier', '供应商承诺'):
            results = import_supplier_data_smart(transformed_rows, field_mapping)
        elif import_type == 'customer':
            results = import_customer_data_smart(transformed_rows, field_mapping)
        elif import_type == 'bom':
            results = import_bom_data_smart(transformed_rows, field_mapping)
        elif import_type == 'inventory':
            results = import_inventory_data_smart(transformed_rows, field_mapping, clean_import=clean_import)
        elif import_type == 'order':
            results = import_order_data_smart(transformed_rows, field_mapping)
        elif import_type == 'purchase':
            results = import_purchase_data_smart(transformed_rows, field_mapping)
        elif import_type == 'workcenter':
            results = import_workcenter_data_smart(transformed_rows, field_mapping)
        elif import_type == 'config':
            results = import_config_data(transformed_rows)
        elif import_type == 'factory_calendar':
            results = import_factory_calendar_data(transformed_rows)
        elif import_type == 'factory_transfer':
            results = import_factory_transfer_data(transformed_rows)
        elif import_type == 'priority_rule':
            results = import_priority_rule_data(transformed_rows)
        elif import_type == 'engineering_change':
            results = import_engineering_change_data(transformed_rows)
        elif import_type == 'factory_calendar_transfer':
            results = import_factory_calendar_transfer_data(transformed_rows)
        elif import_type == 'config_rules_ecn':
            results = import_config_rules_ecn_data(transformed_rows)
        elif import_type == 'substitute':
            from .models import SubstituteMaterial
            results = _batch_import_substitute(transformed_rows, clean_import=clean_import)
        elif import_type == 'delivery_change':
            results = import_delivery_change_data(transformed_rows)
        else:
            results['status'] = 'error'
            results['message'] = '不支持的导入类型'

        # 合并字段映射信息到结果中
        results['field_mapping'] = field_mapping
        results['used_smart_recognition'] = True

    except Exception as e:
        results['status'] = 'error'
        results['message'] = f'智能导入处理异常: {str(e)}'
        results['errors'].append(f'系统错误: {str(e)}')

    return results


# ==================== 智能导入函数（使用正则识别后的标准化字段名） ====================

def import_material_data_smart(rows, field_mapping):
    """使用正则识别结果的物料导入"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            material_code = safe_str(row.get('material_code'))
            if not material_code:
                errors.append(f'第{i}行：物料代码不能为空')
                continue

            material_type_raw = safe_str(row.get('material_type'), 'raw')
            type_mapping = {
                '原材料': 'raw', '半成品': 'semi', '成品': 'finished',
                'raw': 'raw', 'semi': 'semi', 'semi_finished': 'semi', 'finished': 'finished'
            }
            material_type = type_mapping.get(material_type_raw.lower(), 'raw')

            cost_str = safe_str(row.get('standard_cost'), '0')
            standard_cost = round(float(cost_str.replace(',', '')), 2) if cost_str else 0

            safety_stock_str = safe_str(row.get('safety_stock'))
            safety_stock = int(float(safety_stock_str.replace(',', ''))) if safety_stock_str else \
                          int(safe_str(row.get('min_order_qty'), '100') or '100') * 2

            shelf_life_str = safe_str(row.get('shelf_life'), '0')
            shelf_life = int(shelf_life_str.replace(',', '').replace('天', '')) if shelf_life_str else 0

            defaults = {
                'material_name': safe_str(row.get('material_name')),
                'material_type': material_type,
                'unit': safe_str(row.get('unit'), '件'),
                'shelf_life': shelf_life,
                'min_order_qty': int(safe_str(row.get('min_order_qty'), '1') or '1'),
                'lead_time': int(safe_str(row.get('lead_time'), '7') or '7'),
                'standard_cost': standard_cost,
                'sales_price': round(float(safe_str(row.get('sales_price'), '0').replace(',', '') or '0'), 2),
                'safety_stock': safety_stock,
                'min_production_qty': int(safe_str(row.get('min_production_qty'), '1') or '1'),
                'is_active': True
            }

            obj, created = Material.objects.update_or_create(
                material_code=material_code,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

            main_supplier = safe_str(row.get('main_supplier')) or safe_str(row.get('supplier_code'))
            if main_supplier:
                supplier = Supplier.objects.filter(
                    supplier_code__icontains=main_supplier
                ).first()

                if not supplier:
                    supplier_name = row.get('supplier_name', main_supplier)
                    supplier, _ = Supplier.objects.get_or_create(
                        supplier_code=main_supplier,
                        defaults={
                            'supplier_name': safe_str(supplier_name),
                            'is_active': True
                        }
                    )

                SupplierMaterial.objects.update_or_create(
                    material=obj,
                    supplier=supplier,
                    defaults={
                        'unit_price': standard_cost,
                        'min_order_qty': int(safe_str(row.get('min_order_qty'), '1')),
                        'lead_time': int(safe_str(row.get('lead_time'), '7')),
                        'is_forbidden': False
                    }
                )

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'物料数据智能导入完成（使用正则识别）',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_supplier_data_smart(rows, field_mapping):
    """使用正则识别结果的供应商导入"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            supplier_code = safe_str(row.get('supplier_code'))
            if not supplier_code:
                errors.append(f'第{i}行：供应商代码不能为空')
                continue

            defaults = {
                'supplier_name': safe_str(row.get('supplier_name')),
                'contact_person': safe_str(row.get('contact_person')),
                'phone': safe_str(row.get('phone')),
                'email': safe_str(row.get('email')),
                'address': safe_str(row.get('address')),
                'rating': SUPPLIER_RATING_MAPPING.get(safe_str(row.get('rating')), 'B'),
                'delivery_reliability': round(float(row.get('delivery_reliability', '0.9') or 0.9), 3),
                'normal_lead_time': int(row.get('normal_lead_time', '7') or 7),
                'is_active': row.get('status', '1') != '0'
            }

            obj, created = Supplier.objects.update_or_create(
                supplier_code=supplier_code,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'供应商数据智能导入完成',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_bom_data_smart(rows, field_mapping):
    """使用正则识别结果的BOM导入"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            parent_code = safe_str(row.get('parent_code'))
            child_code = safe_str(row.get('child_code'))

            if not parent_code or not child_code:
                errors.append(f'第{i}行：父物料或子物料代码不能为空')
                continue

            parent_material = Material.objects.filter(material_code=parent_code).first()
            child_material = Material.objects.filter(material_code=child_code).first()

            if not parent_material:
                errors.append(f'第{i}行：父物料 {parent_code} 不存在')
                continue
            if not child_material:
                errors.append(f'第{i}行：子物料 {child_code} 不存在')
                continue

            # 读取并校验可替代物料列（逗号分隔的物料代码）
            alt_raw = safe_str(row.get('alternative_group', row.get('可替代物料', ''))).strip()
            alt_codes = [c.strip() for c in alt_raw.split(',') if c.strip()] if alt_raw else []
            if alt_codes:
                valid_codes = [c for c in alt_codes if Material.objects.filter(material_code=c).exists()]
                alternative_group = ','.join(valid_codes) if valid_codes else ''
            else:
                alternative_group = ''

            defaults = {
                'quantity': int(round(float(row.get('quantity', '1') or 1))),
                'unit': safe_str(row.get('unit'), '件'),
                'bom_level': int(row.get('结构层级', row.get('BOM层级', '1')) or 1),
                'usage_ratio': round(float(row.get('usage_ratio', '0') or 0), 2),
                'scrap_rate': round(float(row.get('scrap_rate', '0') or 0), 3),
                'alternative_group': alternative_group,
                'alternative_priority': int(row.get('alternative_priority', '1') or 1),
                'alternative_ratio': round(float(row.get('alternative_ratio', '1.0') or 1.0), 2),
                'is_active': row.get('status', '1') != '0'
            }

            obj, created = BillOfMaterials.objects.update_or_create(
                parent_material=parent_material,
                child_material=child_material,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'BOM数据智能导入完成',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_inventory_data_smart(rows, field_mapping, clean_import=False):
    """使用正则识别结果的库存导入（在途合并到最小仓库）"""
    imported = 0
    updated = 0
    errors = []
    auto_created_materials = 0

    if clean_import:
        deleted_count, _ = Inventory.objects.all().delete()

    # ---- 两阶段收集 ----
    local_rows = []
    transit_by_material = {}

    for i, row in enumerate(rows, 1):
        try:
            material_code = safe_str(row.get('material_code'))
            if not material_code:
                errors.append(f'第{i}行：物料代码不能为空')
                continue

            material = Material.objects.filter(material_code=material_code).first()
            if not material:
                material_name = safe_str(row.get('material_name', material_code))
                material, _ = Material.objects.get_or_create(
                    material_code=material_code,
                    defaults={
                        'material_name': material_name if material_name else material_code,
                        'material_type': 'raw',
                        'unit': '件',
                        'is_active': True
                    }
                )
                auto_created_materials += 1

            expiry_date_str = row.get('expiry_date', '') or ''
            expiry_date = None
            if expiry_date_str and str(expiry_date_str).strip():
                date_str = str(expiry_date_str).strip().split(' ')[0]
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        expiry_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            hold_qty = str(row.get('冻结数量', '0') or '0')
            hold_quantity = int(float(hold_qty)) if hold_qty not in ['', 'None'] else 0

            quantity = int(float(row.get('quantity', '0') or 0))

            if quantity < 0:
                errors.append(f'第{i}行：在库数量不能为负数（{quantity}）')
                continue

            wh_key = safe_str(row.get('warehouse')) or '主仓库'
            inv_type_raw = safe_str(row.get('inventory_type'), '本地').lower()
            inv_type_map = {
                '本地': 'local', 'local': 'local',
                '在途': 'transit', 'transit': 'transit', '运输中': 'transit',
            }
            inventory_type = inv_type_map.get(inv_type_raw, inv_type_raw) or 'local'

            # ===== 在途：收集 =====
            if inventory_type in ('transit', '在途'):
                mid = material.id
                if mid not in transit_by_material:
                    transit_by_material[mid] = [0, 0]
                transit_by_material[mid][0] += quantity
                transit_by_material[mid][1] += hold_quantity
                continue

            data_date_str = safe_str(row.get('data_date'))
            data_date = None
            if data_date_str:
                date_str = data_date_str.split(' ')[0]
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        data_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            hold_until_str = safe_str(row.get('预计解Hold日期'))
            hold_until = None
            if hold_until_str:
                date_str = hold_until_str.split(' ')[0]
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        hold_until = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            local_rows.append({
                'row_idx': i,
                'material': material,
                'wh_key': wh_key,
                'quantity': quantity,
                'hold_quantity': hold_quantity,
                'expiry_date': expiry_date,
                'location': safe_str(row.get('location')),
                'batch_no': safe_str(row.get('batch_no')),
                'hold_reason': safe_str(row.get('冻结原因')),
                'hold_until': hold_until,
                'data_date': data_date,
            })

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    # ---- 阶段1：处理本地行 ----
    mat_wh_qty = {}
    for lr in local_rows:
        defaults = {
            'quantity': lr['quantity'],
            'hold_quantity': lr['hold_quantity'],
            'available_quantity': max(0, lr['quantity'] - lr['hold_quantity']),
            'inventory_type': '本地',
            'expiry_date': lr['expiry_date'],
            'location': lr['location'],
            'batch_no': lr['batch_no'],
            'is_hold': lr['hold_quantity'] > 0,
            'hold_reason': lr['hold_reason'],
            'hold_until': lr['hold_until'],
            'data_date': lr['data_date']
        }
        obj, created = Inventory.objects.update_or_create(
            material=lr['material'],
            warehouse=lr['wh_key'],
            defaults=defaults
        )
        if created:
            imported += 1
        else:
            updated += 1
        mat_wh_qty[(lr['material'].id, lr['wh_key'])] = lr['quantity']

    # ---- 阶段2：将在途合并到最小仓库 ----
    if transit_by_material:
        for mat_id, (t_qty, t_hold) in transit_by_material.items():
            if t_qty <= 0:
                continue
            wh_qtys = [(wh, q) for (mid, wh), q in mat_wh_qty.items() if mid == mat_id and q > 0]
            if not wh_qtys:
                continue
            wh_qtys.sort(key=lambda x: x[1])
            target_wh = wh_qtys[0][0]
            obj = Inventory.objects.filter(material_id=mat_id, warehouse=target_wh).first()
            if obj:
                obj.quantity = (obj.quantity or 0) + t_qty
                obj.hold_quantity = (obj.hold_quantity or 0) + t_hold
                obj.available_quantity = max(0, obj.quantity - obj.hold_quantity - (obj.locked_quantity or 0))
                obj.save(update_fields=['quantity', 'hold_quantity', 'available_quantity', 'updated_at'])
                updated += 1

    message = f'库存数据智能导入完成'
    if clean_import:
        message = f'库存数据已清空重新导入（清除旧记录 {deleted_count} 条）'
    if auto_created_materials > 0:
        message += f'（自动创建了 {auto_created_materials} 个缺失的物料）'
    if transit_by_material:
        message += f'（{len(transit_by_material)}个物料的在途库存已合并到最少仓库）'

    return {
        'status': 'success' if not errors else 'partial',
        'message': message,
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_order_data_smart(rows, field_mapping):
    """使用正则识别结果的订单导入"""
    imported = 0
    updated = 0
    errors = []
    auto_created_materials = 0

    for i, row in enumerate(rows, 1):
        try:
            order_no = safe_str(row.get('order_no'))
            if not order_no:
                errors.append(f'第{i}行：订单号不能为空')
                continue

            material_code = safe_str(row.get('material_code'))
            if not material_code:
                errors.append(f'第{i}行：成品ID不能为空')
                continue

            material = Material.objects.filter(material_code=material_code).first()
            if not material:
                material_name = safe_str(row.get('material_name', material_code))
                unit_price = round(float(row.get('unit_price', row.get('standard_cost', '0')) or 0), 2)
                material, _ = Material.objects.get_or_create(
                    material_code=material_code,
                    defaults={
                        'material_name': material_name if material_name else material_code,
                        'material_type': 'finished',
                        'unit': '件',
                        'standard_cost': unit_price,
                        'is_active': True
                    }
                )
                auto_created_materials += 1

            demand_date_str = row.get('demand_date', '') or ''
            demand_date = datetime.now().date() + timedelta(days=7)
            if demand_date_str and str(demand_date_str).strip():
                date_str = str(demand_date_str).strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        demand_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            order_date_str = row.get('order_date', '') or ''
            order_date = None
            if order_date_str and str(order_date_str).strip():
                date_str = str(order_date_str).strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        order_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            quantity = int(row.get('quantity', '0') or 0)
            total_amount = round(float(row.get('total_amount', '0') or 0), 2)
            unit_price = round(float(row.get('unit_price', '0') or 0), 2) or round(total_amount / quantity, 2) if quantity > 0 else 0

            defaults = {
                'material': material,
                'customer_name': safe_str(row.get('customer_name')),
                'quantity': quantity,
                'unit_price': unit_price,
                'total_amount': total_amount or (quantity * unit_price),
                'order_date': order_date,
                'demand_date': demand_date,
                'priority': int(row.get('priority', '3') or 3),
                'status': ORDER_STATUS_MAPPING.get(safe_str(row.get('status')), 'pending'),
                'shipping_method': SHIPPING_METHOD_MAPPING.get(safe_str(row.get('shipping_method')), 'land'),
                'shipping_days': 0
            }

            obj, created = SalesOrder.objects.update_or_create(
                order_no=order_no,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    message = f'订单数据智能导入完成'
    if auto_created_materials > 0:
        message += f'（自动创建了 {auto_created_materials} 个缺失的成品物料）'

    return {
        'status': 'success' if not errors else 'partial',
        'message': message,
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_purchase_data_smart(rows, field_mapping):
    """使用正则识别结果的采购订单导入"""
    from prediction.models import PurchaseOrder
    imported = 0
    updated = 0
    errors = []

    status_map = {
        # 中文输入
        '草稿': 'draft', '待处理': 'pending', '已下达': 'issued',
        '已确认': 'confirmed', '生产中': 'in_production',
        '部分到货': 'partial', '已完成': 'completed', '已取消': 'cancelled',
        '已发货': 'shipped', '部分发货': 'partial_shipped', '进行中': 'processing',
        # 英文输入（兼容）
        'draft': 'draft', 'pending': 'pending', 'issued': 'issued',
        'confirmed': 'confirmed', 'in_production': 'in_production',
        'partial': 'partial', 'completed': 'completed', 'cancelled': 'cancelled',
        'shipped': 'shipped', 'partial_shipped': 'partial_shipped', 'processing': 'processing'
    }

    for i, row in enumerate(rows, 1):
        try:
            po_no = (safe_str(row.get('采购订单号')) or safe_str(row.get('po_no'))
                     or safe_str(row.get('订单号')) or safe_str(row.get('订单编号')))
            if not po_no:
                errors.append(f'第{i}行：采购订单号不能为空')
                continue

            supplier_code = (safe_str(row.get('供应商代码')) or safe_str(row.get('supplier_code'))
                            or safe_str(row.get('供应商ID')) or safe_str(row.get('供应商编号')))
            if not supplier_code:
                errors.append(f'第{i}行：供应商代码不能为空')
                continue

            material_code = (safe_str(row.get('物料代码')) or safe_str(row.get('material_code'))
                           or safe_str(row.get('物料ID')) or safe_str(row.get('物料编号')))
            if not material_code:
                errors.append(f'第{i}行：物料代码不能为空')
                continue

            supplier = Supplier.objects.filter(supplier_code=supplier_code).first()
            if not supplier:
                errors.append(f'第{i}行：供应商 {supplier_code} 不存在')
                continue

            material = Material.objects.filter(material_code=material_code).first()
            if not material:
                errors.append(f'第{i}行：物料 {material_code} 不存在')
                continue

            quantity = int(float(safe_str(row.get('订单数量')) or safe_str(row.get('quantity')) or '0'))
            if quantity <= 0:
                errors.append(f'第{i}行：订单数量必须大于0')
                continue

            unit_price = round(float(safe_str(row.get('单价')) or safe_str(row.get('unit_price'))
                             or str(float(material.standard_cost or 0))), 2)
            total_amount = round(float(safe_str(row.get('总金额')) or safe_str(row.get('total_amount'))
                              or quantity * unit_price), 2)

            order_date_str = safe_str(row.get('下单日期')) or safe_str(row.get('order_date'))
            order_date = None
            if order_date_str:
                date_str = order_date_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        order_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue
            if not order_date:
                order_date = datetime.now().date()

            # 交期天数：优先使用CSV中的"交期天数"，其次用供应商正常交期，默认7天
            delivery_days_raw = safe_str(row.get('交期天数')) or safe_str(row.get('交期'))
            delivery_days = supplier.normal_lead_time or 7
            if delivery_days_raw:
                try:
                    delivery_days = int(float(delivery_days_raw))
                except (ValueError, TypeError):
                    pass

            delivery_date_str = safe_str(row.get('预计交付日期')) or safe_str(row.get('delivery_date'))
            delivery_date = None
            if delivery_date_str:
                date_str = delivery_date_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        delivery_date = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue
            if not delivery_date:
                delivery_date = order_date + timedelta(days=delivery_days)

            actual_delivery_str = safe_str(row.get('实际交付日期')) or safe_str(row.get('actual_delivery'))
            actual_delivery = None
            if actual_delivery_str:
                date_str = actual_delivery_str.strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        actual_delivery = datetime.strptime(date_str, fmt).date()
                        break
                    except ValueError:
                        continue

            status_raw = (safe_str(row.get('状态')) or safe_str(row.get('status'))
                        or safe_str(row.get('订单状态')) or 'confirmed').strip()
            status = status_map.get(status_raw, status_map.get(status_raw.lower(), 'confirmed'))

            defaults = {
                'supplier': supplier,
                'material': material,
                'quantity': quantity,
                'unit_price': unit_price,
                'total_amount': total_amount,
                'order_date': order_date,
                'delivery_date': delivery_date,
                'status': status,
            }
            if actual_delivery:
                defaults['actual_delivery_date'] = actual_delivery

            obj, created = PurchaseOrder.objects.update_or_create(po_no=po_no, defaults=defaults)
            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    message = f'导入完成，新增{imported}条，更新{updated}条'
    return {
        'status': 'success' if not errors else 'partial',
        'message': message,
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_workcenter_data_smart(rows, field_mapping):
    """使用正则识别结果的工作中心导入"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            work_center_code = safe_str(row.get('work_center_code'))
            if not work_center_code:
                errors.append(f'第{i}行：产线ID不能为空')
                continue

            def parse_date_field(date_str):
                if not date_str or not str(date_str).strip():
                    return None
                date_str = str(date_str).strip().split(' ')[0]
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                if '/' in date_str:
                    parts = date_str.split('/')
                    if len(parts) == 3:
                        date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        return datetime.strptime(date_str, fmt).date()
                    except ValueError:
                        continue
                return None

            defaults = {
                'work_center_name': safe_str(row.get('work_center_name')),
                'available_products': safe_str(row.get('available_products')),
                'daily_available_hours': round(float(row.get('daily_available_hours', '0') or 0), 2),
                'shift_count': int(row.get('shift_count', '1') or 1),
                'hours_per_shift': round(float(row.get('hours_per_shift', '8') or 8), 2),
                'production_days_per_week': int(row.get('production_days_per_week', '5') or 5),
                'planned_headcount': int(row.get('planned_headcount', '0') or 0),
                'actual_headcount': int(row.get('actual_headcount', '0') or 0),
                'daily_capacity_limit': int(float(row.get('daily_capacity_limit', '0') or 0)),
                'changeover_time': round(float(row.get('changeover_time', '0') or 0), 2),
                'planned_maintenance_hours': round(float(row.get('planned_maintenance_hours', '0') or 0), 2),
                'maintenance_start_date': parse_date_field(row.get('maintenance_start_date')),
                'maintenance_end_date': parse_date_field(row.get('maintenance_end_date')),
                'is_active': row.get('status', '1') != '0'
            }

            obj, created = WorkCenter.objects.update_or_create(
                work_center_code=work_center_code,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'工作中心数据智能导入完成',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_customer_data_smart(rows, field_mapping):
    """使用正则识别结果的客户导入"""
    imported = 0
    updated = 0
    errors = []

    for i, row in enumerate(rows, 1):
        try:
            customer_code = safe_str(row.get('customer_code'))
            if not customer_code:
                errors.append(f'第{i}行：客户代码不能为空')
                continue

            customer_name = safe_str(row.get('customer_name'))
            if not customer_name:
                errors.append(f'第{i}行：客户名称不能为空')
                continue

            credit_limit_str = safe_str(row.get('credit_limit'), '100000')
            credit_limit = round(float(credit_limit_str.replace(',', '')), 2) if credit_limit_str else 100000.0

            is_active_val = safe_str(row.get('is_active'), '1').lower()
            is_active = is_active_val not in ['0', 'false', '否', '禁用']

            customer_type_raw = safe_str(row.get('customer_type'), '其他')
            type_mapping = {'海外': 'overseas', '电商': 'ecommerce', '运营商': 'operator',
                           '工程': 'engineering', '零售': 'retail', '集采': 'centralized'}
            customer_type = type_mapping.get(customer_type_raw, customer_type_raw)

            level_raw = safe_str(row.get('customer_level'), 'normal')
            level_mapping = {'VIP': 'vip', 'vip': 'vip', '重要': 'important', '普通': 'normal', '一般': 'normal'}
            customer_level = level_mapping.get(level_raw, 'normal')

            defaults = {
                'customer_name': customer_name,
                'contact_person': safe_str(row.get('contact_person')),
                'phone': safe_str(row.get('phone')),
                'email': safe_str(row.get('email')),
                'address': safe_str(row.get('address')),
                'credit_limit': credit_limit,
                'customer_type': customer_type,
                'payment_terms': safe_str(row.get('payment_terms'), '月结30天'),
                'customer_level': customer_level,
                'delivery_priority': int(float(safe_str(row.get('delivery_priority'), '5'))) or 5,
                'is_active': is_active
            }

            obj, created = Customer.objects.update_or_create(
                customer_code=customer_code,
                defaults=defaults
            )

            if created:
                imported += 1
            else:
                updated += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'客户数据智能导入完成',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_factory_calendar_transfer_data(rows):
    """导入工厂日历与调拨数据（拆分后的独立表）"""
    imported = 0
    updated = 0
    errors = []

    calendar_rows = []
    transfer_rows = []

    for row in rows:
        data_type = row.get('数据类型', '').strip()
        if data_type == '工厂日历':
            calendar_rows.append(row)
        elif data_type == '工厂调拨':
            transfer_rows.append(row)

    # 处理工厂日历
    if calendar_rows:
        from prediction.models import FactoryCalendar
        for i, row in enumerate(calendar_rows, 1):
            try:
                date_str = safe_str(row.get('日期'))
                if not date_str:
                    continue
                is_workday = safe_str(row.get('是否工作日', '是')).strip() in ['是', '1', 'True', 'true']
                shift_type = safe_str(row.get('班次类型', '正常'))
                shift_count = 1 if shift_type == '正常' else (2 if shift_type == '加班' else 0)
                remark = safe_str(row.get('备注', ''))

                date_val = None
                normalized_cal = date_str.strip()
                if '/' in normalized_cal:
                    parts = normalized_cal.split('/')
                    if len(parts) == 3:
                        normalized_cal = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        date_val = datetime.strptime(normalized_cal, fmt).date()
                        break
                    except ValueError:
                        continue

                if date_val:
                    fc = safe_str(row.get('工厂代码', '')).strip()
                    factory_code = fc if fc else 'DEFAULT'
                    FactoryCalendar.objects.update_or_create(
                        factory_code=factory_code,
                        date=date_val,
                        defaults={
                            'is_workday': is_workday,
                            'shift_count': shift_count,
                            'remarks': remark
                        }
                    )
                    imported += 1
            except Exception as e:
                errors.append(f'工厂日历第{i}行：{str(e)}')

    # 处理工厂调拨
    if transfer_rows:
        from prediction.models import FactoryTransfer
        for i, row in enumerate(transfer_rows, 1):
            try:
                transfer_no = safe_str(row.get('调拨编号'))
                if not transfer_no:
                    continue
                material_code = safe_str(row.get('物料ID'))
                from_factory = safe_str(row.get('源工厂'))
                to_factory = safe_str(row.get('目标工厂'))
                quantity = int(float(safe_str(row.get('调拨数量', '0'))))

                transfer_date_str = safe_str(row.get('调拨日期'))
                transfer_date = None
                if transfer_date_str:
                    normalized_td = transfer_date_str.strip()
                    if '/' in normalized_td:
                        parts = normalized_td.split('/')
                        if len(parts) == 3:
                            normalized_td = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                    for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            transfer_date = datetime.strptime(normalized_td, fmt).date()
                            break
                        except ValueError:
                            continue

                arrive_date_str = safe_str(row.get('预计到达日期'))
                arrive_date = None
                if arrive_date_str:
                    normalized_ad = arrive_date_str.strip()
                    if '/' in normalized_ad:
                        parts = normalized_ad.split('/')
                        if len(parts) == 3:
                            normalized_ad = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                    for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            arrive_date = datetime.strptime(normalized_ad, fmt).date()
                            break
                        except ValueError:
                            continue

                status_raw = safe_str(row.get('状态', '待调拨'))
                status_map = {
                    '已完成': 'completed', '进行中': '在途', '待调拨': 'pending'
                }
                status = status_map.get(status_raw, 'pending')

                material = Material.objects.filter(material_code=material_code).first() if material_code else None
                related_order = safe_str(row.get('关联订单'))

                FactoryTransfer.objects.update_or_create(
                    transfer_no=transfer_no,
                    defaults={
                        'material': material,
                        'from_factory': from_factory,
                        'to_factory': to_factory,
                        'quantity': quantity,
                        'expected_arrival_date': arrive_date,
                        'status': status,
                        'reason': safe_str(row.get('调拨原因', '')),
                        'related_order': related_order if related_order else None
                    }
                )
                imported += 1
            except Exception as e:
                errors.append(f'工厂调拨第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'工厂日历与调拨导入完成（日历+调拨）',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_config_rules_ecn_data(rows):
    """导入规则与工程变更数据（拆分后的独立表）"""
    imported = 0
    updated = 0
    errors = []

    rule_rows = []
    ecn_rows = []

    for row in rows:
        data_type = row.get('数据类型', '').strip()
        if data_type == '优先级规则':
            rule_rows.append(row)
        elif data_type == '工程变更':
            ecn_rows.append(row)

    # 处理优先级规则
    if rule_rows:
        from prediction.models import PriorityRule
        for i, row in enumerate(rule_rows, 1):
            try:
                rule_name = safe_str(row.get('规则名称'))
                if not rule_name:
                    continue
                strategy_type = safe_str(row.get('策略类型', 'delivery_first'))

                status_raw = safe_str(row.get('状态', '启用'))
                is_active = status_raw in ['启用', 'active', '1', 'True', 'true']

                PriorityRule.objects.update_or_create(
                    name=rule_name,
                    defaults={
                        'strategy': strategy_type,
                        'urgency_weight': float(safe_str(row.get('紧急度权重', '0.25'))),
                        'delivery_weight': float(safe_str(row.get('交期权重', '0.25'))),
                        'customer_weight': float(safe_str(row.get('客户等级权重', '0.20'))),
                        'value_weight': float(safe_str(row.get('订单价值权重', '0.15'))),
                        'product_weight': float(safe_str(row.get('产品组权重', '0.10'))),
                        'is_active': is_active
                    }
                )
                imported += 1
            except Exception as e:
                errors.append(f'优先级规则第{i}行：{str(e)}')

    # 处理工程变更(ECN)
    if ecn_rows:
        from prediction.models import EngineeringChange
        material_cache_ecn = {m.material_code: m for m in Material.objects.all()}
        for i, row in enumerate(ecn_rows, 1):
            try:
                ecn_no = safe_str(row.get('ECN编号') or row.get('调拨编号'))  # 兼容新旧列名
                if not ecn_no:
                    continue
                material_code = safe_str(row.get('物料ID'))
                material = material_cache_ecn.get(material_code) if material_code else None

                eff_date_str = safe_str(row.get('生效日期', ''))
                effective_date = None
                if eff_date_str:
                    for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            effective_date = datetime.strptime(eff_date_str.strip(), fmt).date()
                            break
                        except ValueError:
                            continue

                exp_date_str = safe_str(row.get('失效日期', ''))
                expiry_date = None
                if exp_date_str:
                    for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            expiry_date = datetime.strptime(exp_date_str.strip(), fmt).date()
                            break
                        except ValueError:
                            continue

                status_raw = safe_str(row.get('状态', '启用'))
                is_active = status_raw in ['启用', 'active', '1', 'True', 'true']

                EngineeringChange.objects.update_or_create(
                    ecn_no=ecn_no,
                    defaults={
                        'material': material,
                        'change_type': safe_str(row.get('变更类型') or row.get('策略类型', '材料替换')),
                        'reason': safe_str(row.get('变更原因') or row.get('调拨原因', '')),
                        'related_product': safe_str(row.get('关联产品') or row.get('关联订单', '')),
                        'ecn_category': safe_str(row.get('ECN类别') or row.get('规则名称', '')),
                        'effective_date': effective_date,
                        'expiry_date': expiry_date,
                        'status': 'active' if is_active else 'inactive',
                        'remarks': safe_str(row.get('备注', '')),
                    }
                )
                imported += 1
            except Exception as e:
                errors.append(f'工程变更第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'规则与工程变更导入完成（优先级规则+ECN）',
        'imported': imported,
        'updated': updated,
        'errors': errors
    }


def import_factory_calendar_data(rows):
    """导入工厂日历数据（独立表）"""
    imported = 0
    errors = []
    from prediction.models import FactoryCalendar

    for i, row in enumerate(rows, 1):
        try:
            date_str = safe_str(row.get('日期'))
            if not date_str:
                continue
            is_workday = safe_str(row.get('是否工作日', '是')).strip() in ['是', '1', 'True', 'true']
            shift_type = safe_str(row.get('班次类型', '正常'))
            shift_count = 1 if shift_type == '正常' else (2 if shift_type == '加班' else 0)
            remark = safe_str(row.get('备注', ''))

            date_val = None
            normalized_cal = date_str.strip()
            if '/' in normalized_cal:
                parts = normalized_cal.split('/')
                if len(parts) == 3:
                    normalized_cal = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
            for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                try:
                    date_val = datetime.strptime(normalized_cal, fmt).date()
                    break
                except ValueError:
                    continue

            if date_val:
                fc = safe_str(row.get('工厂代码', '')).strip()
                factory_code = fc if fc else 'DEFAULT'
                FactoryCalendar.objects.update_or_create(
                    factory_code=factory_code, date=date_val,
                    defaults={'is_workday': is_workday, 'shift_count': shift_count, 'remarks': remark}
                )
                imported += 1
        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {'status': 'success' if not errors else 'partial', 'message': f'工厂日历导入完成，共{imported}条',
            'imported': imported, 'updated': 0, 'errors': errors}


def import_factory_transfer_data(rows):
    """导入工厂调拨数据（独立表）"""
    imported = 0
    errors = []
    from prediction.models import FactoryTransfer

    for i, row in enumerate(rows, 1):
        try:
            transfer_no = safe_str(row.get('调拨编号'))
            if not transfer_no:
                continue
            material_code = safe_str(row.get('物料ID'))
            material = Material.objects.filter(material_code=material_code).first() if material_code else None

            status_raw = safe_str(row.get('状态', '待调拨'))
            status = {'已完成': 'completed', '进行中': '在途', '待调拨': 'pending'}.get(status_raw, 'pending')

            arrive_date_str = safe_str(row.get('预计到达日期'))
            arrive_date = None
            if arrive_date_str:
                normalized_ad = arrive_date_str.strip()
                if '/' in normalized_ad:
                    parts = normalized_ad.split('/')
                    if len(parts) == 3:
                        normalized_ad = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try: arrive_date = datetime.strptime(normalized_ad, fmt).date(); break
                    except ValueError: continue

            FactoryTransfer.objects.update_or_create(
                transfer_no=transfer_no,
                defaults={
                    'material': material, 'from_factory': safe_str(row.get('源工厂')),
                    'to_factory': safe_str(row.get('目标工厂')),
                    'quantity': int(float(safe_str(row.get('调拨数量', '0')))),
                    'expected_arrival_date': arrive_date, 'status': status,
                    'reason': safe_str(row.get('调拨原因', '')),
                    'related_order': safe_str(row.get('关联订单')) or None,
                }
            )
            imported += 1
        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {'status': 'success' if not errors else 'partial', 'message': f'工厂调拨导入完成，共{imported}条',
            'imported': imported, 'updated': 0, 'errors': errors}


def import_priority_rule_data(rows):
    """导入优先级规则数据（独立表）"""
    imported = 0
    errors = []
    from prediction.models import PriorityRule

    for i, row in enumerate(rows, 1):
        try:
            rule_name = safe_str(row.get('规则名称'))
            if not rule_name:
                continue
            status_raw = safe_str(row.get('状态', '启用'))
            is_active = status_raw in ['启用', 'active', '1', 'True', 'true']

            PriorityRule.objects.update_or_create(
                name=rule_name,
                defaults={
                    'strategy': safe_str(row.get('策略类型', 'delivery_first')),
                    'urgency_weight': float(safe_str(row.get('紧急度权重', '0.25'))),
                    'delivery_weight': float(safe_str(row.get('交期权重', '0.25'))),
                    'customer_weight': float(safe_str(row.get('客户等级权重', '0.20'))),
                    'value_weight': float(safe_str(row.get('订单价值权重', '0.15'))),
                    'product_weight': float(safe_str(row.get('产品组权重', '0.10'))),
                    'inventory_status_weight': float(safe_str(row.get('库存状态权重', '0'))),
                    'capacity_utilization_weight': float(safe_str(row.get('产能利用率权重', '0'))),
                    'is_active': is_active,
                }
            )
            imported += 1
        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {'status': 'success' if not errors else 'partial', 'message': f'优先级规则导入完成，共{imported}条',
            'imported': imported, 'updated': 0, 'errors': errors}


def import_engineering_change_data(rows):
    """导入工程变更ECN数据（独立表，列名已标准化为ECN编号/变更类型/变更原因等）"""
    imported = 0
    errors = []
    from prediction.models import EngineeringChange
    material_cache = {m.material_code: m for m in Material.objects.all()}

    for i, row in enumerate(rows, 1):
        try:
            ecn_no = safe_str(row.get('ECN编号'))
            if not ecn_no:
                continue
            material_code = safe_str(row.get('物料ID'))
            material = material_cache.get(material_code) if material_code else None

            def _parse_dt(ds):
                if not ds or not str(ds).strip(): return None
                s = str(ds).strip().split(' ')[0]
                if '/' in s:
                    p = s.split('/')
                    if len(p) == 3: s = f'{p[0]}/{int(p[1]):02d}/{int(p[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try: return datetime.strptime(s, fmt).date()
                    except ValueError: continue
                return None

            status_raw = safe_str(row.get('状态', '启用'))
            is_active = status_raw in ['启用', 'active', '1', 'True', 'true']

            EngineeringChange.objects.update_or_create(
                ecn_no=ecn_no,
                defaults={
                    'material': material,
                    'change_type': safe_str(row.get('变更类型', '材料替换')),
                    'reason': safe_str(row.get('变更原因', '')),
                    'related_product': safe_str(row.get('关联产品', '')),
                    'ecn_category': safe_str(row.get('ECN类别', '')),
                    'effective_date': _parse_dt(row.get('生效日期')),
                    'expiry_date': _parse_dt(row.get('失效日期')),
                    'status': 'active' if is_active else 'inactive',
                    'remarks': safe_str(row.get('备注', '')),
                }
            )
            imported += 1
        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {'status': 'success' if not errors else 'partial', 'message': f'工程变更导入完成，共{imported}条',
            'imported': imported, 'updated': 0, 'errors': errors}


def import_delivery_change_data(rows):
    """导入交期变更记录数据"""
    imported = 0
    errors = []

    # 变更类型中文→英文映射
    change_type_map = {
        '供应商延期': 'supplier_delay',
        '供应商提前': 'supplier_advance',
        '客户加急': 'customer_rush',
        '客户延后': 'customer_postpone',
        '物流问题': 'logistics_issue',
        '物料短缺': 'material_shortage',
        # 英文兼容
        'supplier_delay': 'supplier_delay',
        'supplier_advance': 'supplier_advance',
        'customer_rush': 'customer_rush',
        'customer_postpone': 'customer_postpone',
        'logistics_issue': 'logistics_issue',
        'material_shortage': 'material_shortage',
    }

    def _parse_dt(ds):
        if not ds or not str(ds).strip():
            return None
        s = str(ds).strip().split(' ')[0]
        if '/' in s:
            p = s.split('/')
            if len(p) == 3:
                s = f'{p[0]}/{int(p[1]):02d}/{int(p[2]):02d}'
        for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    for i, row in enumerate(rows, 1):
        try:
            order_no = safe_str(row.get('订单号') or row.get('关联订单号') or row.get('order_no'))
            if not order_no:
                continue

            original_date = _parse_dt(row.get('原交付日期') or row.get('原定交付日期') or row.get('original_date'))
            new_date = _parse_dt(row.get('新交付日期') or row.get('新定交付日期') or row.get('new_date'))

            if not original_date or not new_date:
                errors.append(f'第{i}行：原交付日期和新交付日期不能为空')
                continue

            # 计算变更天数
            change_days_raw = str(row.get('变更天数', '') or '').strip()
            try:
                change_days = int(float(change_days_raw))
            except (ValueError, TypeError):
                change_days = (new_date - original_date).days

            change_type_cn = safe_str(row.get('变更类型') or row.get('change_type'))
            change_type = change_type_map.get(change_type_cn, 'supplier_delay')

            DeliveryChange.objects.create(
                order_no=order_no,
                po_no=safe_str(row.get('采购单号') or row.get('po_no')) or None,
                material_code=safe_str(row.get('物料ID') or row.get('物料代码') or row.get('material_code')) or None,
                supplier_code=safe_str(row.get('供应商代码') or row.get('supplier_code')) or None,
                change_type=change_type,
                original_date=original_date,
                new_date=new_date,
                change_days=change_days,
                reason=safe_str(row.get('变更原因') or row.get('reason')),
                change_by=safe_str(row.get('变更来源') or row.get('change_by') or 'system'),
            )
            imported += 1
        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    return {
        'status': 'success' if not errors else 'partial',
        'message': f'交期变更记录导入完成，共{imported}条',
        'imported': imported,
        'updated': 0,
        'errors': errors
    }


def import_config_data(rows):
    """导入系统配置数据（兼容旧版混合文件，内部按类型分流）"""
    result_cal = import_factory_calendar_transfer_data(rows)
    result_rules = import_config_rules_ecn_data(rows)

    all_errors = result_cal.get('errors', []) + result_rules.get('errors', [])
    total_imported = result_cal.get('imported', 0) + result_rules.get('imported', 0)
    total_updated = result_cal.get('updated', 0) + result_rules.get('updated', 0)

    return {
        'status': 'success' if not all_errors else 'partial',
        'message': f'系统配置导入完成（工厂日历/调拨/规则/工程变更）',
        'imported': total_imported,
        'updated': total_updated,
        'errors': all_errors
    }


# ==================== 高性能批量导入函数（优化版本） ====================

from django.db import transaction
import time


def import_data_high_performance(rows, import_type, clean_import=False):
    """高性能批量导入函数 - 使用预加载缓存和批量操作

    优化点：
    1. 预加载关联数据到内存字典，避免重复查询
    2. 使用 bulk_create/bulk_update 批量操作
    3. 使用 atomic() 减少数据库事务提交次数
    4. 并行处理无依赖的数据

    性能提升：预计 5-10 倍速度提升
    """
    start_time = time.time()

    # 根据类型调用对应的高性能函数
    if import_type == 'material':
        result = _batch_import_material(rows, clean_import)
    elif import_type in ('supplier', '供应商承诺'):
        result = _batch_import_supplier(rows, clean_import)
    elif import_type == 'customer':
        result = _batch_import_customer(rows, clean_import)
    elif import_type == 'inventory':
        result = _batch_import_inventory(rows, clean_import)
    elif import_type == 'order':
        result = _batch_import_order(rows, clean_import)
    elif import_type == 'purchase':
        result = _batch_import_purchase(rows, clean_import)
    elif import_type == 'bom':
        result = _batch_import_bom(rows, clean_import)
    elif import_type == 'workcenter':
        result = _batch_import_workcenter(rows, clean_import)
    elif import_type == 'config':
        result = _batch_import_config(rows, clean_import)
    elif import_type == 'factory_calendar':
        result = _batch_import_factory_calendar(rows, clean_import)
    elif import_type == 'factory_transfer':
        result = _batch_import_factory_transfer(rows, clean_import)
    elif import_type == 'priority_rule':
        result = _batch_import_priority_rule(rows, clean_import)
    elif import_type == 'engineering_change':
        result = _batch_import_engineering_change(rows, clean_import)
    elif import_type == 'factory_calendar_transfer':
        result = _batch_import_factory_calendar_transfer(rows, clean_import)
    elif import_type == 'config_rules_ecn':
        result = _batch_import_config_rules_ecn(rows, clean_import)
    elif import_type == 'substitute':
        result = _batch_import_substitute(rows, clean_import)
    elif import_type == 'delivery_change':
        result = _batch_import_delivery_change(rows, clean_import)
    else:
        result = {
            'status': 'error',
            'message': f'不支持的导入类型: {import_type}',
            'imported': 0,
            'updated': 0,
            'errors': []
        }

    elapsed = time.time() - start_time
    logger.info(f'[高性能导入] 类型={import_type}, 耗时={elapsed:.2f}秒, 导入={result.get("imported", 0)}, 更新={result.get("updated", 0)}')

    # 添加性能信息
    result['performance'] = {
        'elapsed_seconds': round(elapsed, 2),
        'mode': 'high_performance'
    }

    return result


def _preload_suppliers():
    """预加载所有供应商到字典"""
    suppliers = Supplier.objects.all()
    return {s.supplier_code: s for s in suppliers}


def _preload_materials():
    """预加载所有物料到字典"""
    materials = Material.objects.all()
    return {m.material_code: m for m in materials}


def _preload_customers():
    """预加载所有客户到字典"""
    customers = Customer.objects.all()
    return {c.customer_code: c for c in customers}


def _batch_import_material(rows, clean_import):
    """高性能物料导入 - 真正的批量操作"""
    imported = updated = 0
    errors = []

    if clean_import:
        Material.objects.all().delete()

    # 一次性预加载所有现有物料
    existing_map = {m.material_code: m for m in Material.objects.all()}
    to_create = []
    to_update_objs = []  # (obj, defaults) 元组列表

    # 预加载供应商缓存，用于处理主/备供应商关联
    supplier_cache = _preload_suppliers()
    sm_to_create = []  # SupplierMaterial 待创建列表
    sm_existing_keys = set()  # 已存在的 (material_code, supplier_code) 集合
    for sm in SupplierMaterial.objects.select_related('material', 'supplier').all():
        sm_existing_keys.add((sm.material.material_code, sm.supplier.supplier_code))

    type_mapping = {
        '原材料': 'raw', '半成品': 'semi', '成品': 'finished',
        'raw': 'raw', 'semi': 'semi', 'finished': 'finished'
    }

    for i, row in enumerate(rows, 1):
        try:
            material_code = str(row.get('物料ID', row.get('material_code', ''))).strip()
            if not material_code:
                errors.append(f'第{i}行：物料代码不能为空')
                continue

            material_type_raw = str(row.get('物料类型', row.get('类型(原材料/半成品)', row.get('类型', 'raw')))).strip()
            if '(' in material_type_raw:
                material_type = material_type_raw.split('(')[0].strip()
            else:
                material_type = material_type_raw
            material_type = type_mapping.get(material_type.lower(), 'raw')

            cost_str = str(row.get('标准成本', row.get('单价(元)', '0'))).strip()
            standard_cost = round(float(cost_str.replace(',', '')), 2) if cost_str else 0

            safety_stock_str = str(row.get('安全库存', '')).strip()
            if safety_stock_str:
                safety_stock = int(float(safety_stock_str.replace(',', '')))
            else:
                min_order = int(str(row.get('最小起订量', '100')).strip() or '100')
                safety_stock = min_order * 2

            shelf_life_str = str(row.get('保质期(天)', '0')).strip()
            shelf_life = int(shelf_life_str.replace(',', '').replace('天', '')) if shelf_life_str else 0

            defaults = {
                'material_name': str(row.get('物料名称', '')).strip(),
                'material_type': material_type,
                'unit': str(row.get('单位', '件')).strip(),
                'shelf_life': shelf_life,
                'min_order_qty': int(str(row.get('最小起订量', '1')).strip() or '1'),
                'lead_time': int(str(row.get('采购提前期(天)', '7')).strip() or '7'),
                'standard_cost': standard_cost,
                'sales_price': round(float(str(row.get('销售价格', '0')).replace(',', '') or '0'), 2),
                'safety_stock': safety_stock,
                'min_production_qty': int(str(row.get('最小生产批量', '1')).strip() or '1'),
                'is_active': True
            }

            if material_code in existing_map:
                obj = existing_map[material_code]
                for k, v in defaults.items():
                    setattr(obj, k, v)
                to_update_objs.append(obj)
                updated += 1
            else:
                to_create.append(Material(material_code=material_code, **defaults))
                imported += 1

            # 处理主供应商关联（01_物料.csv 中的扩展字段）
            main_sup_code = str(row.get('主供应商', '') or row.get('main_supplier', '')).strip()
            if main_sup_code:
                supplier = supplier_cache.get(main_sup_code)
                if not supplier:
                    # 供应商必须在供应商主数据中预先定义，不自动创建空壳记录
                    continue

                sm_key = (material_code, main_sup_code)
                if sm_key not in sm_existing_keys:
                    sm_to_create.append({
                        'material_code': material_code,
                        'supplier_code': main_sup_code,
                        'unit_price': standard_cost,
                        'min_order_qty': defaults.get('min_order_qty', 1),
                        'lead_time': defaults.get('lead_time', 7),
                        'is_forbidden': False
                    })
                    sm_existing_keys.add(sm_key)

            # 处理备用供应商关联
            backup_sup_code = str(row.get('备用供应商', '') or row.get('backup_supplier', '')).strip()
            if backup_sup_code:
                if backup_sup_code not in supplier_cache:
                    continue
                sm_key = (material_code, backup_sup_code)
                if sm_key not in sm_existing_keys:
                    sm_to_create.append({
                        'material_code': material_code,
                        'supplier_code': backup_sup_code,
                        'unit_price': standard_cost,
                        'min_order_qty': defaults.get('min_order_qty', 1),
                        'lead_time': defaults.get('lead_time', 7),
                        'is_forbidden': False
                    })
                    sm_existing_keys.add(sm_key)

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    # 真正批量操作：各只需1-2次DB操作（带重试，防止SQLite并发锁）
    def _do_bulk_write():
        with transaction.atomic():
            if to_create:
                Material.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
            if to_update_objs:
                Material.objects.bulk_update(
                    to_update_objs,
                    ['material_name', 'material_type', 'unit', 'shelf_life',
                     'min_order_qty', 'lead_time', 'standard_cost', 'sales_price',
                     'safety_stock', 'min_production_qty', 'is_active'],
                    batch_size=500
                )

            # 批量创建 SupplierMaterial 关联
            if sm_to_create:
                material_objs = {m.material_code: m for m in Material.objects.filter(
                    material_code__in=list(set(sm['material_code'] for sm in sm_to_create))
                )}
                supplier_objs = {s.supplier_code: s for s in Supplier.objects.filter(
                    supplier_code__in=list(set(sm['supplier_code'] for sm in sm_to_create))
                )}
                sm_instances = []
                for sm in sm_to_create:
                    mat = material_objs.get(sm['material_code'])
                    sup = supplier_objs.get(sm['supplier_code'])
                    if mat and sup:
                        sm_instances.append(SupplierMaterial(
                            material=mat, supplier=sup,
                            unit_price=sm['unit_price'],
                            min_order_qty=sm['min_order_qty'],
                            lead_time=sm['lead_time'],
                            is_forbidden=sm['is_forbidden']
                        ))
                if sm_instances:
                    SupplierMaterial.objects.bulk_create(sm_instances, batch_size=500, ignore_conflicts=True)

    _retry_db_operation(_do_bulk_write)

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'物料导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_supplier(rows, clean_import):
    """高性能供应商导入 - 真正的批量操作"""
    imported = updated = 0
    errors = []

    if clean_import:
        Supplier.objects.all().delete()

    existing_map = {s.supplier_code: s for s in Supplier.objects.all()}
    to_create = []
    to_update_objs = []

    for i, row in enumerate(rows, 1):
        try:
            # 如果有数据类型列，只处理供应商信息行（跳过承诺行）
            data_type = str(row.get('数据类型', '')).strip()
            if data_type and data_type != '供应商信息':
                continue

            supplier_code = str(row.get('供应商ID', '') or row.get('供应商代码', '')
                           or row.get('supplier_code', '')).strip()
            if not supplier_code:
                errors.append(f'第{i}行：供应商代码不能为空')
                continue

            supplier_name = str(row.get('供应商名称', '') or row.get('名称', '')
                            or row.get('supplier_name', supplier_code)).strip()

            rating_raw = safe_str(row.get('评级', row.get('供应商评级', 'B级')))
            rating = SUPPLIER_RATING_MAPPING.get(rating_raw, 'B')

            delivery_reliability = float(str(row.get('交付可靠率', '0.9')).strip() or 0.9)
            normal_lead_time = int(str(row.get('正常交期(天)', '7')).strip() or '7')

            defaults = {
                'supplier_name': supplier_name,
                'contact_person': str(row.get('联系人', '')).strip(),
                'phone': str(row.get('联系电话', '')).strip(),
                'email': str(row.get('邮箱', '')).strip(),
                'address': str(row.get('地址', '')).strip(),
                'rating': rating,
                'delivery_reliability': delivery_reliability,
                'normal_lead_time': normal_lead_time,
                'payment_terms': str(row.get('结算方式', '月结30天')).strip() or '月结30天',
                'min_order_qty': int(str(row.get('最小起订量(件)', 100)).strip() or 100),
                'capacity_level': str(row.get('产能等级', 'B')).strip() or 'B',
                'cooperation_years': int(str(row.get('合作年限(年)', 3)).strip() or 3),
                'warranty_months': int(str(row.get('质保期(月)', 12)).strip() or 12),
                'on_time_delivery_rate': float(str(row.get('准时交付率', 0.95)).strip() or 0.95),
                'is_active': True
            }

            if supplier_code in existing_map:
                obj = existing_map[supplier_code]
                for k, v in defaults.items():
                    setattr(obj, k, v)
                to_update_objs.append(obj)
                updated += 1
            else:
                to_create.append(Supplier(supplier_code=supplier_code, **defaults))
                imported += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    with transaction.atomic():
        if to_create:
            Supplier.objects.bulk_create(to_create, batch_size=200, ignore_conflicts=True)
        if to_update_objs:
            Supplier.objects.bulk_update(
                to_update_objs,
                ['supplier_name', 'contact_person', 'phone', 'email',
                 'address', 'rating', 'delivery_reliability',
                 'normal_lead_time', 'payment_terms', 'min_order_qty',
                 'capacity_level', 'cooperation_years', 'warranty_months',
                 'on_time_delivery_rate', 'is_active'],
                batch_size=200
            )

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'供应商导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_customer(rows, clean_import):
    """高性能客户导入 - 真正的批量操作"""
    imported = updated = 0
    errors = []

    if clean_import:
        Customer.objects.all().delete()

    existing_map = {c.customer_code: c for c in Customer.objects.all()}
    to_create = []
    to_update_objs = []

    for i, row in enumerate(rows, 1):
        try:
            customer_code = str(row.get('客户代码', '') or row.get('客户ID', '')
                            or row.get('customer_code', '')).strip()
            if not customer_code:
                errors.append(f'第{i}行：客户代码不能为空')
                continue

            customer_name = str(row.get('客户名称', '') or row.get('名称', '')
                            or row.get('customer_name', '')).strip()
            if not customer_name:
                errors.append(f'第{i}行：客户名称不能为空')
                continue

            credit_limit = round(float(str(row.get('信用额度', '100000')).replace(',', '')), 2)

            is_active_val = str(row.get('是否启用', row.get('is_active', '1'))).lower()
            is_active = is_active_val not in ['0', 'false', '否', '禁用']

            level_raw = str(row.get('客户等级', 'normal'))
            level_map = {'S级': 'vip', 'A级': 'important', 'B级': 'normal', 'C级': 'normal',
                        'S': 'vip', 'A': 'important', 'B': 'normal', 'C': 'normal'}
            customer_level = level_map.get(level_raw, level_raw.lower())

            # 从客户名称推断缺失的客户类型/付款条件（CSV中C001-C050等行这些字段可能为空）
            _ctype_raw = str(row.get('客户类型', '')).strip()
            _pterms_raw = str(row.get('付款条件', '')).strip()
            if not _ctype_raw or not _pterms_raw or not level_raw or level_raw.strip() == '':
                _name_lower = customer_name.lower()
                if not _ctype_raw:
                    if '海外' in customer_name or any(k in _name_lower for k in ['ebay', 'amazon', 'lazada', 'shopee']):
                        _ctype_raw = '海外渠道'
                    elif '工程' in customer_name:
                        _ctype_raw = '工程渠道'
                    elif '企业集采' in customer_name or 'corp' in _name_lower:
                        _ctype_raw = '企业集采'
                    elif any(k in customer_name for k in ['电商', '抖音', '京东', '天猫', '拼多多', '唯品会', '苏宁', '快手']):
                        _ctype_raw = '电商平台'
                    elif any(k in customer_name for k in ['线下零售', '沃尔玛', '家乐福', '大润发', '物美', '华润', '联华', '永辉', '国美']):
                        _ctype_raw = '线下零售'
                    elif any(k in customer_name for k in ['运营商', '电信', '移动', '联通', '广电']):
                        _ctype_raw = '运营商'
                    else:
                        _ctype_raw = '其他'
                if not _pterms_raw:
                    if '运营商' in customer_name:
                        _pterms_raw = '月结90天'
                    elif '企业集采' in customer_name or '工程' in customer_name:
                        _pterms_raw = '月结45天'
                    elif '线下零售' in customer_name:
                        _pterms_raw = '月结30天'
                    else:
                        _pterms_raw = '月结30天'
                if not level_raw or level_raw.strip() == '':
                    if any(k in customer_name for k in ['中国移动', '中国电信', '华为', '小米', '比亚迪', '格力', '联想', 'OPPO', 'vivo']):
                        customer_level = 'vip'
                    elif any(k in customer_name for k in ['中兴', '万科', '碧桂园', '龙湖', '绿地']):
                        customer_level = 'important'
                    else:
                        customer_level = 'normal'

            # 清洗联系人名称：去除数字后缀（如"采购1"/"周经理90" → "采购"/"周经理"）
            import re as _re
            _contact_raw = str(row.get('联系人', '')).strip()
            _contact_cleaned = _re.sub(r'\d+$', '', _contact_raw) if _contact_raw else ''
            if not _contact_cleaned or len(_contact_cleaned) < 2:
                # 如果清洗后太短或为空，从客户名称推断
                if '海外' in customer_name:
                    _contact_cleaned = '张经理'
                elif '工程' in customer_name:
                    _contact_cleaned = '李经理'
                elif '企业集采' in customer_name:
                    _contact_cleaned = '王经理'
                elif any(k in customer_name for k in ['电商', '抖音', '京东', '快手']):
                    _contact_cleaned = '陈经理'
                elif any(k in customer_name for k in ['线下零售', '沃尔玛', '家乐福']):
                    _contact_cleaned = '刘经理'
                elif any(k in customer_name for k in ['运营商', '电信', '移动', '联通']):
                    _contact_cleaned = '赵总'
                else:
                    _contact_cleaned = '周经理'

            defaults = {
                'customer_name': customer_name,
                'contact_person': _contact_cleaned,
                'phone': str(row.get('联系电话', '')).strip(),
                'email': str(row.get('邮箱', '')).strip(),
                'address': str(row.get('地址', '')).strip(),
                'credit_limit': credit_limit,
                'payment_terms': _pterms_raw or '月结30天',
                'customer_type': _ctype_raw or '其他',
                'customer_level': customer_level,
                'delivery_priority': int(float(str(row.get('交付优先级', '5')).strip() or '5')),
                'is_active': is_active
            }

            if customer_code in existing_map:
                obj = existing_map[customer_code]
                for k, v in defaults.items():
                    setattr(obj, k, v)
                to_update_objs.append(obj)
                updated += 1
            else:
                to_create.append(Customer(customer_code=customer_code, **defaults))
                imported += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    def _do_db_write_2():
        with transaction.atomic():
            if to_create:
                Customer.objects.bulk_create(to_create, batch_size=200, ignore_conflicts=True)
            if to_update_objs:
                Customer.objects.bulk_update(
                    to_update_objs,
                    ['customer_name', 'contact_person', 'phone', 'email',
                     'address', 'credit_limit', 'payment_terms',
                     'customer_type', 'customer_level', 'delivery_priority', 'is_active'],
                    batch_size=200
                )

    _retry_db_operation(_do_db_write_2)

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'客户导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_inventory(rows, clean_import):
    """高性能库存导入 - 真正的批量操作

    在途库存处理策略：
      - 本地类型：按(material, warehouse)正常创建/更新
      - 在途/运输中类型：不单独建记录，而是将该物料的所有在途数量
        合并累加到该物料「本地库存量最小且不为0」的仓库记录上
    """
    imported = updated = 0
    errors = []

    if clean_import:
        Inventory.objects.all().delete()

    # 预加载物料和现有库存
    material_cache = _preload_materials()
    existing_inv = {}  # (material_id, warehouse) -> Inventory obj（按物料+仓库去重）
    for inv in Inventory.objects.select_related('material').all():
        key = (inv.material_id, (inv.warehouse or '主仓库'))
        existing_inv[key] = inv

    to_create = []
    to_update_objs = []

    # ---- 两阶段收集：先分离本地行和在途行 ----
    local_rows = []   # 正常处理的本地行
    transit_by_material = {}  # material_id -> [累计quantity, 累计hold_qty]

    def parse_date_field(date_str):
        if not date_str or not str(date_str).strip():
            return None
        date_str = str(date_str).strip().split(' ')[0]
        if '/' in date_str:
            parts = date_str.split('/')
            if len(parts) == 3:
                date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
        for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return None

    for i, row in enumerate(rows, 1):
        try:
            material_code = str(row.get('物料ID', '') or row.get('物料代码', '')
                            or row.get('material_code', '')).strip()
            if not material_code:
                errors.append(f'第{i}行：物料代码不能为空')
                continue

            material = material_cache.get(material_code)
            if not material:
                errors.append(f'第{i}行：物料 {material_code} 不存在于物料主数据中，已跳过此行')
                continue

            quantity = int(float(str(row.get('在库数量', '0')))) if row.get('在库数量') else 0
            hold_quantity = int(float(str(row.get('Hold数量', '0')))) if row.get('Hold数量') else 0

            if quantity < 0:
                errors.append(f'第{i}行：在库数量不能为负数（{quantity}）')
                continue
            if hold_quantity < 0:
                errors.append(f'第{i}行：Hold数量不能为负数（{hold_quantity}）')
                continue

            warehouse = str(row.get('仓库', row.get('库位', ''))).strip() or '主仓库'

            batch_no = row.get('批次号', '') or ''
            expiry_date = parse_date_field(row.get('保质期到期日', '') or row.get('有效期至', ''))

            # 解析库存类型
            inv_type_raw = str(row.get('库存类型', row.get('类型', '本地'))).strip()
            inv_type_map = {
                '本地库存': '本地', '在途库存': '在途', '供应商承诺': '供应商承诺',
                '成品库存': 'finished', '半成品库存': 'semi',
                '本地': '本地', '在途': '在途', '供应商承诺': '供应商承诺',
                'finished': 'finished', 'semi': 'semi',
                'transit': '在途', '运输中': '在途'
            }
            inventory_type = inv_type_map.get(inv_type_raw, '本地')

            # ===== 在途类型：收集待合并，不直接创建记录 =====
            if inventory_type == '在途':
                if material.id not in transit_by_material:
                    transit_by_material[material.id] = [0, 0]
                transit_by_material[material.id][0] += quantity
                transit_by_material[material.id][1] += hold_quantity
                continue

            # ===== 本地类型：正常处理 =====
            is_hold_raw = str(row.get('是否冻结', row.get('状态', '否'))).strip()
            is_hold = is_hold_raw in ['是', '冻结', 'hold', 'True', '1'] or hold_quantity > 0
            hold_reason = str(row.get('Hold原因', '') or row.get('冻结原因', '')).strip()
            hold_until = parse_date_field(row.get('预计解Hold日期', '') or row.get('预计解Hold日期', ''))
            data_date = parse_date_field(row.get('数据日期', '') or row.get('data_date', ''))

            local_rows.append({
                'row_idx': i,
                'material': material,
                'warehouse': warehouse,
                'quantity': quantity,
                'hold_quantity': hold_quantity,
                'batch_no': batch_no,
                'expiry_date': expiry_date,
                'is_hold': is_hold,
                'hold_reason': hold_reason,
                'hold_until': hold_until,
                'data_date': data_date,
                'row': row,
            })

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    # ---- 阶段1：处理所有本地行 ----
    # 用于追踪每个物料各仓库的最终数量（供在途合并时找最小仓库用）
    mat_wh_final_qty = {}  # (material_id, warehouse) -> 最终quantity

    for lr in local_rows:
        try:
            material = lr['material']
            warehouse = lr['warehouse']
            key = (material.id, warehouse)

            ss_lower = str(lr['row'].get('安全库存下限', '0')).strip()
            tl_val = str(lr['row'].get('目标水位', '0')).strip()
            mu_val = str(lr['row'].get('库存上限', lr['row'].get('库存 上限', '0'))).strip()
            restr_raw = str(lr['row'].get('是否禁用', '否')).strip()

            defaults = {
                'material': material,
                'inventory_type': '本地',
                'quantity': lr['quantity'],
                'hold_quantity': lr['hold_quantity'],
                'available_quantity': max(0, lr['quantity'] - lr['hold_quantity']),
                'location': str(lr['row'].get('库位', '')).strip(),
                'batch_no': lr['batch_no'],
                'is_hold': lr['is_hold'],
                'hold_reason': lr['hold_reason'],
                'hold_until': lr['hold_until'],
                'data_date': lr['data_date'],
                'warehouse': warehouse,
                'safety_stock_lower': int(float(ss_lower)) if ss_lower else 0,
                'target_level': int(float(tl_val)) if tl_val else 0,
                'max_stock_upper': int(float(mu_val)) if mu_val else 0,
                'is_restricted': restr_raw in ('是', 'True', 'true', '1'),
                'restricted_reason': str(lr['row'].get('禁用原因', '')).strip(),
            }
            if lr['expiry_date']:
                defaults['expiry_date'] = lr['expiry_date']

            if key in existing_inv:
                obj = existing_inv[key]
                for k, v in defaults.items():
                    if k != 'material':
                        setattr(obj, k, v)
                to_update_objs.append(obj)
                updated += 1
                mat_wh_final_qty[key] = lr['quantity']
            else:
                to_create.append(Inventory(**defaults))
                imported += 1
                mat_wh_final_qty[key] = lr['quantity']

        except Exception as e:
            errors.append(f'第{lr["row_idx"]}行：{str(e)}')

    # ---- 阶段2：将在途数量合并到该物料库存量最小且不为0的仓库 ----
    if transit_by_material:
        for mat_id, (transit_qty, transit_hold) in transit_by_material.items():
            if transit_qty <= 0:
                continue

            # 找到该物料所有本地仓库及其当前数量
            wh_qtys = [(wh, qty) for (mid, wh), qty in mat_wh_final_qty.items() if mid == mat_id and qty > 0]
            if not wh_qtys:
                # 该物料没有任何本地仓库记录，跳过在途数据（无法归并）
                continue

            # 按数量升序排列，取最小的
            wh_qtys.sort(key=lambda x: x[1])
            target_wh = wh_qtys[0][0]
            target_key = (mat_id, target_wh)

            # 将在途量叠加到目标仓库
            if target_key in existing_inv:
                obj = existing_inv[target_key]
                obj.quantity += transit_qty
                obj.hold_quantity += transit_hold
                obj.available_quantity = max(0, obj.quantity - obj.hold_quantity - (obj.locked_quantity or 0))
                if obj not in to_update_objs:
                    to_update_objs.append(obj)
                updated += 1
            else:
                # 目标仓库是新创建的（在to_create中），找到它并修改
                found = False
                for item in to_create:
                    if item.material_id == mat_id and (item.warehouse or '主仓库') == target_wh:
                        item.quantity += transit_qty
                        item.hold_quantity += transit_hold
                        item.available_quantity = max(0, item.quantity - item.hold_quantity)
                        found = True
                        break
                if not found:
                    # 不应该发生，但做防御
                    pass

            mat_wh_final_qty[target_key] = mat_wh_final_qty.get(target_key, 0) + transit_qty

    with transaction.atomic():
        if to_create:
            Inventory.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
        if to_update_objs:
            Inventory.objects.bulk_update(
                to_update_objs,
                ['quantity', 'hold_quantity', 'available_quantity', 'location', 'batch_no',
                 'inventory_type', 'is_hold', 'hold_reason', 'hold_until', 'data_date',
                 'warehouse', 'expiry_date', 'safety_stock_lower', 'target_level',
                 'max_stock_upper', 'is_restricted', 'restricted_reason'],
                batch_size=500
            )

    transit_count = len(transit_by_material)
    msg = f'库存导入完成，新增{imported}条，更新{updated}条'
    if transit_count > 0:
        msg += f'（{transit_count}个物料的在途库存已合并到对应最少仓库）'

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': msg,
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_order(rows, clean_import):
    """高性能订单导入 - 真正的批量操作"""
    imported = updated = 0
    errors = []

    if clean_import:
        SalesOrder.objects.all().delete()

    # 预加载
    material_cache = _preload_materials()
    customer_cache = _preload_customers()
    existing_orders = {o.order_no: o for o in SalesOrder.objects.select_related('material').all()}

    to_create = []
    to_update_objs = []

    for i, row in enumerate(rows, 1):
        try:
            order_no = str(row.get('订单ID', '') or row.get('订单号', '') or row.get('销售订单号', '')
                       or row.get('order_no', '')).strip()
            if not order_no:
                errors.append(f'第{i}行：订单号不能为空')
                continue

            material_code = str(row.get('成品ID', '') or row.get('产品ID', '')
                            or row.get('material_code', '')).strip()
            if not material_code:
                errors.append(f'第{i}行：成品ID不能为空')
                continue

            # 获取或创建物料（纯内存操作，不查DB）
            material = material_cache.get(material_code)
            if not material:
                # 成品物料必须在物料主数据中预先定义，不自动创建空壳记录
                errors.append(f'第{i}行：成品 {material_code} 不存在于物料主数据中，已跳过此行')
                continue

            customer_code = str(row.get('客户名称', '') or row.get('客户代码', '')
                            or row.get('customer_code', '')).strip()

            quantity = int(float(str(row.get('订单数量', '') or row.get('数量', '')
                       or row.get('order_quantity', '0'))))
            unit_price = round(float(str(row.get('单价', '') or row.get('unit_price', '0'))), 2)

            # 防护：数量和单价不能为负数
            if quantity < 0:
                errors.append(f'第{i}行：订单数量不能为负数（{quantity}）')
                continue
            if unit_price < 0:
                errors.append(f'第{i}行：单价不能为负数（{unit_price}）')
                continue
            total_amount = round(float(str(row.get('总金额', '') or row.get('total_amount', '0'))), 2)
            # 如果总金额未提供或为0，自动计算
            if not total_amount and unit_price:
                total_amount = round(quantity * unit_price, 2)

            demand_date = datetime.now().date() + timedelta(days=7)
            # 【修复】CSV实际列名为'需求交付日 期'(中间含空格)，需精确匹配
            date_str = str(row.get('需求交付日 期', '') or row.get('需求交付日期', '') or row.get('交期', '') or row.get('demand_date', '')).strip()
            if date_str:
                # 标准化单数字月份/日期
                normalized = date_str
                if '/' in normalized:
                    parts = normalized.split('/')
                    if len(parts) == 3:
                        normalized = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        demand_date = datetime.strptime(normalized.split(' ')[0], fmt).date()
                        break
                    except ValueError:
                        continue

            priority_raw = str(row.get('优先级', '1'))
            priority_int_map = {'高': 3, '紧急': 4, '中': 2, '低': 1}
            try:
                priority = int(priority_raw)
            except ValueError:
                priority = priority_int_map.get(priority_raw, 1)

            # 订单日期
            order_date = None
            order_date_str = str(row.get('订单日期', '') or row.get('下单日期', '')).strip()
            if order_date_str:
                normalized_od = order_date_str
                if '/' in normalized_od:
                    parts = normalized_od.split('/')
                    if len(parts) == 3:
                        normalized_od = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        order_date = datetime.strptime(normalized_od.split(' ')[0], fmt).date()
                        break
                    except ValueError:
                        continue

            # 实际交付日期（新增字段）
            actual_delivery_date = None
            add_str = str(row.get('实际交付日期', '') or row.get('actual_delivery_date', '')).strip()
            if add_str:
                norm_ad = add_str
                if '/' in norm_ad:
                    p = norm_ad.split('/')
                    if len(p) == 3: norm_ad = f'{p[0]}/{int(p[1]):02d}/{int(p[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        actual_delivery_date = datetime.strptime(norm_ad.split(' ')[0], fmt).date()
                        break
                    except ValueError:
                        continue

            # 交付优先级（新增字段）
            delivery_priority_val = 5
            dp_raw = str(row.get('交付优先级', '') or row.get('delivery_priority', '5')).strip()
            try: delivery_priority_val = int(float(dp_raw))
            except (ValueError, TypeError): pass

            # 备注（新增字段）
            remarks_val = str(row.get('备注', '') or row.get('remarks', '')).strip()

            # 订单状态（必须与 ORDER_STATUS_CHOICES 一致）
            order_status_map = {
                # 中文输入
                '待确认': 'pending', '待处理': 'pending', '待排产': 'pending',
                '已确认': 'confirmed', '生产中': 'in_production', '进行中': 'processing',
                '已占料': 'allocated',
                '部分齐套': 'partial', '部分发货': 'partial', '部分完成': 'partial',
                '完全齐套': 'complete', '已完成': 'complete',
                '已发货': 'shipped', '已交付': 'delivered',
                '已取消': 'cancelled',
                # 英文输入（兼容）
                'pending': 'pending', 'confirmed': 'confirmed',
                'in_production': 'in_production', 'processing': 'processing',
                'allocated': 'allocated', 'partial': 'partial', 'complete': 'complete',
                'completed': 'complete', 'shipped': 'shipped', 'delivered': 'delivered',
                'cancelled': 'cancelled'
            }
            status_raw = str(row.get('状态', ''))
            order_status = order_status_map.get(status_raw.strip(), 'pending')

            # 物流方式
            shipping_map = {
                '空运': 'air', '海运': 'sea', '陆运': 'land',
                '快递': 'express', '铁路': 'rail', 'air': 'air', 'sea': 'sea'
            }
            shipping_raw = str(row.get('运输方式', ''))
            shipping_method = shipping_map.get(shipping_raw.strip(), shipping_raw.lower() or 'sea')

            if order_no in existing_orders:
                obj = existing_orders[order_no]
                obj.customer_name = customer_code or ''
                obj.material = material
                obj.quantity = quantity
                obj.unit_price = unit_price
                obj.total_amount = total_amount
                obj.order_date = order_date
                obj.demand_date = demand_date
                obj.status = order_status
                obj.priority = priority
                obj.shipping_method = shipping_method
                obj.actual_delivery_date = actual_delivery_date
                obj.delivery_priority = delivery_priority_val
                obj.remarks = remarks_val
                to_update_objs.append(obj)
                updated += 1
            else:
                to_create.append(SalesOrder(
                    order_no=order_no,
                    customer_name=customer_code or '',
                    material=material,
                    quantity=quantity,
                    unit_price=unit_price,
                    total_amount=total_amount,
                    order_date=order_date,
                    demand_date=demand_date,
                    status=order_status,
                    priority=priority,
                    shipping_method=shipping_method,
                    actual_delivery_date=actual_delivery_date,
                    delivery_priority=delivery_priority_val,
                    remarks=remarks_val,
                ))
                imported += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    def _do_db_write_4():
        with transaction.atomic():
            if to_create:
                SalesOrder.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
            if to_update_objs:
                SalesOrder.objects.bulk_update(
                    to_update_objs,
                    ['customer_name', 'material_id', 'quantity', 'unit_price',
                     'total_amount', 'order_date', 'demand_date', 'status',
                     'priority', 'shipping_method', 'actual_delivery_date',
                     'delivery_priority', 'remarks'],
                    batch_size=500
                )

    _retry_db_operation(_do_db_write_4)

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'订单导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_purchase(rows, clean_import):
    """高性能采购订单导入 - 真正的批量操作"""
    from ..models import PurchaseOrder
    imported = updated = 0
    errors = []

    if clean_import:
        PurchaseOrder.objects.all().delete()

    supplier_cache = _preload_suppliers()
    material_cache = _preload_materials()
    existing_pos = {p.po_no: p for p in PurchaseOrder.objects.select_related('supplier', 'material').all()}

    status_map = {
        # 中文输入
        '待确认': 'draft', '已下达': 'issued', '已确认': 'confirmed',
        '部分到货': 'partial', '已完成': 'completed', '已取消': 'cancelled',
        '待处理': 'pending', '生产中': 'in_production', '已发货': 'shipped',
        '部分发货': 'partial_shipped', '进行中': 'processing',
        # 英文输入（兼容）
        'draft': 'draft', 'issued': 'issued', 'confirmed': 'confirmed',
        'partial': 'partial', 'completed': 'completed', 'cancelled': 'cancelled',
        'pending': 'pending', 'in_production': 'in_production', 'shipped': 'shipped',
        'partial_shipped': 'partial_shipped', 'processing': 'processing'
    }

    to_create = []
    to_update_objs = []

    for i, row in enumerate(rows, 1):
        try:
            po_no = str(row.get('采购订单号', '') or row.get('po_no', '')).strip()
            if not po_no:
                errors.append(f'第{i}行：采购订单号不能为空')
                continue

            supplier_code = str(row.get('供应商代码', '') or row.get('supplier_code', '')).strip()
            if not supplier_code:
                for key in row:
                    if '供应商' in key and ('代码' in key or 'ID' in key) and '名称' not in key:
                        supplier_code = str(row[key] or '').strip()
                        break
            supplier = supplier_cache.get(supplier_code)
            if not supplier:
                errors.append(f'第{i}行：供应商 {supplier_code} 不存在')
                continue

            material_code = str(row.get('物料代码', '') or row.get('material_code', '')).strip()
            # 防御性查找：如果标准列名找不到，尝试模糊匹配
            if not material_code:
                for key in row:
                    if '物料' in key and '代码' in key and key not in ('物料名称',):
                        material_code = str(row[key] or '').strip()
                        break
            material = material_cache.get(material_code)
            if not material:
                errors.append(f'第{i}行：物料 {material_code} 不存在')
                continue

            quantity = int(float(str(row.get('订单数量', '') or row.get('数量', '')
                       or row.get('quantity', '0'))))
            unit_price = round(float(str(row.get('单价', '0') or row.get('unit_price', '0'))), 2)

            # 防护：数量和单价不能为负数
            if quantity < 0:
                errors.append(f'第{i}行：订单数量不能为负数（{quantity}）')
                continue
            if unit_price < 0:
                errors.append(f'第{i}行：单价不能为负数（{unit_price}）')
                continue
            total_raw = row.get('总金额')
            if total_raw is not None and str(total_raw).strip() not in ('', '0', '0.0'):
                total_amount = round(float(str(total_raw).replace(',', '')), 2)
            else:
                total_amount = round(quantity * unit_price, 2)

            # 交期天数：优先使用CSV中的"交期天数"，其次用供应商正常交期，默认7天
            delivery_days_raw = str(row.get('交期天数', '') or row.get('交期', '') or '').strip()
            delivery_days = supplier.normal_lead_time or 7
            if delivery_days_raw:
                try:
                    delivery_days = int(float(delivery_days_raw))
                except (ValueError, TypeError):
                    pass

            order_date = datetime.now().date()
            order_date_str = str(row.get('下单日期', '') or row.get('order_date', '')).strip()
            if order_date_str:
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                normalized_od = order_date_str.split(' ')[0]
                if '/' in normalized_od:
                    parts = normalized_od.split('/')
                    if len(parts) == 3:
                        normalized_od = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        order_date = datetime.strptime(normalized_od, fmt).date()
                        break
                    except ValueError:
                        continue

            delivery_date = order_date + timedelta(days=delivery_days)
            delivery_date_str = str(row.get('预计交付日期', '') or row.get('delivery_date', '')).strip()
            if delivery_date_str:
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                normalized_dd = delivery_date_str.split(' ')[0]
                if '/' in normalized_dd:
                    parts = normalized_dd.split('/')
                    if len(parts) == 3:
                        normalized_dd = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        delivery_date = datetime.strptime(normalized_dd, fmt).date()
                        break
                    except ValueError:
                        continue

            # 实际交付日期
            actual_delivery_date = None
            actual_delivery_str = str(row.get('实际交付日期', '') or row.get('actual_delivery_date', '')).strip()
            if actual_delivery_str:
                # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
                normalized_ad = actual_delivery_str.split(' ')[0]
                if '/' in normalized_ad:
                    parts = normalized_ad.split('/')
                    if len(parts) == 3:
                        normalized_ad = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                    try:
                        actual_delivery_date = datetime.strptime(normalized_ad, fmt).date()
                        break
                    except ValueError:
                        continue

            status_raw = str(row.get('状态', 'pending'))
            status = status_map.get(status_raw, status_raw.lower())

            if po_no in existing_pos:
                obj = existing_pos[po_no]
                obj.supplier = supplier
                obj.material = material
                obj.quantity = quantity
                obj.unit_price = unit_price
                obj.total_amount = total_amount
                obj.order_date = order_date
                obj.delivery_date = delivery_date
                obj.actual_delivery_date = actual_delivery_date
                obj.status = status
                to_update_objs.append(obj)
                updated += 1
            else:
                to_create.append(PurchaseOrder(
                    po_no=po_no, supplier=supplier, material=material,
                    quantity=quantity, unit_price=unit_price,
                    total_amount=total_amount, order_date=order_date,
                    delivery_date=delivery_date, actual_delivery_date=actual_delivery_date,
                    status=status
                ))
                imported += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    with transaction.atomic():
        if to_create:
            PurchaseOrder.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
        if to_update_objs:
            PurchaseOrder.objects.bulk_update(
                to_update_objs,
                ['supplier_id', 'material_id', 'quantity', 'unit_price',
                 'total_amount', 'order_date', 'delivery_date',
                 'actual_delivery_date', 'status'],
                batch_size=500
            )

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'采购订单导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_bom(rows, clean_import):
    """高性能BOM导入 - 真正的批量操作"""
    imported = updated = 0
    errors = []
    warnings = []

    if clean_import:
        BillOfMaterials.objects.all().delete()

    # 成品物料默认值映射（BOM父项引用时用于校验）
    MATERIAL_DEFAULTS = {
        'P0001': {'material_name': '智能音箱 标准版', 'standard_cost': 89.50, 'sales_price': 129.00, 'safety_stock': 100, 'shelf_life': 1095, 'lead_time': 7, 'min_production_qty': 100},
        'P0002': {'material_name': '智能音箱 Pro', 'standard_cost': 159.80, 'sales_price': 229.00, 'safety_stock': 80, 'shelf_life': 1095, 'lead_time': 8, 'min_production_qty': 80},
        'P0003': {'material_name': '智能音箱 Mini', 'standard_cost': 59.90, 'sales_price': 89.00, 'safety_stock': 150, 'shelf_life': 1095, 'lead_time': 5, 'min_production_qty': 150},
        # 智能网关系列
        'P0010': {'material_name': '智能网关 标准版', 'standard_cost': 198.00, 'sales_price': 499.00, 'safety_stock': 100, 'shelf_life': 1095, 'lead_time': 6, 'min_production_qty': 100},
        'P0011': {'material_name': '智能网关 Pro', 'standard_cost': 315.50, 'sales_price': 799.00, 'safety_stock': 80, 'shelf_life': 1095, 'lead_time': 7, 'min_production_qty': 80},
        'P0012': {'material_name': '智能网关 Mini', 'standard_cost': 142.80, 'sales_price': 369.00, 'safety_stock': 120, 'shelf_life': 1095, 'lead_time': 5, 'min_production_qty': 150},
        # 智能门锁系列
        'P0013': {'material_name': '智能门锁 标准版', 'standard_cost': 268.00, 'sales_price': 699.00, 'safety_stock': 80, 'shelf_life': 1460, 'lead_time': 7, 'min_production_qty': 80},
        'P0014': {'material_name': '智能门锁 Pro', 'standard_cost': 385.00, 'sales_price': 999.00, 'safety_stock': 50, 'shelf_life': 1460, 'lead_time': 8, 'min_production_qty': 50},
        'P0015': {'material_name': '智能门锁 青春版', 'standard_cost': 188.00, 'sales_price': 459.00, 'safety_stock': 120, 'shelf_life': 1460, 'lead_time': 6, 'min_production_qty': 120},
        # 传感器系列
        'P0016': {'material_name': '温湿度传感器', 'standard_cost': 32.50, 'sales_price': 89.00, 'safety_stock': 500, 'shelf_life': 2190, 'lead_time': 3, 'min_production_qty': 500},
        'P0017': {'material_name': '人体感应器 PIR', 'standard_cost': 45.80, 'sales_price': 119.00, 'safety_stock': 400, 'shelf_life': 2190, 'lead_time': 4, 'min_production_qty': 400},
        'P0018': {'material_name': '门窗传感器', 'standard_cost': 38.90, 'sales_price': 99.00, 'safety_stock': 500, 'shelf_life': 2190, 'lead_time': 3, 'min_production_qty': 500},
        'P0019': {'material_name': '烟雾报警器', 'standard_cost': 28.60, 'sales_price': 79.00, 'safety_stock': 600, 'shelf_life': 2555, 'lead_time': 3, 'min_production_qty': 600},
        # 安防传感器系列
        'P0020': {'material_name': '燃气报警器', 'standard_cost': 35.80, 'sales_price': 99.00, 'safety_stock': 500, 'shelf_life': 2190, 'lead_time': 4, 'min_production_qty': 500},
        'P0021': {'material_name': '水浸传感器', 'standard_cost': 22.50, 'sales_price': 59.00, 'safety_stock': 600, 'shelf_life': 2190, 'lead_time': 3, 'min_production_qty': 600},
        # 智能开关/家电控制系列
        'P0022': {'material_name': '无线开关', 'standard_cost': 42.30, 'sales_price': 109.00, 'safety_stock': 400, 'shelf_life': 1825, 'lead_time': 4, 'min_production_qty': 400},
        'P0023': {'material_name': '情景开关 4键', 'standard_cost': 58.90, 'sales_price': 149.00, 'safety_stock': 300, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 300},
        'P0024': {'material_name': '旋钮开关 调光', 'standard_cost': 48.60, 'sales_price': 129.00, 'safety_stock': 350, 'shelf_life': 1825, 'lead_time': 4, 'min_production_qty': 350},
        'P0025': {'material_name': '空调伴侣', 'standard_cost': 65.40, 'sales_price': 169.00, 'safety_stock': 300, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 300},
        # 智能窗帘系列
        'P0026': {'material_name': '窗帘电机', 'standard_cost': 88.50, 'sales_price': 229.00, 'safety_stock': 200, 'shelf_life': 1460, 'lead_time': 6, 'min_production_qty': 200},
        'P0027': {'material_name': '窗帘轨道 3m', 'standard_cost': 35.20, 'sales_price': 89.00, 'safety_stock': 400, 'shelf_life': 1825, 'lead_time': 4, 'min_production_qty': 400},
        # 智能摄像头系列
        'P0028': {'material_name': '智能摄像头 室内', 'standard_cost': 128.60, 'sales_price': 299.00, 'safety_stock': 150, 'shelf_life': 1095, 'lead_time': 7, 'min_production_qty': 150},
        'P0029': {'material_name': '智能摄像头 室外', 'standard_cost': 168.90, 'sales_price': 399.00, 'safety_stock': 100, 'shelf_life': 1095, 'lead_time': 8, 'min_production_qty': 100},
        # 智能门铃
        'P0030': {'material_name': '智能门铃', 'standard_cost': 98.50, 'sales_price': 249.00, 'safety_stock': 200, 'shelf_life': 1095, 'lead_time': 6, 'min_production_qty': 200},
        # 智能温控/环境电器系列
        'P0031': {'material_name': '智能温控器', 'standard_cost': 78.30, 'sales_price': 199.00, 'safety_stock': 250, 'shelf_life': 1460, 'lead_time': 5, 'min_production_qty': 250},
        'P0032': {'material_name': '智能空气净化器', 'standard_cost': 185.60, 'sales_price': 459.00, 'safety_stock': 100, 'shelf_life': 1095, 'lead_time': 8, 'min_production_qty': 100},
        'P0033': {'material_name': '智能除湿机', 'standard_cost': 225.80, 'sales_price': 549.00, 'safety_stock': 80, 'shelf_life': 1095, 'lead_time': 8, 'min_production_qty': 80},
        'P0034': {'material_name': '智能扫地机器人', 'standard_cost': 398.00, 'sales_price': 999.00, 'safety_stock': 60, 'shelf_life': 730, 'lead_time': 10, 'min_production_qty': 60},
        'P0035': {'material_name': '智能投影仪', 'standard_cost': 1258.00, 'sales_price': 2999.00, 'safety_stock': 30, 'shelf_life': 730, 'lead_time': 12, 'min_production_qty': 20},
        'P0036': {'material_name': '智能行车记录仪', 'standard_cost': 168.50, 'sales_price': 399.00, 'safety_stock': 150, 'shelf_life': 1095, 'lead_time': 6, 'min_production_qty': 150},
        # 智能穿戴系列
        'P0037': {'material_name': '智能手环', 'standard_cost': 68.90, 'sales_price': 179.00, 'safety_stock': 300, 'shelf_life': 730, 'lead_time': 5, 'min_production_qty': 300},
        'P0038': {'material_name': '智能手表', 'standard_cost': 198.50, 'sales_price': 499.00, 'safety_stock': 120, 'shelf_life': 730, 'lead_time': 7, 'min_production_qty': 100},
        'P0039': {'material_name': '智能耳机 TWS', 'standard_cost': 85.60, 'sales_price': 229.00, 'safety_stock': 200, 'shelf_life': 730, 'lead_time': 5, 'min_production_qty': 200},
        'P0040': {'material_name': '智能充电座', 'standard_cost': 28.90, 'sales_price': 79.00, 'safety_stock': 400, 'shelf_life': 1460, 'lead_time': 3, 'min_production_qty': 400},
        'P0041': {'material_name': '智能台灯', 'standard_cost': 52.30, 'sales_price': 139.00, 'safety_stock': 350, 'shelf_life': 1825, 'lead_time': 4, 'min_production_qty': 350},
        # 智能厨电系列
        'P0042': {'material_name': '智能加湿器', 'standard_cost': 48.60, 'sales_price': 129.00, 'safety_stock': 300, 'shelf_life': 1460, 'lead_time': 4, 'min_production_qty': 300},
        'P0043': {'material_name': '智能电饭煲', 'standard_cost': 158.90, 'sales_price': 399.00, 'safety_stock': 120, 'shelf_life': 1095, 'lead_time': 6, 'min_production_qty': 120},
        'P0044': {'material_name': '智能压力锅', 'standard_cost': 188.50, 'sales_price': 469.00, 'safety_stock': 100, 'shelf_life': 1095, 'lead_time': 7, 'min_production_qty': 100},
        'P0045': {'material_name': '智能微波炉', 'standard_cost': 228.60, 'sales_price': 549.00, 'safety_stock': 80, 'shelf_life': 1095, 'lead_time': 8, 'min_production_qty': 80},
        'P0046': {'material_name': '智能烤箱', 'standard_cost': 298.90, 'sales_price': 699.00, 'safety_stock': 60, 'shelf_life': 1095, 'lead_time': 9, 'min_production_qty': 60},
        'P0047': {'material_name': '智能洗碗机', 'standard_cost': 558.00, 'sales_price': 1299.00, 'safety_stock': 40, 'shelf_life': 730, 'lead_time': 12, 'min_production_qty': 30},
        'P0048': {'material_name': '智能洗衣机', 'standard_cost': 698.00, 'sales_price': 1599.00, 'safety_stock': 30, 'shelf_life': 730, 'lead_time': 14, 'min_production_qty': 20},
        'P0049': {'material_name': '智能冰箱', 'standard_cost': 898.00, 'sales_price': 1999.00, 'safety_stock': 25, 'shelf_life': 730, 'lead_time': 15, 'min_production_qty': 15},
        'P0050': {'material_name': '智能空调', 'standard_cost': 1198.00, 'sales_price': 2699.00, 'safety_stock': 20, 'shelf_life': 730, 'lead_time': 16, 'min_production_qty': 10},
        # 智能音箱扩展系列
        'P0051': {'material_name': '智能门锁 Pro', 'standard_cost': 1057.03, 'sales_price': 1426.99, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 13, 'min_production_qty': 10},
        'P0052': {'material_name': '智能摄像头 360', 'standard_cost': 735.62, 'sales_price': 993.09, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 6, 'min_production_qty': 10},
        'P0053': {'material_name': '智能路由器 AX', 'standard_cost': 465.19, 'sales_price': 628.01, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 10, 'min_production_qty': 10},
        # 智能生活扩展系列
        'P0054': {'material_name': '智能插座 Mini', 'standard_cost': 634.13, 'sales_price': 856.08, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 14, 'min_production_qty': 10},
        'P0055': {'material_name': '智能灯带 RGB', 'standard_cost': 1937.67, 'sales_price': 2615.85, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 13, 'min_production_qty': 10},
        'P0056': {'material_name': '智能窗帘电机', 'standard_cost': 1549.49, 'sales_price': 2091.81, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 8, 'min_production_qty': 10},
        'P0057': {'material_name': '智能温控器', 'standard_cost': 910.55, 'sales_price': 1229.24, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 13, 'min_production_qty': 10},
        'P0058': {'material_name': '智能烟雾报警器', 'standard_cost': 517.56, 'sales_price': 698.71, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 10},
        'P0059': {'material_name': '智能门铃 视频版', 'standard_cost': 1390.09, 'sales_price': 1876.62, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 14, 'min_production_qty': 10},
        'P0060': {'material_name': '智能净水器', 'standard_cost': 1531.78, 'sales_price': 2067.90, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 8, 'min_production_qty': 10},
        'P0061': {'material_name': '智能投影仪 Lite', 'standard_cost': 1526.49, 'sales_price': 2060.76, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 10},
        'P0062': {'material_name': '智能体脂秤', 'standard_cost': 696.26, 'sales_price': 939.95, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 8, 'min_production_qty': 10},
        'P0063': {'material_name': '智能手环 Fit', 'standard_cost': 1763.29, 'sales_price': 2380.44, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 10},
        'P0064': {'material_name': '智能手表 Sport', 'standard_cost': 1376.82, 'sales_price': 1858.71, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 8, 'min_production_qty': 10},
        'P0065': {'material_name': '智能耳机 Pro', 'standard_cost': 332.19, 'sales_price': 448.46, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 6, 'min_production_qty': 10},
        'P0066': {'material_name': '智能充电宝 20000mAh', 'standard_cost': 316.84, 'sales_price': 427.73, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 11, 'min_production_qty': 10},
        'P0067': {'material_name': '智能键盘 机械版', 'standard_cost': 1343.17, 'sales_price': 1813.28, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 15, 'min_production_qty': 10},
        'P0068': {'material_name': '智能鼠标 静音版', 'standard_cost': 1018.56, 'sales_price': 1375.06, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 11, 'min_production_qty': 10},
        'P0069': {'material_name': '智能音箱 Mini', 'standard_cost': 804.84, 'sales_price': 1086.53, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 8, 'min_production_qty': 10},
        'P0070': {'material_name': '智能台灯 护眼版', 'standard_cost': 201.95, 'sales_price': 272.63, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 9, 'min_production_qty': 10},
        'P0071': {'material_name': '智能加湿器', 'standard_cost': 495.26, 'sales_price': 668.60, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 13, 'min_production_qty': 10},
        'P0072': {'material_name': '智能空气净化器', 'standard_cost': 429.64, 'sales_price': 580.01, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 15, 'min_production_qty': 10},
        'P0073': {'material_name': '智能电饭煲', 'standard_cost': 1754.81, 'sales_price': 2368.99, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 10},
        'P0074': {'material_name': '智能烤箱 家用版', 'standard_cost': 682.96, 'sales_price': 922.00, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 11, 'min_production_qty': 10},
        'P0075': {'material_name': '智能扫地机器人', 'standard_cost': 456.74, 'sales_price': 616.60, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 9, 'min_production_qty': 10},
        'P0076': {'material_name': '智能拖地机器人', 'standard_cost': 1377.39, 'sales_price': 1859.48, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 8, 'min_production_qty': 10},
        'P0077': {'material_name': '智能晾衣架', 'standard_cost': 1185.53, 'sales_price': 1600.47, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 7, 'min_production_qty': 10},
        'P0078': {'material_name': '智能保险柜', 'standard_cost': 679.73, 'sales_price': 917.64, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 11, 'min_production_qty': 10},
        'P0079': {'material_name': '智能鱼缸', 'standard_cost': 1218.98, 'sales_price': 1645.62, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 10, 'min_production_qty': 10},
        'P0080': {'material_name': '智能花盆 自动浇水', 'standard_cost': 1447.89, 'sales_price': 1954.65, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 12, 'min_production_qty': 10},
        'P0081': {'material_name': '智能血压计', 'standard_cost': 770.95, 'sales_price': 1040.78, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 6, 'min_production_qty': 10},
        'P0082': {'material_name': '智能体温计', 'standard_cost': 1178.85, 'sales_price': 1591.45, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 10},
        'P0083': {'material_name': '智能血氧仪', 'standard_cost': 1213.44, 'sales_price': 1638.14, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 6, 'min_production_qty': 10},
        'P0084': {'material_name': '智能按摩仪', 'standard_cost': 1811.15, 'sales_price': 2445.05, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 14, 'min_production_qty': 10},
        'P0085': {'material_name': '智能筋膜枪', 'standard_cost': 1310.61, 'sales_price': 1769.32, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 6, 'min_production_qty': 10},
        'P0086': {'material_name': '智能护眼仪', 'standard_cost': 698.79, 'sales_price': 943.37, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 12, 'min_production_qty': 10},
        'P0087': {'material_name': '智能香薰机', 'standard_cost': 309.14, 'sales_price': 417.34, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 5, 'min_production_qty': 10},
        'P0088': {'material_name': '智能风扇 落地版', 'standard_cost': 775.57, 'sales_price': 1047.02, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 6, 'min_production_qty': 10},
        'P0089': {'material_name': '智能暖风机', 'standard_cost': 1634.51, 'sales_price': 2206.59, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 6, 'min_production_qty': 10},
        'P0090': {'material_name': '智能除湿机', 'standard_cost': 634.42, 'sales_price': 856.47, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 9, 'min_production_qty': 10},
        'P0091': {'material_name': '智能新风机', 'standard_cost': 1297.78, 'sales_price': 1752.00, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 8, 'min_production_qty': 10},
        'P0092': {'material_name': '智能晾衣机', 'standard_cost': 997.40, 'sales_price': 1346.49, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 7, 'min_production_qty': 10},
        'P0093': {'material_name': '智能门磁', 'standard_cost': 150.18, 'sales_price': 202.74, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 7, 'min_production_qty': 10},
        'P0094': {'material_name': '智能水浸传感器', 'standard_cost': 698.40, 'sales_price': 942.84, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 6, 'min_production_qty': 10},
        'P0095': {'material_name': '智能燃气报警器', 'standard_cost': 185.99, 'sales_price': 251.09, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 13, 'min_production_qty': 10},
        'P0096': {'material_name': '智能窗帘遥控器', 'standard_cost': 119.33, 'sales_price': 161.10, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 10, 'min_production_qty': 10},
        'P0097': {'material_name': '智能场景开关', 'standard_cost': 2197.67, 'sales_price': 2966.85, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 9, 'min_production_qty': 10},
        'P0098': {'material_name': '智能中控屏', 'standard_cost': 1927.67, 'sales_price': 2602.35, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 15, 'min_production_qty': 10},
        'P0099': {'material_name': '智能网关 Hub', 'standard_cost': 1384.14, 'sales_price': 1868.59, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 14, 'min_production_qty': 10},
        'P0100': {'material_name': '智能遥控器 万能版', 'standard_cost': 980.74, 'sales_price': 1324.00, 'safety_stock': 100, 'shelf_life': 1825, 'lead_time': 10, 'min_production_qty': 10},
    }

    if clean_import:
        BillOfMaterials.objects.all().delete()

    material_cache = _preload_materials()
    # 预加载现有BOM，用(parent_id, child_id)做key
    existing_bom = {}
    for bom in BillOfMaterials.objects.select_related('parent_material', 'child_material').all():
        key = (bom.parent_material_id, bom.child_material_id)
        existing_bom[key] = bom

    to_create = []
    to_update_objs = []

    for i, row in enumerate(rows, 1):
        try:
            parent_code = str(row.get('成品ID', '') or row.get('父项物料', '')
                          or row.get('parent_material', '')).strip()
            child_code = str(row.get('构成原材料ID', '') or row.get('子项物料', '')
                         or row.get('child_material', '')).strip()

            if not parent_code or not child_code:
                errors.append(f'第{i}行：成品ID或原材料ID不能为空')
                continue

            parent = material_cache.get(parent_code)
            child = material_cache.get(child_code)

            # 父项和子项物料都必须在物料主数据中预先定义，不自动创建空壳记录
            if not parent:
                # 成品父项必须存在于物料主数据中，不自动创建空壳记录
                # 因为成品物料必须在 01_物料.csv 中预先定义
                warnings.append(f'BOM第{i}行：父项成品 {parent_code} 不存在于物料主数据中，已跳过此行')
                continue

            if not child:
                # 子项物料也必须在物料主数据中预先定义，不自动创建空壳记录
                # 原材料/半成品应在 01_物料.csv 中预先导入
                warnings.append(f'BOM第{i}行：子项物料 {child_code} 不存在于物料主数据中，已跳过此行')
                continue

            # 检测BOM自引用（父项不能等于子项）— 用代码比较而非ID（自动创建的物料尚未保存无ID）
            if parent_code == child_code:
                errors.append(f'第{i}行：父项物料({parent_code})不能与子项物料({child_code})相同')
                continue

            quantity_raw = str(row.get('单位用量', '1') or row.get('quantity', '1'))
            # 清理非数字字符（如单位后缀"台"、"个"等）
            quantity_clean = re.sub(r'[^\d.\-]', '', str(quantity_raw))
            try:
                quantity = float(quantity_clean) if quantity_clean else 1.0
            except (ValueError, TypeError):
                quantity = 1.0
            # 防护：用量不能为负数
            if quantity < 0:
                errors.append(f'第{i}行：单位用量不能为负数（{quantity}）')
                continue
            bom_level = int(float(str(row.get('BOM层级', '1') or row.get('level', '1'))))
            unit = str(row.get('单位', row.get('unit', '件'))).strip()

            def safe_float(val, default=0.0):
                try:
                    v = str(val).strip()
                    return float(v) if v else default
                except (ValueError, TypeError):
                    return default

            usage_ratio = round(safe_float(row.get('用量占比(%)', row.get('usage_ratio', '0')), 0), 1)
            scrap_rate = round(safe_float(row.get('报废率', row.get('scrap_rate', '0')), 0), 3)
            # 读取并校验可替代物料列
            alt_raw = str(row.get('可替代物料', row.get('替代组', row.get('alternative_group', '')))).strip()
            alt_codes = [c.strip() for c in alt_raw.split(',') if c.strip()] if alt_raw else []
            if alt_codes:
                valid_codes = [c for c in alt_codes if c in material_cache]
                if len(valid_codes) < len(alt_codes):
                    invalid = set(alt_codes) - set(valid_codes)
                    warnings.append(f'BOM第{i}行：替代物料{list(invalid)}不存在于物料表中，已忽略')
                alternative_group = ','.join(valid_codes) if valid_codes else ''
            else:
                alternative_group = ''
            alternative_priority = int(float(str(row.get('优先级(1最高)', row.get('alternative_priority', '1')))))
            # 【修复】替代料用料比例：优先从"替代比例"/"替代用料比例"列读取
            #           "成本占比系数"是成本维度指标，不可混入用量比例（硬性约束第6条）
            #           若无明确替代比例则默认1.0（等比例分配）
            alt_ratio_raw = str(row.get('替代比例', row.get('替代用料比例', row.get('alternative_ratio', '1.0')))).strip()
            alternative_ratio = round(safe_float(alt_ratio_raw, 1.0), 2)
            factory_code = str(row.get('工厂代码', row.get('factory_code', ''))).strip()
            is_active_raw = str(row.get('是否启用', row.get('is_active', '是'))).strip()
            is_active = is_active_raw in ['是', '启用', 'True', 'true', '1', 'Y', 'y']

            defaults = {
                'quantity': quantity,
                'bom_level': bom_level,
                'unit': unit,
                'usage_ratio': usage_ratio,
                'scrap_rate': scrap_rate,
                'alternative_group': alternative_group or None,
                'alternative_priority': alternative_priority,
                'alternative_ratio': alternative_ratio,
                'factory_code': factory_code or None,
                'is_active': is_active,
            }

            key = (parent.id, child.id)

            if key in existing_bom:
                obj = existing_bom[key]
                for k, v in defaults.items():
                    setattr(obj, k, v)
                to_update_objs.append(obj)
                updated += 1
            else:
                to_create.append(BillOfMaterials(
                    parent_material=parent,
                    child_material=child,
                    **defaults
                ))
                imported += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    def _do_db_write_6():
        with transaction.atomic():
            # auto_created_materials 已废弃（父项和子项缺失均跳过，不再自动创建）
            if to_create:
                BillOfMaterials.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
            if to_update_objs:
                BillOfMaterials.objects.bulk_update(
                    to_update_objs,
                    ['quantity', 'bom_level', 'unit', 'usage_ratio', 'scrap_rate',
                     'alternative_group', 'alternative_priority', 'alternative_ratio',
                     'factory_code', 'is_active'],
                    batch_size=500
                )

    _retry_db_operation(_do_db_write_6)

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'BOM导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50],
        'warnings': warnings[:50]
    }


def _batch_import_workcenter(rows, clean_import):
    """高性能工作中心导入 - 真正的批量操作"""
    imported = updated = 0
    errors = []

    if clean_import:
        WorkCenter.objects.all().delete()

    existing_wc = {w.work_center_code: w for w in WorkCenter.objects.all()}
    to_create = []
    to_update_objs = []

    def parse_date_field(date_str):
        if not date_str or not str(date_str).strip():
            return None
        date_str = str(date_str).strip().split(' ')[0]
        # 标准化单数字月份/日期为双位：2026/5/21 → 2026/05/21
        if '/' in date_str:
            parts = date_str.split('/')
            if len(parts) == 3:
                date_str = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
        for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return None

    for i, row in enumerate(rows, 1):
        try:
            work_center_code = str(row.get('产线ID', '') or row.get('work_center_code', '')).strip()
            if not work_center_code:
                errors.append(f'第{i}行：产线ID不能为空')
                continue

            defaults = {
                'work_center_name': str(row.get('产线名称', '')).strip(),
                'available_products': str(row.get('可生产产品', '')).strip(),
                'daily_available_hours': round(float(str(row.get('每日可用工时', '0')).strip() or '0'), 2),
                'shift_count': int(str(row.get('班次数', '1')).strip() or '1'),
                'hours_per_shift': round(float(str(row.get('每班工时', '8')).strip() or '8'), 2),
                'production_days_per_week': int(str(row.get('每周生产天数', '') or row.get('每周工作天数', '5')).strip() or '5'),
                'planned_headcount': int(str(row.get('定编人数', '') or row.get('计划人数', '0')).strip() or '0'),
                'actual_headcount': int(str(row.get('在岗人数', '') or row.get('实际人数', '0')).strip() or '0'),
                'daily_capacity_limit': int(float(str(row.get('日产能上限', '0')).strip() or '0')),
                'changeover_time': round(float(str(row.get('换线时间(小时/次)', '') or row.get('换线时间', '0')).strip() or '0'), 2),
                'planned_maintenance_hours': round(float(str(row.get('计划维护停机时长(小时)', '') or row.get('计划维护时间', '0')).strip() or '0'), 2),
                'maintenance_start_date': parse_date_field(row.get('维护生效日期', '') or row.get('维护开始日期', '')),
                'maintenance_end_date': parse_date_field(row.get('维护失效日期', '') or row.get('维护结束日期', '')),
                'is_active': str(row.get('状态', '1')).strip() not in ['0', 'false', '否', '禁用']
            }

            if work_center_code in existing_wc:
                obj = existing_wc[work_center_code]
                for k, v in defaults.items():
                    setattr(obj, k, v)
                to_update_objs.append(obj)
                updated += 1
            else:
                to_create.append(WorkCenter(work_center_code=work_center_code, **defaults))
                imported += 1

        except Exception as e:
            errors.append(f'第{i}行：{str(e)}')

    with transaction.atomic():
        if to_create:
            WorkCenter.objects.bulk_create(to_create, batch_size=200, ignore_conflicts=True)
        if to_update_objs:
            WorkCenter.objects.bulk_update(
                to_update_objs,
                ['work_center_name', 'available_products', 'daily_available_hours',
                 'shift_count', 'hours_per_shift', 'production_days_per_week',
                 'planned_headcount', 'actual_headcount', 'daily_capacity_limit',
                 'changeover_time', 'planned_maintenance_hours',
                 'maintenance_start_date', 'maintenance_end_date', 'is_active'],
                batch_size=200
            )

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'工作中心导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_factory_calendar(rows, clean_import):
    """高性能工厂日历导入（独立表）"""
    imported = updated = 0
    errors = []
    from prediction.models import FactoryCalendar

    def _do_write():
        nonlocal imported
        with transaction.atomic():
            if clean_import:
                FactoryCalendar.objects.all().delete()
            to_create = []
            for i, row in enumerate(rows, 1):
                try:
                    date_str = str(row.get('日期', '')).strip()
                    if not date_str: continue
                    date_val = None
                    s = date_str
                    if '/' in s:
                        p = s.split('/')
                        if len(p) == 3: s = f'{p[0]}/{int(p[1]):02d}/{int(p[2]):02d}'
                    for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                        try: date_val = datetime.strptime(s, fmt).date(); break
                        except ValueError: continue
                    if date_val:
                        fc = str(row.get('工厂代码', '')).strip()
                        to_create.append(FactoryCalendar(
                            factory_code=fc if fc else 'DEFAULT', date=date_val,
                            is_workday=str(row.get('是否工作日', '是')).strip() in ['是','1','True'],
                            shift_count=1 if str(row.get('班次类型','正常'))=='正常' else (2 if '加班' else 0),
                            remarks=str(row.get('备注', ''))))
                        imported += 1
                except Exception as e:
                    errors.append(f'第{i}行：{str(e)}')
            if to_create:
                FactoryCalendar.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)

    _retry_db_operation(_do_write)
    return {'status': 'success' if not errors else 'partial',
            'message': f'工厂日历导入完成，新增{imported}条', 'imported': imported, 'updated': 0, 'errors': errors[:50]}


def _batch_import_factory_transfer(rows, clean_import):
    """高性能工厂调拨导入（独立表）"""
    imported = updated = 0
    errors = []
    from prediction.models import FactoryTransfer

    material_cache = _preload_materials()
    order_map = {o.order_no: o for o in SalesOrder.objects.all()}
    existing = {t.transfer_no: t for t in FactoryTransfer.objects.select_related('material','related_order').all()}

    def _do_write():
        nonlocal imported, updated
        with transaction.atomic():
            if clean_import: FactoryTransfer.objects.all().delete(); existing.clear()
            to_create, to_update = [], []
            status_map = {'已完成':'completed','进行中':'在途','待调拨':'pending'}
            for i, row in enumerate(rows, 1):
                try:
                    tn = str(row.get('调拨编号','')).strip()
                    if not tn: continue
                    mc = str(row.get('物料ID','')).strip()
                    defaults = {
                        'material': material_cache.get(mc) if mc else None,
                        'from_factory': str(row.get('源工厂','')).strip(),
                        'to_factory': str(row.get('目标工厂','')).strip(),
                        'quantity': int(float(str(row.get('调拨数量','0') or '0'))),
                        'reason': str(row.get('调拨原因','')).strip(),
                        'status': status_map.get(str(row.get('状态','待调拨')).strip(), 'pending'),
                    }
                    ad = str(row.get('预计到达日期','')).strip()
                    if ad:
                        if '/' in ad:
                            p=ad.split('/')
                            if len(p)==3: ad=f'{p[0]}/{int(p[1]):02d}/{int(p[2]):02d}'
                        for fmt in ['%Y-%m-%d','%Y/%m/%d']:
                            try: defaults['expected_arrival_date']=datetime.strptime(ad,fmt).date(); break
                            except ValueError: continue
                    ro = str(row.get('关联订单','')).strip()
                    if ro and ro in order_map: defaults['related_order'] = order_map[ro]
                    if tn in existing:
                        obj = existing[tn]
                        for k,v in defaults.items(): setattr(obj,k,v)
                        to_update.append(obj); updated += 1
                    else:
                        to_create.append(FactoryTransfer(transfer_no=tn,**defaults)); imported += 1
                except Exception as e: errors.append(f'第{i}行：{str(e)}')
            if to_create: FactoryTransfer.objects.bulk_create(to_create,batch_size=200,ignore_conflicts=True)
            if to_update: FactoryTransfer.objects.bulk_update(to_update,
                ['material_id','from_factory','to_factory','quantity','reason','expected_arrival_date','status','related_order_id'],batch_size=200)
    _retry_db_operation(_do_write)
    return {'status': 'success' if not errors else 'partial',
            'message': f'工厂调拨导入完成，新增{imported}条，更新{updated}条', 'imported': imported, 'updated': updated, 'errors': errors[:50]}


def _batch_import_priority_rule(rows, clean_import):
    """高性能优先级规则导入（独立表）"""
    imported = updated = 0
    errors = []
    from prediction.models import PriorityRule

    existing = {r.name: r for r in PriorityRule.objects.all()}

    def _do_write():
        nonlocal imported, updated
        with transaction.atomic():
            if clean_import: PriorityRule.objects.all().delete(); existing.clear()
            to_create, to_update = [], []
            for i, row in enumerate(rows, 1):
                try:
                    name = str(row.get('规则名称','')).strip()
                    if not name: continue
                    ia = str(row.get('状态','启用')).strip() in ['启用','active','1','True']
                    defaults = {
                        'strategy': str(row.get('策略类型','delivery_first')),
                        'urgency_weight': float(str(row.get('紧急度权重','0.25'))),
                        'delivery_weight': float(str(row.get('交期权重','0.25'))),
                        'customer_weight': float(str(row.get('客户等级权重','0.20'))),
                        'value_weight': float(str(row.get('订单价值权重','0.15'))),
                        'product_weight': float(str(row.get('产品组权重','0.10'))),
                        'inventory_status_weight': float(str(row.get('库存状态权重','0'))),
                        'capacity_utilization_weight': float(str(row.get('产能利用率权重','0'))),
                        'is_active': ia,
                    }
                    if name in existing:
                        obj = existing[name]
                        for k,v in defaults.items(): setattr(obj,k,v)
                        to_update.append(obj); updated += 1
                    else:
                        to_create.append(PriorityRule(name=name,**defaults)); imported += 1
                except Exception as e: errors.append(f'第{i}行：{str(e)}')
            if to_create: PriorityRule.objects.bulk_create(to_create,batch_size=100,ignore_conflicts=True)
            if to_update: PriorityRule.objects.bulk_update(to_update,
                ['strategy','urgency_weight','delivery_weight','customer_weight','value_weight','product_weight',
                 'inventory_status_weight','capacity_utilization_weight','is_active'],batch_size=100)
    _retry_db_operation(_do_write)
    return {'status': 'success' if not errors else 'partial',
            'message': f'优先级规则导入完成，新增{imported}条，更新{updated}条', 'imported': imported, 'updated': updated, 'errors': errors[:50]}


def _batch_import_engineering_change(rows, clean_import):
    """高性能工程变更ECN导入（独立表）"""
    imported = updated = 0
    errors = []
    from prediction.models import EngineeringChange

    mc = _preload_materials()
    existing = {e.ecn_no: e for e in EngineeringChange.objects.all()}

    def _parse_dt(ds):
        if not ds or not str(ds).strip(): return None
        s = str(ds).strip().split(' ')[0]
        if '/' in s:
            p=s.split('/')
            if len(p)==3: s=f'{p[0]}/{int(p[1]):02d}/{int(p[2]):02d}'
        for fmt in ['%Y-%m-%d','%Y/%m/%d']:
            try: return datetime.strptime(s,fmt).date()
            except ValueError: continue
        return None

    def _do_write():
        nonlocal imported, updated
        with transaction.atomic():
            if clean_import: EngineeringChange.objects.all().delete(); existing.clear()
            to_create, to_update = [], []
            for i, row in enumerate(rows, 1):
                try:
                    en = str(row.get('ECN编号','') or row.get('调拨编号','')).strip()
                    if not en: continue
                    mcode = str(row.get('物料ID','')).strip()
                    ia = str(row.get('状态','启用')).strip() in ['启用','active','1','True']
                    defaults = {
                        'material': mc.get(mcode) if mcode else None,
                        'change_type': str(row.get('变更类型','') or row.get('策略类型','材料替换')),
                        'reason': str(row.get('变更原因','') or row.get('调拨原因','')),
                        'related_product': str(row.get('关联产品','') or row.get('关联订单','')),
                        'ecn_category': str(row.get('ECN类别','') or row.get('规则名称','')),
                        'effective_date': _parse_dt(row.get('生效日期')),
                        'expiry_date': _parse_dt(row.get('失效日期')),
                        'status': 'active' if ia else 'inactive',
                        'remarks': str(row.get('备注','')),
                    }
                    if en in existing:
                        obj = existing[en]
                        for k,v in defaults.items(): setattr(obj,k,v)
                        to_update.append(obj); updated += 1
                    else:
                        to_create.append(EngineeringChange(ecn_no=en,**defaults)); imported += 1
                except Exception as e: errors.append(f'第{i}行：{str(e)}')
            if to_create: EngineeringChange.objects.bulk_create(to_create,batch_size=100,ignore_conflicts=True)
            if to_update: EngineeringChange.objects.bulk_update(to_update,
                ['material_id','change_type','reason','related_product','ecn_category',
                 'effective_date','expiry_date','status','remarks'],batch_size=100)
    _retry_db_operation(_do_write)
    return {'status': 'success' if not errors else 'partial',
            'message': f'工程变更导入完成，新增{imported}条，更新{updated}条', 'imported': imported, 'updated': updated, 'errors': errors[:50]}


def _batch_import_delivery_change(rows, clean_import):
    """高性能交期变更记录导入 - 批量操作"""
    imported = 0
    errors = []

    change_type_map = {
        '供应商延期': 'supplier_delay', '供应商提前': 'supplier_advance',
        '客户加急': 'customer_rush', '客户延后': 'customer_postpone',
        '物流问题': 'logistics_issue', '物料短缺': 'material_shortage',
        'supplier_delay': 'supplier_delay', 'supplier_advance': 'supplier_advance',
        'customer_rush': 'customer_rush', 'customer_postpone': 'customer_postpone',
        'logistics_issue': 'logistics_issue', 'material_shortage': 'material_shortage',
    }

    def _parse_dt(ds):
        if not ds or not str(ds).strip(): return None
        s = str(ds).strip().split(' ')[0]
        if '/' in s:
            p = s.split('/')
            if len(p) == 3: s = f'{p[0]}/{int(p[1]):02d}/{int(p[2]):02d}'
        for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
            try: return datetime.strptime(s, fmt).date()
            except ValueError: continue
        return None

    def _do_write():
        nonlocal imported
        with transaction.atomic():
            if clean_import:
                DeliveryChange.objects.all().delete()
            to_create = []
            for i, row in enumerate(rows, 1):
                try:
                    order_no = str(row.get('订单号', '') or row.get('关联订单号', '') or row.get('order_no', '')).strip()
                    if not order_no: continue

                    original_date = _parse_dt(row.get('原交付日期', '') or row.get('原定交付日期', '') or row.get('original_date', ''))
                    new_date = _parse_dt(row.get('新交付日期', '') or row.get('新定交付日期', '') or row.get('new_date', ''))
                    if not original_date or not new_date: continue

                    cd_raw = str(row.get('变更天数', '')).strip()
                    try: change_days = int(float(cd_raw))
                    except (ValueError, TypeError): change_days = (new_date - original_date).days

                    ct_cn = str(row.get('变更类型', '') or row.get('change_type', '')).strip()
                    change_type = change_type_map.get(ct_cn, 'supplier_change')

                    to_create.append(DeliveryChange(
                        order_no=order_no,
                        po_no=(str(row.get('采购单号', '') or row.get('po_no', '')).strip()) or None,
                        material_code=(str(row.get('物料ID', '') or row.get('物料代码', '') or row.get('material_code', '')).strip()) or None,
                        supplier_code=(str(row.get('供应商代码', '') or row.get('supplier_code', '')).strip()) or None,
                        change_type=change_type,
                        original_date=original_date,
                        new_date=new_date,
                        change_days=change_days,
                        reason=str(row.get('变更原因', '') or row.get('reason', '')),
                        change_by=str(row.get('变更来源', '') or row.get('change_by', '') or 'system'),
                    ))
                    imported += 1
                except Exception as e:
                    errors.append(f'第{i}行：{str(e)}')
            if to_create:
                DeliveryChange.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
    _retry_db_operation(_do_write)
    return {'status': 'success' if not errors else 'partial',
            'message': f'交期变更记录导入完成，新增{imported}条', 'imported': imported, 'updated': 0, 'errors': errors[:50]}


def _batch_import_factory_calendar_transfer(rows, clean_import):
    """高性能工厂日历与调拨导入 - 拆分后的独立表"""
    imported = updated = 0
    errors = []

    calendar_rows = []
    transfer_rows = []

    for row in rows:
        data_type = str(row.get('数据类型', '')).strip()
        if data_type == '工厂日历':
            calendar_rows.append(row)
        elif data_type == '工厂调拨':
            transfer_rows.append(row)

    existing_transfers = {}

    def _do_db_write_cal_transfer():
        nonlocal imported, updated
        with transaction.atomic():
            # 工厂日历（纯新增，用bulk_create）
            if calendar_rows:
                from prediction.models import FactoryCalendar
                if clean_import:
                    FactoryCalendar.objects.all().delete()

                cal_to_create = []
                for i, row in enumerate(calendar_rows, 1):
                    try:
                        date_str = str(row.get('日期', '')).strip()
                        if not date_str:
                            continue
                        date_val = None
                        normalized_cal2 = date_str
                        if '/' in normalized_cal2:
                            parts = normalized_cal2.split('/')
                            if len(parts) == 3:
                                normalized_cal2 = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                        for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                            try:
                                date_val = datetime.strptime(normalized_cal2, fmt).date()
                                break
                            except ValueError:
                                continue
                        if date_val:
                            is_workday = str(row.get('是否工作日', '是')).strip() in ['是', '1', 'True']
                            shift_type = str(row.get('班次类型', '正常'))
                            shift_count = 1 if shift_type == '正常' else (2 if shift_type == '加班' else 0)
                            fc = str(row.get('工厂代码', '')).strip()
                            factory_code = fc if fc else 'DEFAULT'
                            cal_to_create.append(FactoryCalendar(
                                factory_code=factory_code, date=date_val, is_workday=is_workday,
                                shift_count=shift_count, remarks=str(row.get('备注', ''))
                            ))
                            imported += 1
                    except Exception as e:
                        errors.append(f'工厂日历第{i}行：{str(e)}')

                if cal_to_create:
                    FactoryCalendar.objects.bulk_create(cal_to_create, batch_size=500, ignore_conflicts=True)

            # 工厂调拨（批量操作）
            if transfer_rows:
                from prediction.models import FactoryTransfer
                if clean_import:
                    FactoryTransfer.objects.all().delete()
                else:
                    for t in FactoryTransfer.objects.select_related('material', 'related_order').all():
                        existing_transfers[t.transfer_no] = t

                material_cache = _preload_materials()
                order_map = {o.order_no: o for o in SalesOrder.objects.all()}

                tr_to_create = []
                tr_to_update = []

                def parse_cfg_date(date_str):
                    if not date_str or not str(date_str).strip():
                        return None
                    ds = str(date_str).strip().split(' ')[0]
                    if '/' in ds:
                        parts = ds.split('/')
                        if len(parts) == 3:
                            ds = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                    for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            return datetime.strptime(ds, fmt).date()
                        except ValueError:
                            continue
                    return None

                transfer_status_map = {
                    '已完成': 'completed', '进行中': '在途', '待调拨': 'pending',
                    'completed': 'completed', '在途': '在途', 'pending': 'pending'
                }

                for i, row in enumerate(transfer_rows, 1):
                    try:
                        transfer_no = str(row.get('调拨编号', '')).strip()
                        if not transfer_no:
                            continue
                        material_code = str(row.get('物料ID', '')).strip()
                        material = material_cache.get(material_code) if material_code else None
                        status_raw = str(row.get('状态', '待调拨'))
                        status = transfer_status_map.get(status_raw.strip(), 'pending')

                        defaults = {
                            'material': material,
                            'from_factory': str(row.get('源工厂', '')).strip(),
                            'to_factory': str(row.get('目标工厂', '')).strip(),
                            'quantity': int(float(str(row.get('调拨数量', '0') or '0'))),
                            'reason': str(row.get('调拨原因', '')).strip(),
                            'expected_arrival_date': parse_cfg_date(row.get('预计到达日期', '')),
                            'status': status,
                        }

                        related_order_str = str(row.get('关联订单', '')).strip()
                        if related_order_str:
                            related_obj = order_map.get(related_order_str)
                            if related_obj:
                                defaults['related_order'] = related_obj

                        if transfer_no in existing_transfers:
                            obj = existing_transfers[transfer_no]
                            for k, v in defaults.items():
                                setattr(obj, k, v)
                            tr_to_update.append(obj)
                            updated += 1
                        else:
                            tr_to_create.append(FactoryTransfer(transfer_no=transfer_no, **defaults))
                            imported += 1
                    except Exception as e:
                        errors.append(f'工厂调拨第{i}行：{str(e)}')

                if tr_to_create:
                    FactoryTransfer.objects.bulk_create(tr_to_create, batch_size=200, ignore_conflicts=True)
                if tr_to_update:
                    FactoryTransfer.objects.bulk_update(
                        tr_to_update,
                        ['material_id', 'from_factory', 'to_factory', 'quantity',
                         'reason', 'expected_arrival_date', 'status', 'related_order_id'],
                        batch_size=200
                    )

    _retry_db_operation(_do_db_write_cal_transfer)

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'工厂日历与调拨导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_config_rules_ecn(rows, clean_import):
    """高性能规则与工程变更导入 - 拆分后的独立表"""
    imported = updated = 0
    errors = []

    rule_rows = []
    ecn_rows = []

    for row in rows:
        data_type = str(row.get('数据类型', '')).strip()
        if data_type == '优先级规则':
            rule_rows.append(row)
        elif data_type == '工程变更':
            ecn_rows.append(row)

    existing_rules = {}

    def _do_db_write_rules_ecn():
        nonlocal imported, updated
        with transaction.atomic():
            # 优先级规则（批量操作）
            if rule_rows:
                from prediction.models import PriorityRule
                if clean_import:
                    PriorityRule.objects.all().delete()
                else:
                    for r in PriorityRule.objects.all():
                        existing_rules[r.name] = r

                rule_to_create = []
                rule_to_update = []

                for i, row in enumerate(rule_rows, 1):
                    try:
                        rule_name = str(row.get('规则名称', '')).strip()
                        if not rule_name:
                            continue
                        status_raw = str(row.get('状态', '启用'))
                        is_active = status_raw in ['启用', 'active', '1', 'True']

                        defaults = {
                            'strategy': str(row.get('策略类型', 'delivery_first')),
                            'urgency_weight': float(str(row.get('紧急度权重', '0.25'))),
                            'delivery_weight': float(str(row.get('交期权重', '0.25'))),
                            'customer_weight': float(str(row.get('客户等级权重', '0.20'))),
                            'value_weight': float(str(row.get('订单价值权重', '0.15'))),
                            'product_weight': float(str(row.get('产品组权重', '0.10'))),
                            'inventory_status_weight': float(str(row.get('库存状态权重', '0'))),
                            'capacity_utilization_weight': float(str(row.get('产能利用率权重', '0'))),
                            'is_active': is_active
                        }

                        if rule_name in existing_rules:
                            obj = existing_rules[rule_name]
                            for k, v in defaults.items():
                                setattr(obj, k, v)
                            rule_to_update.append(obj)
                            updated += 1
                        else:
                            rule_to_create.append(PriorityRule(name=rule_name, **defaults))
                            imported += 1
                    except Exception as e:
                        errors.append(f'优先级规则第{i}行：{str(e)}')

                if rule_to_create:
                    PriorityRule.objects.bulk_create(rule_to_create, batch_size=100, ignore_conflicts=True)
                if rule_to_update:
                    PriorityRule.objects.bulk_update(
                        rule_to_update,
                        ['strategy', 'urgency_weight', 'delivery_weight',
                         'customer_weight', 'value_weight', 'product_weight',
                         'inventory_status_weight', 'capacity_utilization_weight', 'is_active'],
                        batch_size=100
                    )

            # 工程变更ECN（批量操作）
            if ecn_rows:
                from prediction.models import EngineeringChange
                if clean_import:
                    EngineeringChange.objects.all().delete()

                material_cache_ec = _preload_materials()
                ecn_to_create = []
                existing_ecns = {e.ecn_no: e for e in EngineeringChange.objects.all()}

                def _parse_date_ecn(date_str):
                    if not date_str or not str(date_str).strip():
                        return None
                    ds = str(date_str).strip().split(' ')[0]
                    if '/' in ds:
                        parts = ds.split('/')
                        if len(parts) == 3:
                            ds = f'{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}'
                    for fmt in ['%Y-%m-%d', '%Y/%m/%d']:
                        try:
                            return datetime.strptime(ds, fmt).date()
                        except ValueError:
                            continue
                    return None

                for i, row in enumerate(ecn_rows, 1):
                    try:
                        ecn_no = str(row.get('调拨编号', '')).strip()
                        if not ecn_no:
                            continue
                        material_code = str(row.get('物料ID', '')).strip()
                        material = material_cache_ec.get(material_code) if material_code else None

                        status_raw = str(row.get('状态', '启用'))
                        is_active = status_raw in ['启用', 'active', '1', 'True']

                        defaults = {
                            'material': material,
                            'change_type': str(row.get('策略类型', '材料替换')),
                            'reason': str(row.get('调拨原因', '')),
                            'related_product': str(row.get('关联订单', '')),
                            'ecn_category': str(row.get('规则名称', '')),
                            'effective_date': _parse_date_ecn(row.get('生效日期', '')),
                            'expiry_date': _parse_date_ecn(row.get('失效日期', '')),
                            'status': 'active' if is_active else 'inactive',
                            'remarks': str(row.get('备注', '')),
                        }

                        if ecn_no in existing_ecns:
                            obj = existing_ecns[ecn_no]
                            for k, v in defaults.items():
                                setattr(obj, k, v)
                            updated += 1
                        else:
                            ecn_to_create.append(EngineeringChange(ecn_no=ecn_no, **defaults))
                            imported += 1
                    except Exception as e:
                        errors.append(f'工程变更第{i}行：{str(e)}')

                if ecn_to_create:
                    EngineeringChange.objects.bulk_create(ecn_to_create, batch_size=100, ignore_conflicts=True)

    _retry_db_operation(_do_db_write_rules_ecn)

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'规则与工程变更导入完成，新增{imported}条，更新{updated}条',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }


def _batch_import_config(rows, clean_import):
    """兼容旧版混合配置文件的高性能导入（内部分流到两个新函数）"""
    result_cal = _batch_import_factory_calendar_transfer(rows, clean_import)
    result_rules = _batch_import_config_rules_ecn(rows, clean_import)

    all_errors = result_cal.get('errors', []) + result_rules.get('errors', [])
    total_imported = result_cal.get('imported', 0) + result_rules.get('imported', 0)
    total_updated = result_cal.get('updated', 0) + result_rules.get('updated', 0)

    return {
        'status': 'success' if not all_errors else ('partial' if (total_imported + total_updated) > 0 else 'error'),
        'message': f'系统配置导入完成（工厂日历/调拨/规则/工程变更）',
        'imported': total_imported,
        'updated': total_updated,
        'errors': all_errors[:50]
    }


def _batch_import_substitute(rows, clean_import=False):
    """高性能批量导入替代物料数据"""
    from .models import SubstituteMaterial, Material

    imported = 0
    updated = 0
    errors = []

    if clean_import:
        SubstituteMaterial.objects.all().delete()

    # 预加载物料缓存
    material_codes = set()
    for row in rows:
        code = str(row.get('物料代码', '')).strip()
        if code:
            material_codes.add(code)
    materials = Material.objects.filter(material_code__in=material_codes)
    material_cache = {m.material_code: m for m in materials}

    to_create = []
    to_update = []
    existing_subs = {}

    # 查询已存在的替代记录
    group_ids = set()
    for row in rows:
        gid = str(row.get('替代料组ID', '')).strip()
        code = str(row.get('物料代码', '')).strip()
        if gid and code:
            group_ids.add(gid)
    if group_ids:
        existing_qs = SubstituteMaterial.objects.filter(group_id__in=group_ids).select_related('material')
        for obj in existing_qs:
            key = (obj.group_id, obj.material.material_code)
            existing_subs[key] = obj

    for i, row in enumerate(rows, 1):
        try:
            group_id = str(row.get('替代料组ID', '')).strip()
            group_name = str(row.get('替代料组名称', '')).strip()
            material_code = str(row.get('物料代码', '')).strip()

            if not group_id or not material_code:
                continue

            material = material_cache.get(material_code)
            if not material:
                errors.append(f'替代物料第{i}行：物料代码「{material_code}」在物料表中不存在')
                continue

            priority = int(str(row.get('替代优先级', '1')).strip() or '1')
            ratio = round(float(str(row.get('替代比例', '1.0')).strip() or '1.0'), 2)
            is_default_raw = str(row.get('是否默认', '否')).strip()
            is_default = is_default_raw in ['是', 'Yes', 'True', 'true', '1']
            remark = str(row.get('备注', '')).strip()

            defaults = {
                'group_name': group_name,
                'priority': priority,
                'ratio': ratio,
                'is_default': is_default,
                'remark': remark,
                'is_active': True
            }

            key = (group_id, material_code)
            if key in existing_subs:
                obj = existing_subs[key]
                for k, v in defaults.items():
                    setattr(obj, k, v)
                to_update.append(obj)
                updated += 1
            else:
                to_create.append(SubstituteMaterial(
                    group_id=group_id, material=material, **defaults))
                imported += 1

        except Exception as e:
            errors.append(f'替代物料第{i}行：{str(e)}')

    if to_create:
        SubstituteMaterial.objects.bulk_create(to_create, batch_size=100, ignore_conflicts=True)
    if to_update:
        SubstituteMaterial.objects.bulk_update(
            to_update,
            ['group_name', 'priority', 'ratio', 'is_default', 'remark', 'is_active'],
            batch_size=100
        )

    return {
        'status': 'success' if not errors else ('partial' if (imported + updated) > 0 else 'error'),
        'message': f'替代物料导入完成（新增{imported}条，更新{updated}条）',
        'imported': imported,
        'updated': updated,
        'errors': errors[:50]
    }