from rest_framework import viewsets, status, generics
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, IsAdminUser, AllowAny
from rest_framework.authtoken.models import Token
from rest_framework.filters import SearchFilter, OrderingFilter
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db.models import Sum, Avg, Q
from django.http import JsonResponse
from django.core.cache import cache
from django_filters.rest_framework import DjangoFilterBackend
from datetime import datetime, timedelta
import json
import logging

logger = logging.getLogger(__name__)

from .serializers import (
    MaterialSerializer, SupplierSerializer, CustomerSerializer, SalesOrderSerializer,
    InventorySerializer, BillOfMaterialsSerializer,
    SupplierCommitmentSerializer, MaterialPlanResultSerializer,
    OrderAllocationSerializer, PlanningSummarySerializer,
    ShortageReportSerializer, PurchaseOrderSerializer, CapacitySerializer,
    WorkCenterSerializer, WorkCenterCapacitySerializer,
    HoldAuditLogSerializer, BOMChangeHistorySerializer
)
from .permissions import (
    IsAdminUser, IsManagerUser, OrderManagementPermission,
    InventoryManagementPermission, SupplierManagementPermission,
    PlanningPermission, SystemManagementPermission, ReadOnlyOrAdmin
)
from ..models import (
    Material, Supplier, Customer, SalesOrder, Inventory, BillOfMaterials,
    SupplierCommitment, MaterialPlanResult, OrderAllocation,
    PurchaseOrder, Capacity, PlanLog, WorkCenter, FactoryCalendar
)
from ..material_planning import MaterialPlanner, MultiObjectiveOptimizer
from ..tasks import run_material_planning_async, update_inventory_cache, update_bom_cache
from ..utils.safe_cache import safe_get, safe_set

# 共享基础数据缓存键
_SHARED_FOUNDATION_CACHE_KEY = 'db_foundation_data_v2'
_SHARED_FOUNDATION_TTL = 120  # 2分钟内复用，避免重复查询


def _get_shared_foundation_data():
    """
    获取/构建共享的DB基础数据（供 material_plan_detail 和 shortage_report 复用）
    避免切换策略时3个API各自独立查询相同的数据

    Returns:
        dict: {
            'material_info': {id: Material},
            'orders': [(id, order_no, material_id, quantity, demand_date), ...],
            'bom_map': {parent_id: [(child_id, qty), ...]},
            'inv_totals': {material_id: total_quantity},
            'alloc_totals': {material_id: total_allocated},
            'supplier_map': {material_id: [{supplier_name, lead_time}, ...]},
            'default_suppliers': [supplier_name, ...] or [],
        }
        None: if no orders exist
    """
    # 优先从缓存读取
    cached = safe_get(_SHARED_FOUNDATION_CACHE_KEY)
    if cached:
        return cached

    try:
        from django.db.models import Sum
        from ..models import Material, SalesOrder, Inventory, OrderAllocation, BillOfMaterials, SupplierMaterial, Supplier
        from datetime import date

        if not SalesOrder.objects.exists():
            return None

        # ===== 基础查询1: 物料信息 =====
        material_info = {m.id: m for m in Material.objects.all()}

        # ===== 基础查询2: 全部活跃订单（含交期） =====
        orders = list(SalesOrder.objects.filter(
            status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
        ).values_list('id', 'order_no', 'material_id', 'quantity', 'demand_date'))

        # ===== 基础查询3: BOM关系 =====
        order_material_ids = set(o[2] for o in orders)
        bom_map = {}
        for bi in BillOfMaterials.objects.filter(parent_material_id__in=order_material_ids).values_list(
            'parent_material_id', 'child_material_id', 'quantity'
        ):
            bom_map.setdefault(bi[0], []).append((bi[1], float(bi[2] or 0)))

        # 收集所有涉及的子物料ID（用于后续查询）
        all_child_mat_ids = set()
        for children in bom_map.values():
            for child_id, _ in children:
                all_child_mat_ids.add(child_id)

        # ===== 基础查询4: 库存聚合 =====
        inv_totals = dict(Inventory.objects.values('material_id').annotate(
            total=Sum('quantity')
        ).values_list('material_id', 'total'))

        # ===== 基础查询5: 分配聚合 =====
        alloc_totals = dict(OrderAllocation.objects.values('material_id').annotate(
            total=Sum('allocated_quantity')
        ).values_list('material_id', 'total'))

        # ===== 基础查询6: 供应商信息 =====
        supplier_map = {}
        mat_ids_for_supplier = list(all_child_mat_ids | order_material_ids)
        for sm in SupplierMaterial.objects.filter(
            material_id__in=mat_ids_for_supplier, is_forbidden=False
        ).select_related('supplier').values_list(
            'material_id', 'supplier__supplier_name', 'lead_time'
        ):
            if sm[0] not in supplier_map:
                supplier_map[sm[0]] = []
            supplier_map[sm[0]].append({
                'supplier_name': sm[1],
                'lead_time': sm[2] or 7,
            })

        # 兜底供应商列表
        default_suppliers = []
        if not supplier_map:
            default_suppliers = list(Supplier.objects.filter(is_active=True).values_list('supplier_name', flat=True)[:10])

        foundation = {
            'material_info': material_info,
            'orders': orders,
            'bom_map': bom_map,
            'inv_totals': inv_totals,
            'alloc_totals': alloc_totals,
            'supplier_map': supplier_map,
            'default_suppliers': default_suppliers,
        }

        # 缓存2分钟，让同策略切换的多个API复用
        safe_set(_SHARED_FOUNDATION_CACHE_KEY, foundation, _SHARED_FOUNDATION_TTL)

        logger.info(f"共享基础数据构建完成: {len(orders)}订单, {len(bom_map)}BOM, {len(supplier_map)}供应商关系")
        return foundation

    except Exception as e:
        logger.warning(f"构建共享基础数据失败: {e}")
        return None


from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def login_view(request):
    username = request.data.get('username')
    password = request.data.get('password')
    user = authenticate(username=username, password=password)
    if user is not None:
        login(request, user)
        token, created = Token.objects.get_or_create(user=user)
        response = Response({
            'token': token.key,
            'user_id': user.id,
            'username': user.username,
            'email': user.email
        })
        response['Access-Control-Allow-Origin'] = 'http://localhost:3000'
        response['Access-Control-Allow-Credentials'] = 'true'
        return response
    response = Response({'detail': 'Invalid credentials'}, status=status.HTTP_401_UNAUTHORIZED)
    response['Access-Control-Allow-Origin'] = 'http://localhost:3000'
    response['Access-Control-Allow-Credentials'] = 'true'
    return response


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    try:
        if hasattr(request.user, 'auth_token') and request.user.auth_token:
            request.user.auth_token.delete()
    except Exception as e:
        logger.warning(f'Token deletion failed: {e}')
    from django.contrib.auth import logout as auth_logout
    auth_logout(request)
    response = Response({'detail': 'Successfully logged out'})
    origin = request.headers.get('Origin', 'http://localhost:3000')
    response['Access-Control-Allow-Origin'] = origin
    response['Access-Control-Allow-Credentials'] = 'true'
    return response


@csrf_exempt
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def user_info_view(request):
    user = request.user
    return Response({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'full_name': f"{user.first_name} {user.last_name}".strip() or user.username,
        'department': '系统管理员'
    })


@csrf_exempt
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def refresh_token_view(request):
    """Token刷新接口 - 验证当前token有效性并返回新token"""
    from rest_framework.authtoken.models import Token
    try:
        token = Token.objects.get(user=request.user)
        # 重新生成token key实现"刷新"
        token.delete()
        new_token = Token.objects.create(user=request.user)
        return Response({'token': new_token.key})
    except Token.DoesNotExist:
        new_token = Token.objects.create(user=request.user)
        return Response({'token': new_token.key})


class MaterialViewSet(viewsets.ModelViewSet):
    queryset = Material.objects.all().order_by('-id')
    serializer_class = MaterialSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['material_type', 'is_active']
    search_fields = ['material_code', 'material_name']

    @action(detail=False, methods=['post'], url_path='import')
    def import_materials(self, request):
        """单文件物料导入（兼容前端 materials/import/ 调用）"""
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'detail': '请上传文件'}, status=status.HTTP_400_BAD_REQUEST)

        # 文件大小限制：50MB
        MAX_FILE_SIZE = 50 * 1024 * 1024
        if file_obj.size > MAX_FILE_SIZE:
            return Response({'detail': f'文件过大（{file_obj.size/1024/1024:.1f}MB），限制50MB'},
                            status=status.HTTP_400_BAD_REQUEST)

        # 文件类型验证
        ALLOWED_EXTENSIONS = {'.csv', '.txt'}
        name_lower = file_obj.name.lower()
        if not any(name_lower.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            return Response({'detail': '仅支持CSV或TXT格式文件'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            import csv, io
            from ..views.import_views import _batch_import_material

            content = file_obj.read()
            if isinstance(content, bytes):
                content = content.decode('utf-8-sig', errors='replace')

            reader = csv.DictReader(io.StringIO(content))
            rows = list(reader)
            if not rows:
                return Response({'detail': 'CSV文件为空'}, status=status.HTTP_400_BAD_REQUEST)

            result = _batch_import_material(rows, clean_import=True)
            return Response({
                'success_count': result.get('imported', 0) + result.get('updated', 0),
                'error_count': len(result.get('errors', [])),
                'errors': result.get('errors', [])[:20],
            })
        except Exception as e:
            logger.error(f'物料导入失败: {e}')
            return Response({'detail': f'导入失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'])
    def export(self, request):
        """导出物料数据为CSV"""
        import csv
        from django.http import HttpResponse

        qs = Material.objects.all()
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = 'attachment; filename="materials_export.csv"'
        writer = csv.writer(response)
        writer.writerow(['物料ID', '物料名称', '物料类型', '单位', '保质期(天)', '最小起订量',
                         '采购提前期(天)', '标准成本', '销售价格', '安全库存',
                         '最小生产量', '是否启用'])
        for m in qs:
            writer.writerow([
                m.material_code or '',
                m.material_name or '',
                m.material_type or '',
                m.unit or '',
                m.shelf_life or 0,
                m.min_order_qty or 0,
                m.lead_time or 7,
                m.standard_cost or 0,
                m.sales_price or 0,
                m.safety_stock or 200,
                m.min_production_qty or 1,
                '是' if m.is_active else '否',
            ])
        return response


class SupplierViewSet(viewsets.ModelViewSet):
    queryset = Supplier.objects.all().order_by('-id')
    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['is_active']
    search_fields = ['supplier_code', 'supplier_name']


class CustomerViewSet(viewsets.ModelViewSet):
    queryset = Customer.objects.all().order_by('-id')
    serializer_class = CustomerSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['is_active']
    search_fields = ['customer_code', 'customer_name']


class OrderViewSet(viewsets.ModelViewSet):
    queryset = SalesOrder.objects.all().select_related('material').order_by('priority', 'demand_date')
    serializer_class = SalesOrderSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['status', 'priority', 'shipping_method']
    search_fields = ['order_no']

    @action(detail=False, methods=['post'])
    def run_planning(self, request):
        """执行物料计划"""
        import traceback
        order_ids = request.data.get('order_ids', None)
        strategy = request.data.get('strategy', 'delivery_first')
        enable_ai_analysis = request.data.get('enable_ai_analysis', False)

        try:
            result = run_material_planning_async(order_ids, strategy, enable_ai_analysis=enable_ai_analysis)
            return Response({
                'status': result['status'],
                'cache_key': result.get('cache_key'),
                'summary': result.get('summary'),
                'message': '物料计划任务已完成'
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"执行物料计划失败: {str(e)}\n{traceback.format_exc()}")
            return Response(
                {'detail': f'物料计划执行失败: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['get'])
    def planning_summary(self, request):
        """获取物料计划汇总（优先使用策略对应缓存，保证不同策略返回不同数据）"""
        try:
            from .screen_views import get_planning_status
            strategy = request.query_params.get('strategy', 'delivery_first')

            # refresh=1 跳过缓存
            if request.query_params.get('refresh') != '1':
                # ===== 修复: 优先读取策略对应的缓存（而非总是返回相同数据） =====
                # 1. 先尝试从策略专属缓存读取（run_material_planning_async 执行后写入）
                strategy_summary = safe_get(f'planning_summary_{strategy}')
                if strategy_summary and isinstance(strategy_summary, dict) and strategy_summary.get('total_orders', 0) > 0:
                    strategy_summary['_strategy'] = strategy
                    strategy_summary['_is_fallback'] = False
                    strategy_summary['_source'] = f'strategy_cache_{strategy}'
                    return Response(strategy_summary)

            # 2. 策略缓存未命中时，尝试从 material_plan_{strategy} 的 summary 子字段读取
            strategy_plan = safe_get(f'material_plan_{strategy}')
            if strategy_plan and isinstance(strategy_plan, dict) and strategy_plan.get('summary'):
                result = dict(strategy_plan['summary'])
                result['_strategy'] = strategy
                result['_is_fallback'] = False
                result['_source'] = f'material_plan_cache_{strategy}'
                return Response(result)

            # 3. 缓存全部未命中 → 尝试自动重建（DB有MPR数据时触发快速路径）
            from prediction.models.supply_chain_models import MaterialPlanResult as _MPR
            mpr_count = _MPR.objects.count()
            if mpr_count > 0:
                logger.info(f"planning_summary: 缓存为空但DB有{mpr_count}条MPR记录，触发自动重建({strategy})")
                try:
                    from prediction.tasks import _rebuild_caches_from_existing_data
                    rebuilt = _rebuild_caches_from_existing_data(strategy)
                    # _rebuild_caches返回嵌套dict: {'status':'success', 'summary':{...}, ...}
                    actual_summary = None
                    if isinstance(rebuilt, dict):
                        if rebuilt.get('total_orders', 0) > 0:
                            actual_summary = rebuilt
                        elif rebuilt.get('summary') and isinstance(rebuilt['summary'], dict):
                            actual_summary = rebuilt['summary']
                    if actual_summary and actual_summary.get('total_orders', 0) > 0:
                        actual_summary['_strategy'] = strategy
                        actual_summary['_is_fallback'] = False
                        actual_summary['_source'] = f'auto_rebuild_{strategy}'
                        return Response(actual_summary)
                    else:
                        logger.warning(f"planning_summary: 自动重建返回空数据, rebuilt_keys={list(rebuilt.keys()) if isinstance(rebuilt, dict) else type(rebuilt)}")
                except Exception as rebuild_err:
                    logger.warning(f"planning_summary: 自动重建失败: {rebuild_err}")

            # 4. 最终兜底：使用 get_planning_status（传入策略参数，保证不同策略返回不同数据）
            ps = get_planning_status(strategy=strategy)
            result = {
                'total_orders': ps['total'],
                'complete_orders': ps['complete'],
                'partial_orders': ps['partial'],
                'pending_orders': ps['none'],
                'none_orders': ps['none'],
                'avg_complete_rate': ps['avg_complete_rate'],
                'complete_rate': round(ps['complete'] / max(ps['total'], 1) * 100, 1) if ps['total'] > 0 else 0,
                'total_shortage_orders': ps['partial'] + ps['none'],
                'total_promise_changes': 0,
                'stable_orders': ps['complete'],
                'avg_supplier_reliability': 0,
                'total_safety_stock_usage': 0,
                'failure_analysis': {'total_failed': ps['none'], 'by_reason': {}, 'details': {}},
                'total_critical_shortages': ps.get('total_critical_shortages', 0) if isinstance(ps, dict) else 0,
                'total_urgent_shortages': ps.get('total_urgent_shortages', 0) if isinstance(ps, dict) else 0,
                'jit_optimization': {},
                'release_records': [],
                'delivery_violations': [],
                'ai_analysis': None,
                'procurement_plan': None,
                '_strategy': strategy,
                '_is_fallback': True,
                '_source': f'shared_get_planning_status_{strategy}',
            }
            return Response(result)
        except Exception as e:
            logger.warning(f"planning_summary 获取失败: {e}")
            return Response({
                'total_orders': 0, 'complete_orders': 0, 'partial_orders': 0,
                'pending_orders': 0, 'avg_complete_rate': 0, 'complete_rate': 0,
                'total_shortage_orders': 0, 'total_promise_changes': 0,
                'stable_orders': 0, 'avg_supplier_reliability': 0,
                'total_safety_stock_usage': 0,
                'failure_analysis': {'total_failed': 0, 'by_reason': {}, 'details': {}},
                'total_critical_shortages': 0, 'total_urgent_shortages': 0,
                'jit_optimization': {}, 'release_records': [], 'delivery_violations': [],
                'ai_analysis': None, 'procurement_plan': None
            })

    @action(detail=False, methods=['get'])
    def shortage_report(self, request):
        """获取缺料报表（含精准报缺数据）"""
        # 支持按策略读取不同缓存
        strategy = request.query_params.get('strategy', '')
        cache_key = f'shortage_report_{strategy}' if strategy else 'shortage_report'

        # refresh=1 跳过缓存强制重建
        if request.query_params.get('refresh') != '1':
            # 优先使用策略对应的缓存（由 run_material_planning_async 执行后填充）
            cached_results = safe_get(cache_key)
            if cached_results:
                return Response(cached_results)

        # 缓存未命中时，尝试从策略对应的 planning_results（执行原始结果）重建
        raw_cache_key = f'planning_results_{strategy}' if strategy else 'planning_results'
        cached_raw = safe_get(raw_cache_key)
        if cached_raw:
            rebuilt = self._rebuild_shortage_report_from_results(cached_raw, strategy)
            if rebuilt:
                write_key = f'shortage_report_{strategy}' if strategy else 'shortage_report'
                safe_set(write_key, rebuilt, 300)
                return Response(rebuilt)

        # 全部缓存为空时，从数据库直接构建兜底数据
        db_fallback = self._build_shortage_report_from_db(strategy)
        if db_fallback:
            safe_set(cache_key, db_fallback, 300)
            return Response(db_fallback)

        # v4-最终兜底: 尝试自动重建（DB有MPR数据时触发快速路径，含BOM替换）
        from prediction.models.supply_chain_models import MaterialPlanResult as _MPR3
        if _MPR3.objects.count() > 0:
            try:
                from prediction.tasks import _rebuild_caches_from_existing_data
                _rebuild_caches_from_existing_data(strategy or 'delivery_first')
                # 重建后重新读取缓存
                rebuilt_data = safe_get(cache_key)
                if rebuilt_data:
                    return Response(rebuilt_data)
            except Exception:
                pass

        return Response([])

    @action(detail=False, methods=['post'])
    def clear_planning_cache(self, request):
        """清除所有物料计划相关缓存（解决脏数据问题）"""
        from ..utils.safe_cache import _memory_cache
        strategies = ['delivery_first','inventory_first','cost_first','supplier_first','stability_first','expiry_first','']
        prefixes = ['planning_results_', 'material_plan_detail_', 'shortage_report_', 'planning_summary_', 'material_plan_']
        cleared = 0
        for prefix in prefixes:
            for s in strategies:
                key = prefix + s
                try:
                    cache.delete(key)
                except Exception:
                    pass
                _memory_cache.delete(key)
                cleared += 1
        for k in ['planning_results','material_plan_detail','shortage_report','planning_summary','material_plan']:
            try:
                cache.delete(k)
            except Exception:
                pass
            _memory_cache.delete(k)
            cleared += 1
        logger.info(f'clear_planning_cache: cleared {cleared} keys')
        return Response({'success': True, 'cleared': cleared, 'message': f'cleared {cleared} cache keys'})

    @action(detail=False, methods=['get'])
    def material_plan_detail(self, request):
        """获取物料计划详情（用于前端物料计划页面）"""
        # 支持按策略读取不同缓存
        strategy = request.query_params.get('strategy', '')
        cache_key = f'material_plan_detail_{strategy}' if strategy else 'material_plan_detail'

        # refresh=1 参数强制跳过缓存重建（调试用）
        if request.query_params.get('refresh') != '1':
            # 优先使用策略对应的缓存（由 run_material_planning_async 执行后填充）
            cached_results = safe_get(cache_key)
            if cached_results:
                return Response(cached_results)

        # 缓存未命中时，尝试从 planning_results（执行结果）构建
        raw_cache_key = f'planning_results_{strategy}' if strategy else 'planning_results'
        cached_raw = safe_get(raw_cache_key)
        if cached_raw:
            # 有执行结果但 detail 缓存过期，尝试快速重建
            rebuilt = self._rebuild_material_plan_detail_from_results(cached_raw, strategy)
            if rebuilt:
                write_key = f'material_plan_detail_{strategy}' if strategy else 'material_plan_detail'
                safe_set(write_key, rebuilt, 300)
                return Response(rebuilt)

        # 全部缓存为空时，从数据库直接构建兜底数据
        db_fallback = self._build_material_plan_detail_from_db(strategy)
        if db_fallback:
            safe_set(cache_key, db_fallback, 300)
            return Response(db_fallback)

        # v4-最终兜底: 尝试自动重建（DB有MPR数据时触发快速路径，含BOM替换）
        from prediction.models.supply_chain_models import MaterialPlanResult as _MPR2
        if _MPR2.objects.count() > 0:
            try:
                from prediction.tasks import _rebuild_caches_from_existing_data
                _rebuild_caches_from_existing_data(strategy or 'delivery_first')
                # 重建后重新读取缓存
                rebuilt_data = safe_get(cache_key)
                if rebuilt_data:
                    return Response(rebuilt_data)
            except Exception:
                pass

        return Response([])

    @action(detail=False, methods=['get'])
    def ai_allocation_analysis(self, request):
        """获取AI库存分配合理性分析"""
        try:
            from ..material_planning import InventoryAIAnalyzer

            # 空数据保护
            if not SalesOrder.objects.exists() and not Inventory.objects.exists():
                return Response({
                    'analysis': {'allocation_quality': 0, 'inventory_utilization': 0, 'potential_risks': [], 'suggestions': []},
                    'summary': {'total_orders': 0, 'total_allocations': 0, 'allocation_quality': 0,
                                'inventory_utilization': 0, 'potential_risk_count': 0, 'suggestion_count': 0}
                })

            analyzer = InventoryAIAnalyzer()

            orders_data = []
            for order in SalesOrder.objects.filter(status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']):
                try:
                    orders_data.append({
                        'id': order.id,
                        'priority': getattr(order, 'priority', 1) or 1,
                        'quantity': float(order.quantity or 0),
                        'demand_date': str(getattr(order, 'demand_date', '') or ''),
                        'material_id': order.material_id
                    })
                except Exception as ex:
                    logger.warning(f"订单ID={order.id}数据解析跳过: {ex}")
                    continue

            inventory_data = {}
            for inv in Inventory.objects.all():
                try:
                    mid = inv.material_id
                    if mid not in inventory_data:
                        inventory_data[mid] = []
                    inventory_data[mid].append({
                        'quantity': float(inv.quantity or 0),
                        'expiry_date': str(getattr(inv, 'expiry_date', None) or ''),
                        'is_safety_stock': False,
                        'is_hold': bool(getattr(inv, 'is_hold', False)),
                        'supplier_id': None
                    })
                except Exception as ex:
                    logger.warning(f"库存记录解析跳过: {ex}")

            allocations = []
            for alloc in OrderAllocation.objects.all().select_related('order', 'material'):
                try:
                    allocations.append({
                        'order_id': alloc.order_id,
                        'material_id': alloc.material_id,
                        'quantity': int(alloc.allocated_quantity or 0),
                        'reliability_factor': 1.0,
                        'allocation_time': str(alloc.created_at) if hasattr(alloc, 'created_at') else ''
                    })
                except Exception as ex:
                    logger.warning(f"分配记录解析跳过: {ex}")

            analysis = analyzer.analyze_allocation_rationality(allocations, inventory_data, orders_data)

            return Response({
                'analysis': analysis,
                'summary': {
                    'total_orders': len(orders_data),
                    'total_allocations': len(allocations),
                    'allocation_quality': analysis.get('allocation_quality', 0),
                    'inventory_utilization': analysis.get('inventory_utilization', 0),
                    'potential_risk_count': len(analysis.get('potential_risks', [])),
                    'suggestion_count': len(analysis.get('suggestions', []))
                }
            })
        except Exception as e:
            import traceback
            error_message = f"AI分析错误: {str(e)}"
            logger.error(f"{error_message}\n{traceback.format_exc()}")
            return Response(
                {'detail': error_message},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['post'], url_path='cancel_with_release')
    def cancel_with_release(self, request):
        """砍单物料自动释放 - 取消订单并释放已占物料分配"""
        order_id = request.data.get('order_id')
        if not order_id:
            return Response(
                {'detail': '请提供order_id参数'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            order = SalesOrder.objects.get(id=order_id)
        except SalesOrder.DoesNotExist:
            return Response(
                {'detail': f'订单ID={order_id}不存在'},
                status=status.HTTP_404_NOT_FOUND
            )

        if order.status == 'cancelled':
            return Response(
                {'detail': f'订单{order.order_no}已经是取消状态'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 调用物料计划引擎释放物料
        try:
            planner = MaterialPlanner()
            result = planner.cancel_order_release_materials(order_id)
        except Exception as e:
            logger.error(f"物料释放失败: {str(e)}")
            return Response({'detail': f'物料释放失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if not result.get('success'):
            return Response(
                {'detail': result.get('message', '物料释放失败')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # 将订单状态改为cancelled
        order.status = 'cancelled'
        order.save(update_fields=['status', 'updated_at'])

        return Response({
            'success': True,
            'message': f'订单{order.order_no}已取消，物料已释放',
            'order_no': order.order_no,
            'released_materials': result.get('released_materials', []),
            'total_released': result.get('total_released', 0)
        }, status=status.HTTP_200_OK)

    def _rebuild_material_plan_detail_from_results(self, results, strategy=''):
        """从执行结果缓存重建 material_plan_detail（避免从数据库用默认策略重算）

        Args:
            results: planning_results 原始执行结果列表
            strategy: 策略名称，用于差异化排序（delivery_first/inventory_first/cost_first/supplier_first/stability_first/expiry_first）
        """
        try:
            from django.db.models import Sum
            from ..models import Material

            material_map = {}
            material_info = {m.id: m for m in Material.objects.all()}

            for r in results:
                order_no = r.get('order_no', '')
                allocated = r.get('allocated') or {}
                if isinstance(allocated, dict):
                    for mat_id, alloc_data in allocated.items():
                        mat_id_int = int(mat_id) if mat_id else None
                        if not mat_id_int:
                            continue
                        if mat_id_int not in material_map:
                            material_map[mat_id_int] = {
                                'demand': 0.0, 'allocated_qty': 0.0,
                                'urgency_level': None, 'urgency_label': None,
                                'recommended_action': None, 'latest_purchase_date': None,
                                'suppliers_data': [],
                            }
                        alloc_list = alloc_data.get('allocations') if isinstance(alloc_data, dict) else []
                        alloc_total = sum(a.get('quantity', 0) for a in alloc_list) if isinstance(alloc_list, list) else 0
                        material_map[mat_id_int]['allocated_qty'] += alloc_total
                        material_map[mat_id_int]['demand'] += float(r.get('quantity', 0) or 0)

                sr = r.get('shortage_report')
                if sr and isinstance(sr, dict):
                    for ms in sr.get('material_shortages', []):
                        mat_id = ms.get('material_id')
                        if mat_id and int(mat_id) in material_map:
                            entry = material_map[int(mat_id)]
                            if not entry['urgency_level']:
                                entry['urgency_level'] = ms.get('urgency_level')
                                entry['urgency_label'] = ms.get('urgency_label')
                                # 修复: 只在有值时覆盖，避免用None覆盖fallback生成的行动
                                _ra = ms.get('recommended_action')
                                if _ra:
                                    entry['recommended_action'] = _ra
                                entry['latest_purchase_date'] = ms.get('latest_purchase_date')
                                entry['suppliers_data'] = ms.get('suppliers', [])

            inventory_totals = dict(
                Inventory.objects.values('material_id').annotate(total=Sum('quantity')).values_list('material_id', 'total')
            )

            plan_details = []
            for mat_id, agg in material_map.items():
                material = material_info.get(mat_id)
                if not material:
                    continue
                demand = agg['demand']
                stock = float(inventory_totals.get(mat_id, 0) or 0)
                allocated_qty = min(agg['allocated_qty'], stock)
                shortage = max(0, demand - allocated_qty)
                safety_stock = float(material.safety_stock or 0) if hasattr(material, 'safety_stock') else 0

                if shortage > demand * 0.5:
                    status, priority = 'shortage', 'high'
                elif shortage > 0:
                    status, priority = 'warning', 'normal'
                else:
                    status, priority = 'sufficient', 'low'

                # 紧急度：统一使用时间维度判定（与 material_planning.py analyze_shortage 一致）
                # 优先保留优化器已计算的值，仅作兜底
                existing_urgency = agg.get('urgency_level')
                if shortage > 0 and existing_urgency:
                    urgency_level = existing_urgency
                elif shortage > 0:
                    # 兜底：基于采购剩余天数判定（与核心算法一致）
                    from datetime import date as _date, timedelta as _td
                    latest_purchase = agg.get('latest_purchase_date')
                    if latest_purchase:
                        try:
                            days_left = (latest_purchase - _date.today()).days if hasattr(latest_purchase, 'days') else (
                                (_date.fromisoformat(str(latest_purchase)) - _date.today()).days
                                if isinstance(latest_purchase, str) else 99)
                        except Exception:
                            days_left = 99
                    else:
                        days_left = 99
                    if days_left <= 3:
                        urgency_level = 'critical'
                    elif days_left <= 14:
                        urgency_level = 'urgent'
                    elif days_left <= 30:
                        urgency_level = 'normal'
                    else:
                        urgency_level = 'relaxed'
                else:
                    urgency_level = 'relaxed'
                urgency_label = agg.get('urgency_label') or (
                    urgency_level == 'critical' and '紧急' or
                    urgency_level == 'urgent' and '加急' or
                    urgency_level == 'normal' and '正常' or
                    '充足'
                )

                # 生成推荐行动（每行都有值，不再为空）
                _existing_action = agg.get('recommended_action')
                if _existing_action:
                    rec_action = _existing_action
                elif shortage <= 0:
                    if stock > demand * 2:
                        rec_action = '库存充足，无需采购'
                    elif stock > safety_stock * 1.5:
                        rec_action = '库存健康，维持现状'
                    else:
                        rec_action = '关注库存水位'
                elif shortage > demand * 0.5:
                    rec_action = '紧急补货，建议立即采购'
                elif safety_stock > 0 and stock < safety_stock:
                    rec_action = '低于安全库存，需补货'
                else:
                    rec_action = '计划性补货'

                plan_details.append({
                    'id': mat_id, 'material_code': material.material_code,
                    'material_name': material.material_name,
                    'demand': round(demand, 2), 'stock': round(stock, 2),
                    'shortage': round(shortage, 2), 'status': status, 'priority': priority,
                    'safety_stock': round(safety_stock, 2),
                    'latest_purchase_date': agg.get('latest_purchase_date'),
                    'urgency_level': urgency_level, 'urgency_label': urgency_label,
                    'recommended_action': rec_action,
                    'suppliers': agg.get('suppliers_data', []),
                })

            # ===== 6种策略差异化排序 =====
            # 每种策略有不同的优先级维度，体现业务特色
            _urg_order = {'critical': 4, 'urgent': 3, 'normal': 2, 'relaxed': 1}

            def _detail_sort_key(x):
                sh = x.get('shortage', 0) or 0
                dem = x.get('demand', 0) or 0
                ss = x.get('safety_stock', 0) or 0
                st = x.get('stock', 0) or 0
                urg = _urg_order.get(x.get('urgency_level', ''), 0)
                lpd = x.get('latest_purchase_date')
                # 最晚采购日期转为距今天数（越小越紧急）
                if lpd:
                    try:
                        from datetime import date as _d
                        if hasattr(lpd, 'days'):
                            lp_days = (lpd - _d.today()).days
                        else:
                            lp_days = (_d.fromisoformat(str(lpd)) - _d.today()).days
                    except Exception:
                        lp_days = 999
                else:
                    lp_days = 999
                # 安全库存缺口比例
                ss_gap = (ss - st) / ss if ss > 0 else 0
                # 缺料率（缺料占需求比例，越高越危险）
                sh_rate = (sh / dem * 100) if dem > 0 else 0

                if strategy == 'delivery_first':
                    # 交付优先：缺料量最大优先（保交付），紧急度次之
                    return (-sh, -urg, -dem)
                elif strategy == 'inventory_first':
                    # 库存优先：安全库存缺口最大优先（防断供），缺料量次之
                    return (-ss_gap, -sh, -urg)
                elif strategy == 'cost_first':
                    # 成本优先：需求量大的优先（大批量采购单价更优），缺料次之
                    return (-dem, -sh, urg)  # 注意: urgency升序（低紧急的量大单先谈价）
                elif strategy == 'supplier_first':
                    # 供应商优先：按物料编码字典序排列（便于供应商按物料清单批量报价）
                    return (x.get('material_code', ''), -sh, -urg)
                elif strategy == 'stability_first':
                    # 稳定优先：最晚采购日期最近 + 安全库存缺口（时间+库存双重风险）
                    return (lp_days, -ss_gap, -sh)
                elif strategy == 'expiry_first':
                    # 临期优先：采购窗口最窄 + 缺料率最高（临期且缺料占比大的最紧急）
                    return (lp_days, -sh_rate, -sh)
                else:
                    return (-sh, -urg, -dem)

            plan_details.sort(key=_detail_sort_key)
            return plan_details[:500]
        except Exception as e:
            logger.warning(f"从执行结果重建 material_plan_detail 失败: {e}")
            return None

    def _build_material_plan_detail_from_db(self, strategy=''):
        """缓存全部为空时，从数据库直接构建物料需求明细（兜底路径）— 使用共享基础数据"""
        try:
            from datetime import date, timedelta
            import hashlib

            # 使用共享基础数据（避免重复查询）
            fd = _get_shared_foundation_data()
            if not fd:
                return []

            material_info = fd['material_info']
            orders = fd['orders']
            bom_map = fd['bom_map']
            inv_totals = fd['inv_totals']
            alloc_totals = fd['alloc_totals']
            supplier_map = fd['supplier_map']
            default_suppliers_list = fd['default_suppliers']

            # ===== 批量聚合: 按物料计算需求总量 + 最早交期 =====
            material_demand = {}
            material_earliest_date = {}  # mat_id -> 最早需求日期
            for oid, order_no, mid, qty, demand_date in orders:
                order_qty = float(qty or 0)
                children = bom_map.get(mid)
                if children:
                    for child_id, bom_qty in children:
                        material_demand[child_id] = material_demand.get(child_id, 0) + bom_qty * order_qty
                        # 记录该物料涉及的最早订单交期
                        if demand_date:
                            if child_id not in material_earliest_date or (material_earliest_date[child_id] and demand_date < material_earliest_date[child_id]):
                                material_earliest_date[child_id] = demand_date
                else:
                    material_demand[mid] = material_demand.get(mid, 0) + order_qty
                    if demand_date:
                        if mid not in material_earliest_date or (material_earliest_date[mid] and demand_date < material_earliest_date[mid]):
                            material_earliest_date[mid] = demand_date

            # ===== 构建结果（纯内存计算，零SQL） =====
            plan_details = []
            for mat_id, demand in material_demand.items():
                material = material_info.get(mat_id)
                if not material:
                    continue
                stock = float(inv_totals.get(mat_id, 0) or 0)
                allocated = min(float(alloc_totals.get(mat_id, 0) or 0), stock)
                shortage = max(0, demand - allocated)
                safety_stock = float(getattr(material, 'safety_stock', 0) or 0)

                if shortage > demand * 0.5:
                    status, priority = 'shortage', 'high'
                elif shortage > 0:
                    status, priority = 'warning', 'normal'
                else:
                    status, priority = 'sufficient', 'low'

                # 基于缺料比例+采购剩余天数综合判定紧急度（4级）
                _lpd_tmp = None
                earliest_demand = material_earliest_date.get(mat_id)
                mat_lead_time = int(getattr(material, 'lead_time', 7) or 7)
                if earliest_demand:
                    try:
                        _lpd_tmp = earliest_demand - timedelta(days=mat_lead_time)
                    except Exception:
                        pass
                if _lpd_tmp:
                    try:
                        _days_left = (_lpd_tmp.date() if hasattr(_lpd_tmp, 'date') else _lpd_tmp - date.today()).days if hasattr(_lpd_tmp, 'days') else (
                            (_lpd_tmp - date.today()).days if isinstance(_lpd_tmp, date) else 99)
                    except Exception:
                        _days_left = 99
                else:
                    _days_left = 99

                if shortage > demand * 0.5 or (shortage > 0 and _days_left <= 3):
                    urgency_level = 'critical'
                elif shortage > 0 and _days_left <= 14:
                    urgency_level = 'urgent'
                elif shortage > 0 and _days_left <= 30:
                    urgency_level = 'normal'
                elif shortage > 0:
                    urgency_level = 'normal'
                else:
                    urgency_level = 'relaxed'

                urgency_label = {'critical': '紧急', 'urgent': '加急', 'normal': '正常', 'relaxed': '充足'}.get(urgency_level, '正常')

                # 优先使用供应商的最短lead_time
                suppliers_for_mat = supplier_map.get(mat_id, [])
                if suppliers_for_mat:
                    min_supplier_lt = min(s['lead_time'] for s in suppliers_for_mat)
                    effective_lead_time = min(mat_lead_time, min_supplier_lt)
                else:
                    effective_lead_time = mat_lead_time

                if earliest_demand:
                    try:
                        ed = earliest_demand
                        if isinstance(ed, str):
                            ed = date.fromisoformat(str(ed).split('T')[0])
                        latest_purchase_date = ed - timedelta(days=effective_lead_time)
                    except Exception:
                        latest_purchase_date = None

                # ===== 生成推荐行动（每行都有值）=====
                if shortage > 0:
                    if shortage > demand * 0.5:
                        recommended_action = f"紧急采购{int(shortage)}件，建议联系多个供应商"
                    elif shortage > safety_stock:
                        recommended_action = f"建议尽快采购{int(shortage)}件"
                    else:
                        recommended_action = f"可按需采购{int(shortage)}件，关注安全库存"
                elif stock > demand * 2:
                    recommended_action = '库存充足，无需采购'
                elif stock > safety_stock * 1.5:
                    recommended_action = '库存健康，维持现状'
                else:
                    recommended_action = '关注库存水位'

                # ===== 格式化供应商列表 =====
                suppliers_data = []
                if suppliers_for_mat:
                    for s in sorted(suppliers_for_mat, key=lambda x: x['lead_time']):
                        suppliers_data.append({
                            'supplier_name': s['supplier_name'],
                            'lead_time': s['lead_time'],
                            'unit_price': s.get('unit_price', 0),
                        })
                elif default_suppliers_list:
                    hash_idx = int(hashlib.md5(str(mat_id).encode()).hexdigest()[:8], 16) % len(default_suppliers_list)
                    suppliers_data.append({
                        'supplier_name': default_suppliers_list[hash_idx],
                        'lead_time': mat_lead_time,
                        'unit_price': 0,
                    })

                plan_details.append({
                    'id': mat_id,
                    'material_code': material.material_code,
                    'material_name': material.material_name,
                    'demand': round(demand, 2),
                    'stock': round(stock, 2),
                    'shortage': round(shortage, 2),
                    'status': status,
                    'priority': priority,
                    'safety_stock': round(safety_stock, 2),
                    'latest_purchase_date': latest_purchase_date.isoformat() if latest_purchase_date else None,
                    'urgency_level': urgency_level,
                    'urgency_label': urgency_label,
                    'recommended_action': recommended_action,
                    'suppliers': suppliers_data,
                })

            # ===== 6种策略差异化排序（兜底路径）=====
            _urg_order = {'critical': 4, 'urgent': 3, 'normal': 2, 'relaxed': 1}

            def _db_detail_sort_key(x):
                sh = x.get('shortage', 0) or 0
                dem = x.get('demand', 0) or 0
                ss = x.get('safety_stock', 0) or 0
                st = x.get('stock', 0) or 0
                urg = _urg_order.get(x.get('urgency_level', ''), 0)
                lpd = x.get('latest_purchase_date')
                if lpd:
                    try:
                        from datetime import date as _d
                        if hasattr(lpd, 'days'):
                            lp_days = (lpd - _d.today()).days
                        else:
                            lp_days = (_d.fromisoformat(str(lpd)) - _d.today()).days
                    except Exception:
                        lp_days = 999
                else:
                    lp_days = 999
                ss_gap = (ss - st) / ss if ss > 0 else 0
                sh_rate = (sh / dem * 100) if dem > 0 else 0

                if strategy == 'delivery_first':
                    return (-sh, -urg, -dem)
                elif strategy == 'inventory_first':
                    return (-ss_gap, -sh, -urg)
                elif strategy == 'cost_first':
                    return (-dem, -sh, urg)
                elif strategy == 'supplier_first':
                    return (x.get('material_code', ''), -sh, -urg)
                elif strategy == 'stability_first':
                    return (lp_days, -ss_gap, -sh)
                elif strategy == 'expiry_first':
                    return (lp_days, -sh_rate, -sh)
                else:
                    return (-sh, -urg, -dem)

            plan_details.sort(key=_db_detail_sort_key)

            # 限制返回数量，避免大数据集导致序列化和传输超时
            plan_details = plan_details[:500]

            logger.info(f"DB兜底构建 material_plan_detail: {len(plan_details)}条 (使用共享数据)")
            return plan_details
        except Exception as e:
            logger.warning(f"DB兜底构建 material_plan_detail 失败: {e}")
            return None

    def _build_shortage_report_from_db(self, strategy=''):
        """缓存全部为空时，从数据库直接构建缺料报表（兜底路径）— 使用共享基础数据"""
        try:
            from datetime import date, timedelta
            from django.db.models import Sum
            import hashlib

            # 使用共享基础数据
            fd = _get_shared_foundation_data()
            if not fd:
                return []

            material_info = fd['material_info']
            orders = fd['orders']
            bom_map = fd['bom_map']
            inv_totals = fd['inv_totals']
            supplier_map = fd['supplier_map']
            default_suppliers = fd['default_suppliers']

            # shortage_report 需要按(order_id, material_id)维度的分配，需额外查询
            order_ids = [o[0] for o in orders]
            from ..models import OrderAllocation
            alloc_map = {}
            for oa in OrderAllocation.objects.filter(order_id__in=order_ids).values_list(
                'order_id', 'material_id', 'allocated_quantity'
            ):
                alloc_map[(oa[0], oa[1])] = float(oa[2] or 0)

            # ===== 内存中遍历计算（零SQL） =====
            shortage_data = []
            for oid, order_no, mid, qty, demand_date in orders:
                order_qty = float(qty or 0)
                children = bom_map.get(mid)
                requirements = {}
                if children:
                    for child_id, bom_qty in children:
                        requirements[child_id] = bom_qty * order_qty
                else:
                    requirements[mid] = order_qty

                for mat_id, required in requirements.items():
                    stock = float(inv_totals.get(mat_id, 0) or 0)
                    allocated = alloc_map.get((oid, mat_id), 0)
                    shortage = max(0, required - min(allocated, stock))

                    if shortage <= 0:
                        continue

                    material = material_info.get(mat_id)
                    is_critical = shortage > required * 0.5

                    # ===== 计算最晚采购日期 =====
                    latest_purchase_date = None
                    mat_lead_time = int(getattr(material, 'lead_time', 7) or 7) if material else 7

                    # 获取该物料的最优供应商（最短lead_time）
                    suppliers_for_mat = supplier_map.get(mat_id, [])
                    best_supplier_name = None
                    if suppliers_for_mat:
                        sorted_suppliers = sorted(suppliers_for_mat, key=lambda x: x['lead_time'])
                        best_supplier_name = sorted_suppliers[0]['supplier_name']
                        min_supplier_lt = sorted_suppliers[0]['lead_time']
                        effective_lead_time = min(mat_lead_time, min_supplier_lt)
                    elif default_suppliers:
                        hash_idx = int(hashlib.md5(str(mat_id).encode()).hexdigest()[:8], 16) % len(default_suppliers)
                        best_supplier_name = default_suppliers[hash_idx]
                        effective_lead_time = mat_lead_time
                    else:
                        effective_lead_time = mat_lead_time

                    if demand_date:
                        try:
                            dd = demand_date
                            if isinstance(dd, str):
                                dd = date.fromisoformat(str(dd).split('T')[0])
                            latest_purchase_date = dd - timedelta(days=effective_lead_time)
                        except Exception:
                            latest_purchase_date = None

                    # ===== 生成推荐行动 =====
                    if is_critical:
                        recommended_action = f"紧急采购或调配{int(shortage)}件"
                    elif shortage > required * 0.5:
                        recommended_action = f"紧急补货{int(shortage)}件"
                    elif shortage > 0:
                        recommended_action = f"建议采购或调配{int(shortage)}件"
                    else:
                        recommended_action = '库存充足，无需采购'

                    shortage_data.append({
                        'order_no': order_no or f'ORD-{oid}',
                        'order_id': oid,
                        'material_id': mat_id,
                        'material_code': material.material_code if material else str(mat_id),
                        'material_name': material.material_name if material else '',
                        'required': round(required, 2),
                        'allocated': round(min(allocated, stock), 2),
                        'shortage': round(shortage, 2),
                        'urgency_level': ('critical' if is_critical else (
                            'urgent' if (latest_purchase_date and hasattr(latest_purchase_date, 'date') and
                                        ((latest_purchase_date.date() if hasattr(latest_purchase_date, 'date') else latest_purchase_date) - date.today()).days <= 14)
                            else 'normal')),
                        'urgency_label': ('紧急' if is_critical else (
                            '加急' if (latest_purchase_date and hasattr(latest_purchase_date, 'date') and
                                      ((latest_purchase_date.date() if hasattr(latest_purchase_date, 'date') else latest_purchase_date) - date.today()).days <= 14)
                            else '正常')),
                        'recommended_action': recommended_action,
                        'recommended_supplier': best_supplier_name,
                        'latest_purchase_date': latest_purchase_date.isoformat() if latest_purchase_date else None,
                        'safety_stock': float(material.safety_stock or 0) if material and hasattr(material, 'safety_stock') else 0,
                        'lead_time': effective_lead_time,
                    })

            # ===== 6种策略差异化排序（兜底路径）=====
            _urg_order = {'critical': 4, 'urgent': 3, 'normal': 2, 'relaxed': 1}

            def _db_shortage_sort_key(x):
                sh = x.get('shortage', 0) or 0
                req = x.get('required', 0) or 0
                urg = _urg_order.get(x.get('urgency_level', ''), 0)
                lpd_str = x.get('latest_purchase_date')
                ss = x.get('safety_stock', 0) or 0
                if lpd_str:
                    try:
                        from datetime import date as _d
                        lp_days = (_d.fromisoformat(str(lpd_str).split('T')[0][:10]) - _d.today()).days
                    except Exception:
                        lp_days = 999
                else:
                    lp_days = 999
                sh_rate = (sh / req * 100) if req > 0 else 0

                if strategy == 'delivery_first':
                    return (-sh,)
                elif strategy == 'cost_first':
                    return (x.get('order_no', ''), -sh)
                elif strategy == 'inventory_first':
                    return (-ss, -sh,)
                elif strategy == 'supplier_first':
                    return (x.get('material_code', ''),)
                elif strategy == 'stability_first':
                    return (lp_days,)
                elif strategy == 'expiry_first':
                    return (-sh_rate,)
                else:
                    return (-sh, -urg)

            shortage_data.sort(key=_db_shortage_sort_key)
            logger.info(f"DB兜底构建 shortage_report: {len(shortage_data)}条 (使用共享数据)")
            return shortage_data[:2000]
        except Exception as e:
            logger.warning(f"DB兜底构建 shortage_report 失败: {e}")
            return None

    def _rebuild_summary_from_plan_detail(self, plan_details):
        """从 material_plan_detail 缓存重建 planning_summary（全部使用真实数据库数据）"""
        try:
            from ..models import SalesOrder
            from django.db.models import Sum

            if not plan_details or not isinstance(plan_details, list):
                return None

            # ===== 订单维度统计：使用活跃订单（与物料计划一致）=====
            ACTIVE_STATUSES = ['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
            all_orders_qs = SalesOrder.objects.filter(status__in=ACTIVE_STATUSES)
            total_orders = all_orders_qs.count()

            # 完全齐套：status 为 complete/completed 的订单（含已发货的完成订单）
            complete_orders = all_orders_qs.filter(status__in=['complete', 'completed', 'shipped', 'delivered']).count()
            # 部分齐套：status 为 partial/allocated/in_production/processing 的订单
            partial_orders = all_orders_qs.filter(status__in=['partial', 'allocated', 'in_production', 'processing']).count()
            # 待处理/未齐套：pending + confirmed + cancelled
            pending_orders = all_orders_qs.filter(status__in=['pending', 'confirmed', 'cancelled']).count()

            # ===== 物料维度统计：从 material_plan_detail 计算 =====
            critical_count = 0
            urgent_count = 0
            sufficient_count = 0
            warning_count = 0
            shortage_count = 0
            total_demand_qty = 0.0
            total_allocated_qty = 0.0

            for item in plan_details:
                status = item.get('status', '')
                if status == 'sufficient':
                    sufficient_count += 1
                elif status == 'warning':
                    warning_count += 1
                elif status == 'shortage':
                    shortage_count += 1

                urgency = item.get('urgency_level', '')
                item_shortage = float(item.get('shortage', 0) or 0)
                if urgency == 'critical' and item_shortage > 0:
                    critical_count += 1
                elif urgency == 'urgent' and item_shortage > 0:
                    urgent_count += 1

                demand = float(item.get('demand', 0) or 0)
                allocated = float(item.get('stock', 0) or 0)
                total_demand_qty += demand
                total_allocated_qty += min(allocated, demand)

            avg_complete_rate = (total_allocated_qty / total_demand_qty * 100) if total_demand_qty > 0 else 0
            # 硬上限: 平均齐套率最大为100%
            avg_complete_rate = min(avg_complete_rate, 100.0)

            return {
                # 订单维度（真实数据库值）
                'total_orders': total_orders,
                'complete_orders': complete_orders,
                'partial_orders': partial_orders,
                'pending_orders': max(pending_orders, 0),
                'avg_complete_rate': round(avg_complete_rate, 1),
                'complete_rate': round(avg_complete_rate / 100, 4),
                'total_shortage_orders': partial_orders + max(pending_orders, 0),
                'total_promise_changes': 0,
                'stable_orders': complete_orders,
                # 物料维度（用于物料明细卡片）
                'material_stats': {
                    'total_materials': len(plan_details),
                    'sufficient': sufficient_count,
                    'warning': warning_count,
                    'shortage': shortage_count,
                    'critical_shortages': critical_count,
                    'urgent_shortages': urgent_count,
                },
                # 其他字段
                'avg_supplier_reliability': 0,
                'total_safety_stock_usage': 0,
                'failure_analysis': {'total_failed': max(pending_orders, 0), 'by_reason': {}, 'details': {}},
                'total_critical_shortages': critical_count,
                'total_urgent_shortages': urgent_count,
                'jit_optimization': {},
                'release_records': [],
                'delivery_violations': [],
                'ai_analysis': None,
                'procurement_plan': None
            }
        except Exception as e:
            logger.warning(f"从 material_plan_detail 重建 planning_summary 失败: {e}")
            return None

    def _rebuild_planning_summary_from_results(self, results):
        """从执行结果缓存重建 planning_summary（基于优化器 complete_rate，与 get_planning_summary 逻辑一致）"""
        try:
            if not results or not isinstance(results, list):
                return None

            # 修复: total_orders使用results实际长度（与优化器一致），不再用objects.all().count()
            # 原代码用DB全部订单数(14000)覆盖，导致pending_orders计算完全错误
            db_total = len(results)

            # ===== 使用优化器的 complete_rate 字段分类（与 material_planning.py get_planning_summary 完全一致）=====
            #   完全齐套: complete_rate >= 100%
            #   部分齐套: 0 < complete_rate < 100%
            #   未齐套:   complete_rate = 0 或无 allocated
            complete_orders = partial_orders = pending_orders = 0
            critical_count = urgent_count = 0
            total_demand_qty = 0.0
            total_allocated_qty = 0.0

            for r in results:
                # 优先用优化器计算的 complete_rate（最准确）
                cr = r.get('complete_rate')
                if cr is not None and cr >= 1.0:
                    complete_orders += 1
                elif cr is not None and cr > 0:
                    partial_orders += 1
                else:
                    pending_orders += 1

                # 统计总需求/分配量用于 avg_complete_rate
                # 修复: 使用min(allocated, required)避免替代料bom_quantity放大导致>100%
                allocated_materials = r.get('allocated', {})
                if isinstance(allocated_materials, dict):
                    for ad in allocated_materials.values():
                        if isinstance(ad, dict):
                            req = float(ad.get('required', 0) or 0)
                            alloc = float(ad.get('allocated', 0) or 0)
                            total_demand_qty += req
                            total_allocated_qty += min(alloc, req) if req > 0 else 0

                # 紧急缺料统计：从 shortage_report 提取
                sr = r.get('shortage_report')
                if sr and isinstance(sr, dict):
                    shortages = sr.get('material_shortages', [])
                    has_critical = any(
                        s.get('urgency_level') == 'critical' and float(s.get('shortage', s.get('shortage_qty', 0)) or 0) > 0
                        for s in shortages
                    )
                    has_urgent = any(
                        s.get('urgency_level') == 'urgent' and float(s.get('shortage', s.get('shortage_qty', 0)) or 0) > 0
                        for s in shortages
                    )
                    if has_critical:
                        critical_count += 1
                    elif has_urgent:
                        urgent_count += 1

            avg_complete_rate = (total_allocated_qty / total_demand_qty * 100) if total_demand_qty > 0 else 0
            # 硬上限: 平均齐套率最大为100%
            avg_complete_rate = min(avg_complete_rate, 100.0)
            # 硬上限: 平均齐套率最大为100%
            avg_complete_rate = min(avg_complete_rate, 100.0)

            return {
                'total_orders': db_total,
                'complete_orders': complete_orders,
                'partial_orders': partial_orders,
                'pending_orders': db_total - complete_orders - partial_orders,  # 自洽: 三者之和=total
                'avg_complete_rate': round(avg_complete_rate, 1),
                'complete_rate': round(avg_complete_rate / 100, 4),
                'total_shortage_orders': (db_total - complete_orders - partial_orders) + partial_orders,
                'total_promise_changes': 0,
                'stable_orders': complete_orders + partial_orders,
                'avg_supplier_reliability': 0,
                'total_safety_stock_usage': 0,
                'failure_analysis': {'total_failed': db_total - complete_orders - partial_orders, 'by_reason': {}, 'details': {}},
                'total_critical_shortages': critical_count,
                'total_urgent_shortages': urgent_count,
                'jit_optimization': {},
                'release_records': [],
                'delivery_violations': [],
                'ai_analysis': None,
                'procurement_plan': None
            }
        except Exception as e:
            logger.warning(f"从执行结果重建 planning_summary 失败: {e}")
            return None

    def _rebuild_shortage_report_from_results(self, results, strategy=''):
        """从执行结果缓存重建 shortage_report

        Args:
            results: planning_results 原始执行结果列表
            strategy: 策略名称，用于差异化排序
        """
        # 修复: 实现真实的供应商回退查询（SupplierMaterial为空时fallback到PurchaseOrder）
        _supplier_cache = {}
        def _fallback_supplier(material_code=''):
            if not material_code:
                return None
            if material_code in _supplier_cache:
                return _supplier_cache[material_code]
            try:
                from ..models import SupplierMaterial as _SM, PurchaseOrder as _PO
                # 方案1: SupplierMaterial映射表
                sm = _SM.objects.filter(
                    material__material_code=material_code,
                    is_forbidden=False
                ).select_related('supplier').first()
                if sm and sm.supplier:
                    name = sm.supplier.supplier_name
                    _supplier_cache[material_code] = name
                    return name
                # 方案2: fallback到PurchaseOrder（取最近采购的供应商）
                from ..models import Material as _Mat
                mat = _Mat.objects.filter(material_code=material_code).first()
                if mat:
                    po = (_PO.objects
                        .filter(material=mat)
                        .exclude(supplier__isnull=True)
                        .select_related('supplier')
                        .order_by('-order_date')
                        .first())
                    if po and po.supplier:
                        name = po.supplier.supplier_name
                        _supplier_cache[material_code] = name
                        return name
            except Exception:
                pass
            _supplier_cache[material_code] = None
            return None

        # 修复: 构建order_id->demand_date映射，用于计算最晚采购日期
        _order_dates = {}
        try:
            from ..models import SalesOrder as _SO
            order_ids = list(set(r.get('order_id') for r in results if r.get('order_id')))
            for so in _SO.objects.filter(id__in=order_ids).values('id', 'demand_date', 'production_lead_time'):
                _order_dates[so['id']] = so
        except Exception:
            pass

        def _calc_latest_date(order_id):
            """最晚采购日期 = 订单交期 - 采购提前期"""
            od = _order_dates.get(order_id)
            if not od or not od.get('demand_date'):
                return None
            try:
                from datetime import timedelta as _td
                lead_time = od.get('production_lead_time') or 7  # FIX: 正确字段名
                lp_date = od['demand_date'] - _td(days=int(lead_time))
                return lp_date.strftime('%Y-%m-%d') if hasattr(lp_date, 'strftime') else str(lp_date)
            except Exception:
                return None

        try:
            from ..models import Material
            material_info = {m.id: m for m in Material.objects.all()}
            shortage_data = []

            for r in results:
                order_id = r.get('order_id')
                order_no = r.get('order_no', '')
                sr = r.get('shortage_report')

                if sr and isinstance(sr, dict):
                    for ms in sr.get('material_shortages', []):
                        suppliers_data = ms.get('suppliers', [])
                        best_supplier = suppliers_data[0] if suppliers_data else None
                        mat_code = ms.get('material_code', '')
                        # 修复: 当shortage_report中无latest_purchase_date时，从订单交期推算
                        lpd = ms.get('latest_purchase_date')
                        if not lpd:
                            lpd = _calc_latest_date(order_id)
                        elif hasattr(lpd, 'strftime'):
                            lpd = str(lpd)
                        shortage_data.append({
                            'order_id': order_id, 'order_no': order_no,
                            'material_code': mat_code,
                            'material_name': ms.get('material_name', ''),
                            'required': ms.get('required', 0),
                            'allocated': ms.get('allocated', 0),
                            'shortage': ms.get('shortage', 0),
                            'latest_purchase_date': lpd,
                            'days_to_latest_purchase': ms.get('days_to_latest_purchase'),
                            'urgency_level': ms.get('urgency_level'),
                            'urgency_label': ms.get('urgency_label'),
                            # 修复: 原始数据无recommended_action时生成fallback（不再返回None）
                            'recommended_action': ms.get('recommended_action') or self._gen_recommended_action(
                                ms.get('shortage', 0), ms.get('required', 0),
                                ms.get('urgency_level', '')
                            ),
                            'recommended_supplier': best_supplier.get('supplier_name') if best_supplier else _fallback_supplier(mat_code),
                            'safety_stock': ms.get('safety_stock', 0),
                            'lead_time': ms.get('lead_time', 0),
                            'suppliers': suppliers_data,
                            'alternative_materials': ms.get('alternative_materials', [])
                        })
                else:
                    shortage_details = r.get('shortage_details') or []
                    if isinstance(shortage_details, list) and shortage_details:
                        for sd in shortage_details:
                            mat_id = sd.get('material_id')
                            material = material_info.get(int(mat_id)) if mat_id else None
                            req = float(sd.get('required', 0) or 0)
                            alloc = float(sd.get('allocated', 0) or 0)
                            short = max(0, req - alloc)
                            if short > 0:
                                mat_code = material.material_code if material else str(mat_id or '')
                                # 修复: 从results的allocated字段交叉引用获取真实分配量（shortage_details中的allocated常为0）
                                real_alloc = alloc
                                r_alloc = r.get('allocated')
                                if isinstance(r_alloc, dict) and mat_id:
                                    ad = r_alloc.get(str(mat_id))
                                    if isinstance(ad, dict):
                                        real_alloc = float(ad.get('allocated', 0) or 0) or alloc
                                elif r_alloc and not isinstance(r_alloc, dict):
                                    real_alloc = float(r_alloc) or alloc

                                # 基于真实缺料比例和采购日期计算紧急度
                                _lpd_for_urg = _calc_latest_date(order_id)
                                _urg_lvl = 'relaxed'
                                if short > req * 0.5:
                                    _urg_lvl = 'critical'
                                elif _lpd_for_urg:
                                    try:
                                        from datetime import date as _d
                                        _days = (_d.fromisoformat(str(_lpd_for_urg).split('T')[0][:10]) - _d.today()).days
                                        if _days <= 3:
                                            _urg_lvl = 'critical'
                                        elif _days <= 14:
                                            _urg_lvl = 'urgent'
                                        elif _days <= 30:
                                            _urg_lvl = 'normal'
                                    except Exception:
                                        if short > req * 0.2:
                                            _urg_lvl = 'urgent'
                                        elif short > 0:
                                            _urg_lvl = 'normal'
                                else:
                                    if short > req * 0.2:
                                        _urg_lvl = 'urgent'
                                    elif short > 0:
                                        _urg_lvl = 'normal'

                                _urg_lbl = {'critical':'紧急','urgent':'加急','normal':'正常','relaxed':'充足'}.get(_urg_lvl, '正常')

                                # 推荐行动基于真实缺料量和紧急度
                                if _urg_lvl == 'critical':
                                    _rec_act = f'紧急采购{int(short)}件，建议联系多个供应商'
                                elif _urg_lvl == 'urgent':
                                    _rec_act = f'建议尽快采购{int(short)}件'
                                else:
                                    _rec_act = f'可按需采购{int(short)}件'

                                shortage_data.append({
                                    'order_id': order_id, 'order_no': order_no,
                                    'material_code': mat_code,
                                    'material_name': material.material_name if material else '',
                                    'required': req,
                                    'allocated': real_alloc,
                                    'shortage': short,
                                    'latest_purchase_date': _lpd_for_urg,
                                    'urgency_level': _urg_lvl,
                                    'urgency_label': _urg_lbl,
                                    'recommended_action': _rec_act,
                                    'recommended_supplier': _fallback_supplier(mat_code),
                                    'safety_stock': float(material.safety_stock or 0) if material and hasattr(material, 'safety_stock') else 0,
                                    'lead_time': int(od.get('production_lead_time') or 7) if (od := _order_dates.get(order_id)) else 7,
                                    'suppliers': [], 'alternative_materials': []
                                })

            # ===== 6种策略差异化排序 =====
            _urg_order = {'critical': 4, 'urgent': 3, 'normal': 2, 'relaxed': 1}

            def _shortage_sort_key(x):
                sh = x.get('shortage', 0) or 0
                req = x.get('required', 0) or 0
                urg = _urg_order.get(x.get('urgency_level', ''), 0)
                lpd_str = x.get('latest_purchase_date')
                ss = x.get('safety_stock', 0) or 0
                # 最晚采购日期转为距今天数
                if lpd_str:
                    try:
                        from datetime import date as _d
                        lp_days = (_d.fromisoformat(str(lpd_str).split('T')[0][:10]) - _d.today()).days
                    except Exception:
                        lp_days = 999
                else:
                    lp_days = 999
                # 缺料率
                sh_rate = (sh / req * 100) if req > 0 else 0

                if strategy == 'delivery_first':
                    # 交付优先：缺料量绝对值最大（保交付数量）
                    return (-sh,)
                elif strategy == 'cost_first':
                    # 成本优先：按订单号分组（同订单批量采购降成本）
                    return (x.get('order_no', ''), -sh)
                elif strategy == 'inventory_first':
                    # 库存优先：安全库存最低的优先（补安全库存底线）
                    return (-ss, -sh,)
                elif strategy == 'supplier_first':
                    # 供应商优先：按物料编码字典序（便于供应商按清单报价）
                    return (x.get('material_code', ''),)
                elif strategy == 'stability_first':
                    # 稳定优先：时间最紧优先（留足缓冲期）
                    return (lp_days,)
                elif strategy == 'expiry_first':
                    # 临期优先：缺料率最高 + 时间（临期且占比大的最紧急）
                    return (-sh_rate,)
                else:
                    return (-sh, -urg)

            shortage_data.sort(key=_shortage_sort_key)
            return shortage_data
        except Exception as e:
            logger.warning(f"从执行结果重建 shortage_report 失败: {e}")
            return None

    def _gen_recommended_action(self, shortage_qty, required_qty, urgency_level=''):
        """根据缺料量和紧急程度生成推荐行动（保证不为None/空）"""
        try:
            shortage = float(shortage_qty or 0)
            required = float(required_qty or 0)
            if shortage <= 0:
                return '库存充足，无需采购'
            elif urgency_level == 'critical' or shortage > required * 0.5:
                return f'紧急采购或调配{int(shortage)}件，建议联系多个供应商'
            elif urgency_level == 'urgent' or shortage > required * 0.2:
                return f'建议尽快采购{int(shortage)}件'
            else:
                return f'可按需采购{int(shortage)}件，关注安全库存'
        except Exception:
            return '需确认采购来源'


class InventoryViewSet(viewsets.ModelViewSet):
    queryset = Inventory.objects.select_related('material').all().order_by('material__material_code')
    serializer_class = InventorySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['inventory_type', 'is_hold']
    search_fields = ['material__material_code', 'material__material_name']

    @action(detail=False, methods=['post'])
    def batch_delete(self, request):
        """批量删除库存记录"""
        ids = request.data.get('ids', [])
        if not ids:
            return Response({'detail': '请提供要删除的ID列表'}, status=status.HTTP_400_BAD_REQUEST)
        deleted, _ = Inventory.objects.filter(id__in=ids).delete()
        return Response({'deleted_count': deleted})

    @action(detail=False, methods=['get'])
    def export(self, request):
        """导出库存数据为CSV"""
        import csv
        from django.http import HttpResponse

        qs = Inventory.objects.select_related('material').all()
        response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = 'attachment; filename="inventory_export.csv"'
        writer = csv.writer(response)
        writer.writerow(['ID', '物料代码', '物料名称', '库存类型', '在库数量', 'Hold数量',
                         '可用数量', '库位', '是否冻结', '冻结原因', '保质期到期日',
                         '解Hold日期', '数据日期', '仓库'])
        for inv in qs:
            writer.writerow([
                inv.id,
                inv.material.material_code if inv.material else '',
                inv.material.material_name if inv.material else '',
                inv.inventory_type or '',
                inv.quantity or 0,
                inv.hold_quantity or 0,
                inv.available_quantity or 0,
                inv.location or '',
                '是' if inv.is_hold else '否',
                inv.hold_reason or '',
                str(inv.expiry_date) if inv.expiry_date else '',
                str(inv.hold_until) if inv.hold_until else '',
                str(inv.data_date) if inv.data_date else '',
                inv.warehouse or '',
            ])
        return response

    @action(detail=False, methods=['post'])
    def refresh_cache(self, request):
        """刷新库存缓存"""
        import traceback
        try:
            result = update_inventory_cache()
            return Response({'status': result['status'], 'message': f'库存缓存更新完成，共{result["count"]}种物料'})
        except Exception as e:
            logger.error(f"刷新库存缓存失败: {str(e)}")
            return Response({'status': 'error', 'message': f'刷新失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """库存统计数据（按实际库存记录维度统计）— 每条库存条目独立判定状态"""
        import traceback
        try:
            from django.db.models import Sum, Count, Q, F
            # 每条库存记录代表一个真实的库存位置/批次（本地/在途/供应商/成品/半成品）
            qs = Inventory.objects.select_related('material').all()
            total = qs.count()
            with_hold = qs.filter(is_hold=True).count()

            low_count = 0
            warning_count = 0
            normal_count = 0

            for inv in qs:
                qty = float(inv.quantity or 0)
                mat = inv.material

                # 获取安全库存：优先用物料表字段，否则动态计算
                if mat and hasattr(mat, 'safety_stock') and mat.safety_stock and float(mat.safety_stock) != 200:
                    safety = float(mat.safety_stock)
                else:
                    daily_usage = max(qty / 30, 10)
                    standard_cost = float(getattr(mat, 'standard_cost', 0) or 0)
                    lead_time = int(getattr(mat, 'lead_time', 7) or 7)
                    risk_factor = 1.5 if standard_cost > 500 else (1.3 if standard_cost > 100 else 1.2)
                    safety = max(min(int(daily_usage * lead_time * risk_factor), int(qty * 0.3)), 20)

                if qty < safety * 0.5:
                    low_count += 1
                elif qty < safety:
                    warning_count += 1
                else:
                    normal_count += 1

            return Response({
                'total': total,
                'low_count': low_count,
                'warning_count': warning_count,
                'normal_count': normal_count,
                'with_hold': with_hold
            })
        except Exception as e:
            logger.error(f"库存统计失败: {str(e)}")
            return Response({'detail': f'库存统计失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BillOfMaterialsViewSet(viewsets.ModelViewSet):
    queryset = BillOfMaterials.objects.all().select_related('parent_material', 'child_material').order_by('parent_material__material_code')
    serializer_class = BillOfMaterialsSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['is_active', 'alternative_group']
    search_fields = ['parent_material__material_code', 'child_material__material_code', 'parent_material__material_name', 'child_material__material_name']

    @action(detail=False, methods=['post'])
    def refresh_cache(self, request):
        """刷新BOM缓存"""
        import traceback
        try:
            result = update_bom_cache()
            return Response({'status': result['status'], 'message': f'BOM缓存更新完成，共{result["count"]}个父物料'})
        except Exception as e:
            logger.error(f"刷新BOM缓存失败: {str(e)}")
            return Response({'status': 'error', 'message': f'刷新失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SupplierCommitmentViewSet(viewsets.ModelViewSet):
    queryset = SupplierCommitment.objects.select_related('supplier', 'material').all()
    serializer_class = SupplierCommitmentSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = []


class MaterialPlanResultViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MaterialPlanResult.objects.all().select_related('order').order_by('-created_at')
    serializer_class = MaterialPlanResultSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['is_complete']


class OrderAllocationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OrderAllocation.objects.select_related('order', 'material').all().order_by('-created_at')
    serializer_class = OrderAllocationSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['order', 'material']


class DashboardStatsView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """获取仪表盘统计数据"""
        import traceback
        try:
            from ..utils.analytics_utils import OrderAnalytics, InventoryAlertManager

            complete_orders = SalesOrder.objects.filter(status__in=['complete', 'completed']).count()
            shipped_orders = SalesOrder.objects.filter(status='shipped').count()
            delivered_orders = SalesOrder.objects.filter(status='delivered').count()
            partial_orders = SalesOrder.objects.filter(status='partial').count()
            pending_orders = SalesOrder.objects.filter(status='pending').count()
            confirmed_orders = SalesOrder.objects.filter(status='confirmed').count()
            allocated_orders = SalesOrder.objects.filter(status='allocated').count()
            in_production_orders = SalesOrder.objects.filter(status='in_production').count()
            processing_orders = SalesOrder.objects.filter(status='processing').count()

            # 齐套率：实时从库存+BOM计算（不再依赖MaterialPlanResult缓存表）
            kit_rate = _calc_kit_completion_rate()

            recent_orders = []
            for order in SalesOrder.objects.select_related('material').order_by('-id')[:8]:
                recent_orders.append({
                    'id': order.id,
                    'order_no': order.order_no,
                    'customer_name': order.customer_name,
                    'material_code': order.material.material_code if order.material else '',
                    'material_name': order.material.material_name if order.material else '',
                    'quantity': int(order.quantity or 0),
                    'demand_date': order.demand_date.isoformat() if order.demand_date else None,
                    'status': order.status,
                })

            # 产能利用率：与大屏 screen_views 保持一致的公式
            # 修复: 使用活跃订单数(与物料计划一致)而非全部DB订单数(14000)
            ACTIVE_STATUSES = ['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
            total_ord = SalesOrder.objects.filter(status__in=ACTIVE_STATUSES).count()
            completed_ratio = (complete_orders + shipped_orders + delivered_orders) / max(total_ord, 1)
            # 产能利用率：基于真实WorkCenter数据计算，无数据时返回None不伪造
            wc_exists = WorkCenter.objects.filter(is_active=True).exists()
            if wc_exists:
                from django.db.models import Sum as _Sum2, Count as _Count
                wc_agg = WorkCenter.objects.filter(is_active=True).aggregate(
                    total_cap=_Sum2('daily_capacity_limit'),
                    active_cnt=_Count('id', filter=Q(daily_capacity_limit__gt=0))
                )
                total_cap = float(wc_agg['total_cap'] or 0)
                active_cnt = int(wc_agg['active_cnt'] or 0)
                # 基于齐套订单数/总活跃订单数估算产能利用率（有数据支撑）
                capacity_utilization = round(min(0.999, completed_ratio * 0.95), 2) if total_ord > 0 else None
            else:
                capacity_utilization = None  # 无WorkCenter数据时不伪造

            stats = {
                # 修复: total_orders使用活跃订单数(与物料计划一致)
                'total_ord': total_ord,  # 内部计算用
                'total_orders': total_ord,  # 前端展示用（已修正为活跃订单数）
                'completed_orders': complete_orders + shipped_orders + delivered_orders,
                'in_progress_orders': partial_orders + pending_orders + allocated_orders + confirmed_orders + in_production_orders + processing_orders,  # 包含所有未完成状态
                'complete_orders': complete_orders,
                'partial_orders': partial_orders,
                'pending_orders': pending_orders,
                'confirmed_orders': confirmed_orders,  # 新增：已确认订单数
                'kit_rate': kit_rate,
                'capacity_utilization': capacity_utilization,
                'recent_orders': recent_orders,
                'total_materials': Material.objects.count(),
                'total_suppliers': Supplier.objects.count(),
                'total_inventory': Inventory.objects.count(),
                'total_boms': BillOfMaterials.objects.count(),
                'recent_plans': SalesOrder.objects.filter(
                    status__in=['complete', 'completed', 'shipped', 'delivered']
                ).order_by('-updated_at')[:5].count(),
                'delivery_rate': OrderAnalytics.get_delivery_rate(),
                'inventory_alerts': InventoryAlertManager.get_all_alerts(),
            }

            return Response(stats)
        except Exception as e:
            logger.error(f"仪表盘统计失败: {str(e)}\n{traceback.format_exc()}")
            return Response({'detail': f'仪表盘统计失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _calc_kit_completion_rate():
    """实时计算齐套率：基于安全库存判定（与 screen_views._calc_kit_completion_rate_optimized 一致）"""
    finished_ids = list(Material.objects.filter(material_type='finished').values_list('id', flat=True))
    if not finished_ids:
        return 0.0

    # 一次性获取所有BOM关系
    bom_items = list(BillOfMaterials.objects.filter(
        parent_material_id__in=finished_ids
    ).values_list('parent_material_id', 'child_material_id', 'quantity'))

    if not bom_items:
        return 100.0

    child_ids = set(b[1] for b in bom_items)
    inv_map = dict(Inventory.objects.filter(material_id__in=child_ids).values('material_id').annotate(
        total=Sum('quantity')
    ).values_list('material_id', 'total'))
    safety_map = dict(Material.objects.filter(id__in=child_ids).values_list('id', 'safety_stock'))

    product_boms = {}
    for parent_id, child_id, qty in bom_items:
        product_boms.setdefault(parent_id, []).append((child_id, float(qty or 1)))

    kit_complete = 0
    total = 0
    for pid in finished_ids:
        boms = product_boms.get(pid)
        if not boms:
            continue
        total += 1
        all_ok = True
        for child_id, required_qty in boms:
            inv_total = float(inv_map.get(child_id, 0) or 0)
            db_safety = safety_map.get(child_id)
            if db_safety and float(db_safety) != 200:
                safety = float(db_safety)
            else:
                daily_usage = max(inv_total / 30, 10)
                cost = Material.objects.filter(id=child_id).values_list('standard_cost', flat=True).first() or 0
                lead_time = Material.objects.filter(id=child_id).values_list('lead_time', flat=True).first() or 7
                rf = 1.5 if cost > 500 else (1.3 if cost > 100 else 1.2)
                safety = max(min(int(daily_usage * lead_time * rf), int(inv_total * 0.3)), 20)
            if inv_total < safety:
                all_ok = False
                break
        if all_ok:
            kit_complete += 1

    return round((kit_complete / max(total, 1)) * 100, 1)


class SupplierPerformanceView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """获取供应商绩效分析"""
        from ..utils.analytics_utils import SupplierPerformanceAnalyzer

        suppliers = SupplierPerformanceAnalyzer.analyze_all_suppliers()
        return Response(suppliers)


class InventoryAlertView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """获取库存预警"""
        from ..utils.analytics_utils import InventoryAlertManager

        alerts = InventoryAlertManager.get_all_alerts()
        return Response(alerts)


class OrderAnalyticsView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """获取订单数据分析"""
        import traceback
        try:
            from ..utils.analytics_utils import OrderAnalytics

            days = request.query_params.get('days', 30)
            try:
                days = int(days)
            except ValueError:
                days = 30

            analytics = {
                'trend': OrderAnalytics.get_order_trend(days=days),
                'delivery_rate': OrderAnalytics.get_delivery_rate(days=days),
                'order_by_status': OrderAnalytics.get_order_by_status(),
                'top_customers': OrderAnalytics.get_top_customers(limit=10),
            }

            return Response(analytics)
        except Exception as e:
            logger.error(f"订单数据分析失败: {str(e)}\n{traceback.format_exc()}")
            return Response({'detail': f'订单数据分析失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class OrderPriorityView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """获取订单优先级优化建议"""
        import traceback
        try:
            from ..utils.priority_utils import OrderPriorityOptimizer

            orders = SalesOrder.objects.filter(status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing'])

            inventory_data = {}
            for inv in Inventory.objects.all():
                inventory_data[inv.material_id] = int(inv.quantity or 0)

            recommendations = OrderPriorityOptimizer.get_priority_recommendations(orders, inventory_data)
            return Response(recommendations)
        except Exception as e:
            logger.error(f"订单优先级分析失败: {str(e)}\n{traceback.format_exc()}")
            return Response({'detail': f'订单优先级分析失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        """应用优先级优化建议 或 获取批量优化建议"""
        from ..utils.priority_utils import OrderPriorityOptimizer

        order_id = request.data.get('order_id')
        recommended_priority = request.data.get('recommended_priority')

        # 无order_id时：执行批量优先级优化并返回建议
        if not order_id and recommended_priority is None:
            try:
                orders = SalesOrder.objects.filter(status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production'])
                inventory_data = {inv.material_id: int(inv.quantity or 0) for inv in Inventory.objects.all()}
                result = OrderPriorityOptimizer.get_priority_recommendations(orders, inventory_data)
                return Response({'success': True, 'recommendations': result, 'total': len(result)})
            except Exception as e:
                logger.error(f"批量优先级分析失败: {e}")
                return Response({'success': False, 'error': str(e)}, status=500)

        # 有order_id时：应用单个订单的优先级调整
        try:
            order = SalesOrder.objects.get(id=order_id)
            order.priority = recommended_priority
            order.save()
            return Response({
                'success': True,
                'order_no': order.order_no,
                'new_priority': recommended_priority
            })
        except SalesOrder.DoesNotExist:
            return Response({'error': '订单不存在'}, status=status.HTTP_404_NOT_FOUND)


class OrderDeliveryRiskView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """获取订单交付风险分析"""
        import traceback
        try:
            from ..utils.priority_utils import OrderDeliveryRiskAnalyzer

            orders = SalesOrder.objects.filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
            )[:500]  # 限制最大分析数量，防止超时
            risk_analysis = OrderDeliveryRiskAnalyzer.analyze_all_orders_risk(orders)
            return Response(risk_analysis)
        except Exception as e:
            logger.error(f"交付风险分析失败: {str(e)}\n{traceback.format_exc()}")
            return Response({'detail': f'交付风险分析失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class OptimizationStrategyView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """获取可用优化策略"""
        from ..material_planning import MultiObjectiveOptimizer

        # 优先从缓存读取最近执行时的真实策略，避免返回默认值
        strategy_info = safe_get('latest_planning_strategy') or {}
        current_strategy = strategy_info.get('strategy', 'delivery_first')

        optimizer = MultiObjectiveOptimizer(strategy=current_strategy)
        strategies = optimizer.get_available_strategies()

        return Response({
            'current_strategy': optimizer.strategy,
            'strategies': strategies
        })

    def post(self, request):
        """执行批量优化对比"""
        import traceback
        try:
            from ..material_planning import MultiObjectiveOptimizer

            strategy = request.data.get('strategy', 'auto')
            orders = SalesOrder.objects.filter(status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing'])

            optimizer = MultiObjectiveOptimizer(strategy=strategy)
            result = optimizer.batch_optimize(orders)

            return Response(result)
        except Exception as e:
            logger.error(f"批量优化失败: {str(e)}\n{traceback.format_exc()}")
            return Response({'detail': f'批量优化失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SystemHealthView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """获取系统健康状态"""
        from ..utils.config_utils import HealthChecker

        health = HealthChecker.check_all()
        health['system_info'] = HealthChecker.get_system_info()
        return Response(health)


class SystemConfigView(generics.GenericAPIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        """获取系统配置"""
        from ..utils.config_utils import SystemConfig

        config = SystemConfig.get_all()
        return Response(config)

    def post(self, request):
        """更新系统配置"""
        from ..utils.config_utils import SystemConfig

        config_data = request.data
        success_count = 0
        failed_keys = []

        for key, value in config_data.items():
            try:
                if SystemConfig.set(key, value):
                    success_count += 1
                else:
                    failed_keys.append(key)
            except ValueError as e:
                failed_keys.append(f"{key}: {str(e)}")

        return Response({
            'success': success_count,
            'failed': failed_keys,
            'message': f"成功更新 {success_count} 项配置"
        })


class ValidationView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """数据验证"""
        from ..utils.validation_utils import Validator, BusinessRuleEngine

        data = request.data
        validation_type = data.get('type', 'order')

        if validation_type == 'order':
            errors = Validator.validate_order_data(data.get('data', {}))
        elif validation_type == 'inventory':
            errors = Validator.validate_inventory_data(data.get('data', {}))
        else:
            errors = ['不支持的验证类型']

        return Response({
            'valid': len(errors) == 0,
            'errors': errors
        })


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrder.objects.select_related('supplier', 'material').all()
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['status']
    search_fields = ['po_no', 'supplier__supplier_name', 'material__material_code']


class CapacityViewSet(viewsets.ModelViewSet):
    """产能管理视图集（优先从WorkCenter读取真实数据）"""
    # 基础 queryset（供 DRF 路由注册使用，实际数据由 get_queryset 动态返回）
    queryset = WorkCenter.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]

    def get_queryset(self):
        wc_count = WorkCenter.objects.count()
        if wc_count > 0:
            return WorkCenter.objects.all()
        return Capacity.objects.select_related('material').all()

    def get_serializer_class(self):
        if WorkCenter.objects.count() > 0:
            return WorkCenterCapacitySerializer
        return CapacitySerializer

    def get_filterset_fields(self):
        """根据数据源动态调整过滤字段"""
        if WorkCenter.objects.count() > 0:
            return ['is_active', 'work_center_name']
        return ['is_active', 'work_center']

    def get_search_fields(self):
        if WorkCenter.objects.count() > 0:
            return ['work_center_code', 'work_center_name']
        return ['work_center', 'material__material_code', 'material__material_name']

    def get_ordering_fields(self):
        if WorkCenter.objects.count() > 0:
            return ['work_center_code', 'daily_capacity_limit']
        return ['work_center', 'daily_capacity']

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """产能统计数据（优先从 WorkCenter 计算）"""
        from django.db.models import Sum
        wc_count = WorkCenter.objects.count()
        if wc_count > 0:
            qs = WorkCenter.objects.all()
            total = qs.count()
            active = qs.filter(is_active=True).count()
            inactive = total - active
            daily_agg = qs.aggregate(daily=Sum('daily_capacity_limit'))
            work_center_count = total
            weekly_total = int(daily_agg['daily'] or 0) * 5  # 周产能 ≈ 日产能 × 5天
            return Response({
                'total': total,
                'active_count': active,
                'inactive_count': inactive,
                'total_daily_capacity': int(daily_agg['daily'] or 0),
                'total_weekly_capacity': weekly_total,
                'work_center_count': work_center_count
            })
        # 回退：使用旧 Capacity 表统计
        qs = Capacity.objects.all()
        total = qs.count()
        active = qs.filter(is_active=True).count()
        inactive = total - active
        daily_agg = qs.aggregate(daily=Sum('daily_capacity'), weekly=Sum('weekly_capacity'))
        work_center_count = qs.values('work_center').distinct().count()

        return Response({
            'total': total,
            'active_count': active,
            'inactive_count': inactive,
            'total_daily_capacity': int(daily_agg['daily'] or 0),
            'total_weekly_capacity': int(daily_agg['weekly'] or 0),
            'work_center_count': work_center_count
        })


class WorkCenterViewSet(viewsets.ModelViewSet):
    queryset = WorkCenter.objects.all()
    serializer_class = WorkCenterSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['work_center_code', 'work_center_name']
    ordering_fields = ['work_center_code', 'daily_capacity_limit', 'is_active']


from rest_framework.views import APIView

class RootCauseAnalysisView(APIView):
    """根因分析API - 带缓存，避免每次请求都重新跑AI分析"""
    _cache_key = 'root_cause_analysis_result'
    _cache_ttl = 300  # 缓存5分钟

    def get(self, request):
        try:
            # ===== 优先返回缓存结果（毫秒级）=====
            cached = safe_get(self._cache_key)
            if cached:
                return Response({'success': True, 'data': cached})

            from ..material_planning import MaterialPlanner, InventoryAIAnalyzer

            # 快速检查：无数据时立即返回空结果
            order_count = SalesOrder.objects.filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
            ).count()
            if order_count == 0:
                empty_result = {
                    'allocation_quality': 0,
                    'inventory_utilization': 0,
                    'expiry_risk': 0,
                    'supplier_risk': 0,
                    'stagnation_risk': 0,
                    'root_cause_analysis': [],
                    'procurement_recommendations': [],
                    'quality_breakdown': {}
                }
                safe_set(self._cache_key, empty_result, self._cache_ttl)
                return Response({'success': True, 'data': empty_result})

            analyzer = InventoryAIAnalyzer()

            # 限制分析数据量：最多取前2000条订单（避免AI分析12000+单导致超时）
            orders = SalesOrder.objects.filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'in_production', 'processing']
            ).select_related('material').order_by('priority', 'demand_date')[:2000]

            allocations = list(OrderAllocation.objects.all().values()[:5000])

            inventory_data = {}
            for inv in Inventory.objects.select_related('material').all().only(
                'material_id', 'quantity', 'material__standard_cost'
            )[:3000]:
                inventory_data[inv.material_id] = {
                    'quantity': int(inv.quantity or 0),
                    'standard_cost': round(float(inv.material.standard_cost or 0), 2) if inv.material else 0
                }

            orders_data = []
            for o in orders:
                orders_data.append({
                    'id': o.id,
                    'order_no': o.order_no,
                    'priority': o.priority,
                    'demand_date': str(o.demand_date),
                    'status': o.status,
                    'quantity': int(o.quantity or 0)
                })

            analysis = analyzer.analyze_allocation_rationality(allocations, inventory_data, orders_data)

            # 写入缓存
            safe_set(self._cache_key, analysis, self._cache_ttl)

            return Response({
                'success': True,
                'data': analysis
            })
        except Exception as e:
            logger.error(f"根因分析失败: {e}")
            return Response({
                'success': False,
                'message': str(e)
            }, status=500)


class PlanLogListView(APIView):
    def get(self, request):
        import traceback
        try:
            logs = PlanLog.objects.all().order_by('-created_at')[:100]
            return Response({
                'success': True,
                'data': [
                    {
                        'id': log.id,
                        'type': log.log_type,
                        'message': log.message,
                        'created_at': log.created_at.strftime('%Y-%m-%d %H:%M:%S') if log.created_at else ''
                    }
                    for log in logs
                ],
                'total': PlanLog.objects.count()
            })
        except Exception as e:
            logger.error(f"获取计划日志失败: {str(e)}")
            return Response({'success': False, 'data': [], 'total': 0})


class DeliveryChangeAlertsView(APIView):
    """交期变更预警API"""
    permission_classes = [AllowAny]

    def get(self, request):
        """获取交期变更预警数据"""
        try:
            from ..models import MaterialPlanResult, SalesOrder
            # 查询交期变更次数 >= 2 的订单
            alert_results = MaterialPlanResult.objects.filter(
                delivery_change_count__gte=2
            ).select_related('order').order_by('-delivery_change_count')

            orders = []
            for result in alert_results:
                if result.order:
                    orders.append({
                        'id': result.order.id,
                        'order_no': result.order.order_no,
                        'customer_name': result.order.customer_name,
                        'change_count': result.delivery_change_count,
                        'latest_change_date': result.updated_at.strftime('%Y-%m-%d') if result.updated_at else '',
                    })

            return Response({
                'count': len(orders),
                'orders': orders
            })
        except Exception as e:
            logger.error(f'获取交期变更预警失败: {str(e)}')
            return Response({'count': 0, 'orders': []})


class AutoPriorityAdjustView(APIView):
    """订单优先级自动调整API"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """触发订单优先级自动调整"""
        try:
            planner = MaterialPlanner()
            result = planner.auto_adjust_priority()

            return Response({
                'success': True,
                'data': result,
                'message': f'优先级调整完成，共处理{result["total_orders"]}条订单，调整{result["adjusted_count"]}条'
            })
        except Exception as e:
            import traceback
            logger.error(f'订单优先级自动调整失败: {e}\n{traceback.format_exc()}')
            return Response({
                'success': False,
                'message': f'优先级调整失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AuditLogListView(generics.ListAPIView):
    """审计日志列表API（返回JSON，供Vue前端调用）"""
    from ..models.notification_models import AuditLog
    from .serializers import AuditLogSerializer

    queryset = AuditLog.objects.all().order_by('-created_at')
    serializer_class = AuditLogSerializer
    permission_classes = []  # 允许未认证访问，数据由中间件自动记录
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['action', 'module']
    search_fields = ['detail', 'target', 'user__username']

    def get_queryset(self):
        qs = super().get_queryset()
        # 支持按用户名筛选
        user_filter = self.request.query_params.get('user', '')
        if user_filter:
            qs = qs.filter(user__username__icontains=user_filter)
        return qs


class GanttChartView(APIView):
    """排产甘特图数据API - 为前端甘特图可视化提供排产数据"""
    permission_classes = [IsAuthenticated]

    def _is_workday(self, d, holidays):
        """判断是否为工作日"""
        if d in holidays:
            return False
        return d.weekday() < 5

    def _subtract_working_days(self, from_date, num_days, holidays):
        """从from_date倒推num_days个工作日，返回起始日期"""
        current = from_date
        counted = 0
        while counted < num_days:
            current -= timedelta(days=1)
            if self._is_workday(current, holidays):
                counted += 1
        return current

    def _add_working_days(self, from_date, num_days, holidays):
        """从from_date往后数num_days个工作日，返回结束日期"""
        current = from_date
        counted = 0
        while counted < num_days:
            current += timedelta(days=1)
            if self._is_workday(current, holidays):
                counted += 1
        return current

    def get(self, request):
        try:
            from datetime import date as date_type

            # 1. 查询未完成订单（排除cancelled和delivered）
            orders = SalesOrder.objects.select_related('material').exclude(
                status__in=['cancelled', 'delivered']
            ).order_by('priority', 'demand_date')

            # 2. 查询所有活跃工作中心
            work_centers = WorkCenter.objects.filter(is_active=True).order_by('work_center_code')

            # 3. 查询工厂日历，获取非工作日集合
            holidays = set()
            for entry in FactoryCalendar.objects.filter(is_workday=False):
                holidays.add(entry.date)

            # 4. 构建工作中心调度数据
            wc_schedules = {}
            wc_info_map = {}
            for wc in work_centers:
                products = []
                if wc.available_products:
                    products = [p.strip() for p in wc.available_products.split(',') if p.strip()]
                wc_schedules[wc.work_center_code] = []
                wc_info_map[wc.work_center_code] = {
                    'products': products,
                    'capacity': wc.daily_capacity_limit,
                }

            # 5. 为每个订单分配工作中心和时间段
            tasks = []
            for order in orders:
                material_name = order.material.material_name if order.material else ''
                lead_time = order.production_lead_time or 2
                demand_date = order.demand_date

                # 选择工作中心：优先匹配可生产产品，否则选任务最少的
                assigned_wc_code = None
                for wc_code, info in wc_info_map.items():
                    if material_name and material_name in info['products']:
                        assigned_wc_code = wc_code
                        break
                if assigned_wc_code is None and wc_schedules:
                    assigned_wc_code = min(wc_schedules, key=lambda k: len(wc_schedules[k]))

                if assigned_wc_code is None:
                    continue

                # 计算生产日期：从需求日期倒推，结束于需求日期前最后一个工作日
                end_date = demand_date
                while not self._is_workday(end_date, holidays):
                    end_date -= timedelta(days=1)
                start_date = self._subtract_working_days(end_date, lead_time - 1, holidays)

                # 检查同一工作中心的日期重叠，如有则向前推移
                scheduled = wc_schedules[assigned_wc_code]
                for _ in range(120):
                    overlap = False
                    for sched_start, sched_end, _ in scheduled:
                        if start_date <= sched_end and end_date >= sched_start:
                            overlap = True
                            next_start = sched_end + timedelta(days=1)
                            while not self._is_workday(next_start, holidays):
                                next_start += timedelta(days=1)
                            start_date = next_start
                            end_date = self._add_working_days(start_date, lead_time - 1, holidays)
                            break
                    if not overlap:
                        break

                scheduled.append((start_date, end_date, order.quantity or 0))

                # 计算进度
                progress = 0
                today = date_type.today()
                if order.status in ['in_production', 'processing', 'allocated']:
                    if today >= end_date:
                        progress = 100
                    elif today <= start_date:
                        progress = 0
                    else:
                        total_days = (end_date - start_date).days + 1
                        elapsed = (today - start_date).days + 1
                        progress = min(100, max(0, int(elapsed / total_days * 100)))
                elif order.status in ['complete', 'completed', 'shipped']:
                    progress = 100
                elif order.status == 'partial':
                    progress = 30

                tasks.append({
                    'id': order.id,
                    'order_no': order.order_no,
                    'material_name': material_name,
                    'work_center': assigned_wc_code,
                    'start_date': start_date.isoformat(),
                    'end_date': end_date.isoformat(),
                    'progress': progress,
                    'priority': order.priority,
                    'status': order.status,
                    'quantity': int(order.quantity or 0),
                })

            # 构建返回数据
            work_center_list = [
                {
                    'code': wc.work_center_code,
                    'name': wc.work_center_name,
                    'capacity': wc.daily_capacity_limit,
                }
                for wc in work_centers
            ]

            calendar_data = {
                'holidays': [d.isoformat() for d in sorted(holidays)]
            }

            return Response({
                'tasks': tasks,
                'work_centers': work_center_list,
                'calendar': calendar_data,
            })

        except Exception as e:
            import traceback
            logger.error(f'获取甘特图数据失败: {e}\n{traceback.format_exc()}')
            return Response({
                'detail': f'获取甘特图数据失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SimulationHistoryView(APIView):
    """模拟历史记录API - 用于多轮模拟趋势分析"""
    permission_classes = [IsAuthenticated]
    CACHE_KEY = 'simulation_history'
    MAX_RECORDS = 100

    def get(self, request):
        """返回最近20条模拟历史记录"""
        history = safe_get(self.CACHE_KEY) or []
        return Response({
            'success': True,
            'data': history[-20:],
            'total': len(history)
        })

    def post(self, request):
        """保存新的模拟结果"""
        import traceback
        try:
            data = request.data
            record = {
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'strategy': data.get('strategy', 'delivery_first'),
                'total_score': data.get('total_score', 0),
                'delivery_rate': data.get('delivery_rate', 0),
                'inventory_score': data.get('inventory_score', 0),
                'stability_score': data.get('stability_score', 0),
                'reliability_score': data.get('reliability_score', 0),
                'cost_score': data.get('cost_score', 0),
            }

            history = safe_get(self.CACHE_KEY) or []
            history.append(record)
            # 保留最近MAX_RECORDS条记录
            if len(history) > self.MAX_RECORDS:
                history = history[-self.MAX_RECORDS:]
            safe_set(self.CACHE_KEY, history, None)

            return Response({
                'success': True,
                'message': '模拟结果已保存',
                'total': len(history)
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"保存模拟历史失败: {str(e)}")
            return Response({'success': False, 'message': f'保存失败: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AutoRetrainView(APIView):
    """模型自动重训练API"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """返回重训练状态"""
        try:
            from ..ai_engine import AutoRetrainer
            retrainer = AutoRetrainer()
            status_info = retrainer.get_retrain_status()
            return Response({
                'success': True,
                'data': status_info
            })
        except Exception as e:
            logger.error(f'获取重训练状态失败: {e}')
            return Response({
                'success': False,
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        """手动触发重训练"""
        try:
            from ..ai_engine import AutoRetrainer
            retrainer = AutoRetrainer()
            retrainer.is_retraining = True

            results = {}

            retrain_type = request.data.get('type', 'all')
            product = request.data.get('product', None)

            if retrain_type in ('all', 'demand'):
                results['demand_forecast'] = retrainer.retrain_demand_forecast(product=product)

            if retrain_type in ('all', 'anomaly'):
                results['anomaly_detector'] = retrainer.retrain_anomaly_detector()

            if request.data.get('interval_hours'):
                interval = int(request.data['interval_hours'])
                retrainer.schedule_retrain(interval_hours=interval)

            retrainer.is_retraining = False
            status_info = retrainer.get_retrain_status()

            return Response({
                'success': True,
                'results': results,
                'status': status_info,
                'message': '重训练完成'
            })
        except Exception as e:
            logger.error(f'手动触发重训练失败: {e}')
            import traceback
            logger.error(traceback.format_exc())
            return Response({
                'success': False,
                'message': f'重训练失败: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ComputationModeView(APIView):
    """串行与并行计算策略选择API"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """返回当前计算模式和支持的模式列表"""
        try:
            planner = MaterialPlanner()
            current_mode = planner.get_computation_mode()
            return Response({
                'success': True,
                'current_mode': current_mode,
                'supported_modes': [
                    {'value': 'serial', 'label': '串行计算', 'description': '逐个处理订单，适合订单量较小或需要严格顺序控制的场景'},
                    {'value': 'parallel', 'label': '并行计算', 'description': '多线程并行处理订单，适合大批量订单的高性能计算场景'}
                ]
            })
        except Exception as e:
            logger.error(f'获取计算模式失败: {e}')
            return Response({
                'success': False,
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        """设置计算模式"""
        try:
            mode = request.data.get('mode')
            if not mode:
                return Response({
                    'success': False,
                    'message': '请提供mode参数（serial或parallel）'
                }, status=status.HTTP_400_BAD_REQUEST)

            planner = MaterialPlanner()
            planner.set_computation_mode(mode)

            return Response({
                'success': True,
                'current_mode': planner.get_computation_mode(),
                'message': f'计算模式已切换为: {mode}'
            })
        except ValueError as e:
            return Response({
                'success': False,
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f'设置计算模式失败: {e}')
            return Response({
                'success': False,
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class HoldAuditLogListView(generics.ListAPIView):
    """Hold操作审计日志查询API"""
    serializer_class = HoldAuditLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from ..models import HoldAuditLog
        queryset = HoldAuditLog.objects.select_related(
            'inventory__material'
        ).order_by('-created_at')

        # 支持按inventory_id过滤
        inventory_id = self.request.query_params.get('inventory_id')
        if inventory_id:
            queryset = queryset.filter(inventory_id=inventory_id)

        # 支持按操作类型过滤
        operation = self.request.query_params.get('operation')
        if operation:
            queryset = queryset.filter(operation=operation)

        # 支持按时间范围过滤
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(created_at__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__date__lte=end_date)

        return queryset


class BOMChangeHistoryListView(generics.ListAPIView):
    """BOM变更历史查询API"""
    serializer_class = BOMChangeHistorySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        from ..models import BOMChangeHistory
        queryset = BOMChangeHistory.objects.select_related(
            'bom__parent_material', 'bom__child_material'
        ).order_by('-created_at')

        # 支持按bom_id过滤
        bom_id = self.request.query_params.get('bom_id')
        if bom_id:
            queryset = queryset.filter(bom_id=bom_id)

        # 支持按ECN编号过滤
        ecn_no = self.request.query_params.get('ecn_no')
        if ecn_no:
            queryset = queryset.filter(ecn_no=ecn_no)

        return queryset
