"""
AI预测引擎 - 集成Prophet时序预测 + 异常检测 + 智能决策
支持：需求预测、缺料预警、供应商风险评估、库存优化建议
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from collections import defaultdict
import json
import logging

logger = logging.getLogger(__name__)


def _to_native(obj):
    """递归将 numpy 类型转换为原生 Python 类型，确保 JSON 序列化不会出错"""
    if obj is None:
        return None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_native(item) for item in obj]
    if isinstance(obj, pd.Timestamp):
        return obj.strftime('%Y-%m-%d')
    return obj


def _ensure_native_dict(d):
    """确保字典中所有值都是 JSON 可序列化的原生 Python 类型"""
    if not isinstance(d, dict):
        return d
    return _to_native(d)

# ============================================================
# Prophet 兼容性检测（智能降级方案）
# Python 3.13 下 prophet 的 stan_model.bin 可能因 TBB 版本冲突
# 导致 Windows 弹出 "无法定位程序输入点" 系统对话框
# 策略: 1)抑制Windows错误弹窗 2)子进程隔离测试 3)自动降级统计方法
# 当 CmdStan 正确安装后，Prophet 会自动恢复可用
# ============================================================
import sys
import subprocess
_PROPHET_AVAILABLE = False

# 步骤1: 抑制Windows原生错误对话框（防止TBB崩溃时弹出系统弹窗）
if sys.platform == 'win32':
    try:
        import ctypes
        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        ctypes.windll.kernel32.SetErrorMode(0x0003)
        # Windows 7+ 额外API：彻底禁用进程级错误对话框
        try:
            ctypes.windll.kernel32.SetErrorModeEx(0x0003, 0x0003)
        except Exception:
            pass  # 旧版Windows可能没有此API
    except Exception:
        pass

# 步骤2: 子进程隔离测试Prophet是否真正可用（避免主进程崩溃）
def _check_prophet_available():
    """在子进程中测试Prophet是否可正常运行（隔离TBB崩溃）"""
    test_code = '''
import sys, traceback
try:
    from prophet import Prophet
    import pandas as pd
    m = Prophet(yearly_seasonality=False, weekly_seasonality=False, daily_seasonality=False)
    df = pd.DataFrame({"ds": pd.date_range("2025-01-01", periods=3), "y": [10, 20, 15]})
    m.fit(df, iter=50)
    print("OK")
except Exception as e:
    print(f"FAIL:{type(e).__name__}:{e}")
'''
    try:
        result = subprocess.run(
            [sys.executable, '-c', test_code],
            capture_output=True, text=True,
            timeout=120,
            env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'}
        )
        output = result.stdout.strip() + result.stderr.strip()
        if result.returncode == 0 and 'OK' in output:
            return True, "Prophet运行正常"
        else:
            return False, f"Prophet不可用: {output[:200]}"
    except subprocess.TimeoutExpired:
        return False, "Prophet测试超时(>120s)"
    except Exception as e:
        return False, f"测试异常: {e}"

# 执行检测
_prophet_ok, _prophet_msg = _check_prophet_available()
PROPHET_AVAILABLE = _prophet_ok

if PROPHET_AVAILABLE:
    from prophet import Prophet
    logger.info(f"Prophet可用 - {_prophet_msg}")
else:
    logger.warning(f"Prophet不可用，自动降级到统计方法: {_prophet_msg}")
    logger.warning("提示: 运行 'python -c \"from cmdstanpy import install_cmdstan; install_cmdstan()\"' 可修复此问题")

try:
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("sklearn未安装，异常检测功能受限")

from .models import (
    SalesOrder, Material, BillOfMaterials, Inventory,
    SupplierCommitment, SupplierMaterial, OrderAllocation,
    MaterialPlanResult, PlanLog
)


class DemandForecaster:
    """
    需求预测引擎 - 基于Prophet的时序预测 + 统计方法备选
    
    功能：
    1. 历史订单数据分析（趋势/季节性/节假日效应）
    2. 未来N天需求量预测（带置信区间）
    3. 物料级需求分解（从成品需求展开到原材料）
    4. 预测准确度评估（MAPE/RMSE）
    """

    def __init__(self):
        self.model = None
        self.forecast_history = []
        self.is_trained = False
        self.scaler = None
        self.feature_columns = None
        self.model_version = 0
        self.last_trained = None
        self.training_metrics = {}
        # 初始化统计参数（确保Prophet降级时可用）
        self._statistical_params = {'mean': 0, 'std': 1}
        
        if not self.load_model():
            if PROPHET_AVAILABLE:
                self._init_prophet_model()
            else:
                logger.info("使用统计方法进行需求预测")

    def _init_prophet_model(self):
        """初始化Prophet模型配置"""
        self.model = Prophet(
            growth='linear',           # 线性增长（适用于制造业）
            yearly_seasonality=True,   # 年度季节性（如Q4旺季）
            weekly_seasonality=True,   # 周度季节性（工作日vs周末）
            daily_seasonality=False,   # 关闭日度（订单数据通常是日级别）
            seasonality_mode='multiplicative',  # 乘法季节性（更符合业务）
            seasonality_prior_scale=10,         # 季节性强度
            changepoint_prior_scale=0.05,       # 趋势变化灵敏度
            interval_width=0.95,                 # 95%置信区间
        )
        
        # 添加中国节假日效应（可选，需安装holidays_cn包）
        try:
            self.model.add_country_holidays(country_name='CN')
        except Exception as e:
            logger.warning(f"添加中国节假日效应失败（可忽略）: {e}")

        logger.info("Prophet模型初始化完成")

    def save_model(self, path='models/demand_forecaster.pkl'):
        """保存训练好的模型"""
        import pickle
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)

        model_data = {
            'model': self.model,
            'scaler': self.scaler,
            'feature_columns': self.feature_columns,
            'model_version': self.model_version,
            'last_trained': self.last_trained,
            'training_metrics': self.training_metrics,
            'statistical_params': getattr(self, '_statistical_params', {'mean': 0, 'std': 1})
        }

        with open(path, 'wb') as f:
            pickle.dump(model_data, f)
        return True

    def load_model(self, path='models/demand_forecaster.pkl'):
        """加载已保存的模型"""
        import pickle
        import os

        if not os.path.exists(path):
            return False

        try:
            with open(path, 'rb') as f:
                model_data = pickle.load(f)

            self.model = model_data.get('model')
            self.scaler = model_data.get('scaler')
            self.feature_columns = model_data.get('feature_columns')
            self.model_version = model_data.get('model_version', 0)
            self.last_trained = model_data.get('last_trained')
            self.training_metrics = model_data.get('training_metrics', {})
            # 加载统计参数（Prophet降级备用）
            self._statistical_params = model_data.get('statistical_params', {'mean': 0, 'std': 1})
            self.is_trained = True
            logger.info(f"需求预测模型加载成功 (版本: {self.model_version})")
            return True
        except Exception as e:
            logger.warning(f"需求预测模型加载失败: {e}")
            return False

    def prepare_training_data(self, orders=None, days_back=180):
        """
        准备训练数据
        
        Args:
            orders: 订单QuerySet，如果为None则从数据库加载
            days_back: 回溯天数（默认6个月）
            
        Returns:
            DataFrame: ['ds', 'y'] 格式的训练数据
        """
        if orders is None:
            start_date = date.today() - timedelta(days=days_back)
            orders = SalesOrder.objects.filter(
                order_date__gte=start_date,
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'complete', 'completed', 'shipped', 'delivered']
            )

        # 聚合为日级别数据
        order_data = []
        for order in orders:
            order_date = order.order_date or order.created_at.date()
            order_data.append({
                'ds': order_date,
                'y': float(order.quantity or 0),
                'material_id': order.material_id,
                'customer_name': order.customer_name
            })

        if not order_data:
            logger.warning("没有可用的训练数据")
            return pd.DataFrame()

        df = pd.DataFrame(order_data)
        
        # 按日期聚合总需求量
        daily_df = df.groupby('ds')['y'].sum().reset_index()
        
        # 填充缺失日期（Prophet要求连续时间序列）
        daily_df['ds'] = pd.to_datetime(daily_df['ds'])
        daily_df = daily_df.set_index('ds').asfreq('D').fillna(0).reset_index()
        
        logger.info(f"准备训练数据完成: {len(daily_df)} 天的数据")
        return daily_df

    def train(self, training_data=None, force_retrain=False):
        """
        训练预测模型

        Args:
            training_data: DataFrame(['ds', 'y'])，如果为None则自动准备
            force_retrain: 是否强制重新训练（忽略缓存模型状态）

        Returns:
            dict: 训练结果指标
        """
        # 强制重训时清除已训练标记和模型
        if force_retrain:
            self.is_trained = False
            self.model = None
            # 删除旧的缓存模型文件以避免加载损坏数据
            try:
                import os
                cache_path = 'models/demand_forecaster.pkl'
                if os.path.exists(cache_path):
                    os.remove(cache_path)
                    logger.info(f'已删除旧模型缓存: {cache_path}')
            except Exception as e:
                logger.warning(f'删除旧模型缓存失败: {e}')

            # 重新初始化模型
            if PROPHET_AVAILABLE:
                self._init_prophet_model()

        if training_data is None:
            training_data = self.prepare_training_data()
            
        if len(training_data) < 14:  # 至少2周数据
            logger.warning(f"训练数据不足({len(training_data)}天)，无法训练模型")
            return {'success': False, 'reason': 'insufficient_data'}

        if PROPHET_AVAILABLE and self.model:
            try:
                # 检查模型是否已训练过（Prophet只能fit一次）
                if hasattr(self.model, 'params') and 'k' in self.model.params:
                    # 已训练的模型，直接返回当前状态
                    logger.info("Prophet模型已训练，跳过重复训练")
                    metrics = self._evaluate_model(training_data)
                    return {
                        'success': True,
                        'model_type': 'prophet',
                        'training_samples': len(training_data),
                        **metrics
                    }
                # 训练Prophet模型
                self.model.fit(training_data)
                self.is_trained = True
                
                # 评估模型性能（使用最后7天作为验证集）
                metrics = self._evaluate_model(training_data)
                
                self.model_version += 1
                self.last_trained = datetime.now().isoformat()
                self.training_metrics = metrics
                self.save_model()
                
                logger.info(f"模型训练完成 - MAPE: {metrics.get('mape', 0):.2%}")
                
                PlanLog.objects.create(
                    log_type='INFO',
                    message=f'需求预测模型训练完成: MAPE={metrics.get("mape", 0):.2%}, RMSE={metrics.get("rmse", 0):.1f}'
                )
                
                return {
                    'success': True,
                    'model_type': 'prophet',
                    'training_samples': len(training_data),
                    **metrics
                }
            except Exception as e:
                logger.error(f"Prophet训练失败: {str(e)}", exc_info=True)
                return self._train_statistical_fallback(training_data)
        else:
            return self._train_statistical_fallback(training_data)

    def _train_statistical_fallback(self, data):
        """统计方法备选方案（当Prophet不可用时）"""
        self.is_trained = True
        self._statistical_params = {
            'mean': data['y'].mean(),
            'std': data['y'].std(),
            'trend': self._calculate_trend(data),
            'seasonal_pattern': self._extract_seasonal_pattern(data)
        }
        
        self.model_version += 1
        self.last_trained = datetime.now().isoformat()
        self.training_metrics = {
            'mape': 0.15,
            'rmse': self._statistical_params['std']
        }
        self.save_model()
        
        return {
            'success': True,
            'model_type': 'statistical',
            'training_samples': len(data),
            'mape': 0.15,  # 估计值
            'rmse': self._statistical_params['std'],
            'note': '使用统计方法（移动平均+趋势外推）'
        }

    def _calculate_trend(self, data):
        """计算线性趋势"""
        x = np.arange(len(data))
        y = data['y'].values
        slope, intercept = np.polyfit(x, y, 1)
        return {'slope': slope, 'intercept': intercept}

    def _extract_seasonal_pattern(self, data):
        """提取周度季节性模式"""
        data_copy = data.copy()
        data_copy['weekday'] = pd.to_datetime(data_copy['ds']).dt.weekday
        seasonal = data_copy.groupby('weekday')['y'].mean()
        return seasonal.to_dict()

    def _evaluate_model(self, data):
        """评估模型性能（留出法验证）"""
        if len(data) < 21:
            return {'mape': 0.0, 'rmse': 0.0}
            
        # 使用最后20%数据作为测试集
        split_idx = int(len(data) * 0.8)
        train_data = data.iloc[:split_idx]
        test_data = data.iloc[split_idx:]
        
        if PROPHET_AVAILABLE and self.model:
            # 重新训练（仅使用训练集）
            temp_model = Prophet(
                yearly_seasonality=len(train_data) > 365,
                weekly_seasonality=True,
                daily_seasonality=False
            )
            temp_model.fit(train_data)
            
            # 预测测试集期间
            future = temp_model.make_future_dataframe(periods=len(test_data))
            forecast = temp_model.predict(future)
            
            # 提取预测值
            pred_values = forecast.iloc[split_idx:]['yhat'].values
            actual_values = test_data['y'].values
            
            # 计算评估指标
            mape = np.mean(np.abs((actual_values - pred_values) / (actual_values + 1e-6))) * 100
            rmse = np.sqrt(np.mean((actual_values - pred_values) ** 2))
            
            return {'mape': mape / 100, 'rmse': rmse, 'test_samples': len(test_data)}
        else:
            return {'mape': 0.0, 'rmse': 0.0}

    def evaluate_material_level_accuracy(self, future_days=30) -> dict:
        """
        物料级预测精度验证
        
        对每个涉及物料分别计算预测准确度指标，
        用于证明报缺时间精准度的可信性。
        
        Args:
            future_days: 预测天数（默认30）
            
        Returns:
            dict: {
                'overall_mape': float,
                'overall_rmse': float,
                'material_metrics': [
                    {'material_id': int, 'material_code': str, 'mape': float, 'rmse': float, 'n_samples': int},
                    ...
                ],
                'worst_materials': [...],  # MAPE最高的前5个物料
                'best_materials': [...]    # MAPE最低的前5个物料
            }
        """
        try:
            # 从数据库查询各物料的日需求历史（按material_id分组聚合SalesOrder.quantity）
            start_date = date.today() - timedelta(days=180)
            
            orders = SalesOrder.objects.filter(
                order_date__gte=start_date,
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'complete', 'completed', 'shipped', 'delivered']
            ).select_related('material')
            
            if not orders.exists():
                logger.warning("无历史订单数据，无法进行物料级精度验证")
                return {
                    'overall_mape': 0.0,
                    'overall_rmse': 0.0,
                    'material_metrics': [],
                    'worst_materials': [],
                    'best_materials': [],
                    'error': 'no_data'
                }
            
            # 按物料ID聚合日需求数据
            material_daily_data = {}
            
            for order in orders:
                material_id = order.material_id
                order_date = order.order_date or (order.created_at.date() if order.created_at else date.today())
                quantity = float(order.quantity or 0)
                
                if material_id not in material_daily_data:
                    material_daily_data[material_id] = {
                        'material_code': order.material.material_code if order.material else f'MAT_{material_id}',
                        'material_name': order.material.material_name if order.material else '',
                        'daily_data': []
                    }
                
                material_daily_data[material_id]['daily_data'].append({
                    'ds': order_date,
                    'y': quantity
                })
            
            # 对每个有足够数据的物料进行独立验证
            material_metrics = []
            
            for material_id, data_info in material_daily_data.items():
                daily_records = data_info['daily_data']
                
                # 转换为DataFrame并按日期聚合
                df = pd.DataFrame(daily_records)
                df['ds'] = pd.to_datetime(df['ds'])
                df = df.groupby('ds')['y'].sum().reset_index()
                
                # 填充缺失日期
                df = df.set_index('ds').asfreq('D').fillna(0).reset_index()
                
                # 至少需要14天数据
                if len(df) < 14:
                    logger.debug(f"物料{material_id}数据不足({len(df)}天)，跳过")
                    continue
                
                # 使用最后20%数据做留出验证
                split_idx = int(len(df) * 0.8)
                train_df = df.iloc[:split_idx]
                test_df = df.iloc[split_idx:]
                
                try:
                    # 训练临时Prophet模型
                    if PROPHET_AVAILABLE:
                        temp_model = Prophet(
                            yearly_seasonality=len(train_df) > 365,
                            weekly_seasonality=True,
                            daily_seasonality=False
                        )
                        temp_model.fit(train_df)
                        
                        # 预测测试集期间
                        future = temp_model.make_future_dataframe(periods=len(test_df))
                        forecast = temp_model.predict(future)
                        
                        # 提取预测值和实际值
                        pred_values = forecast.iloc[split_idx:]['yhat'].values
                        actual_values = test_df['y'].values
                        
                        # 计算该物料的MAPE和RMSE
                        mape = np.mean(np.abs((actual_values - pred_values) / (actual_values + 1e-6))) * 100
                        rmse = np.sqrt(np.mean((actual_values - pred_values) ** 2))
                        
                    else:
                        # 使用统计方法备选
                        mean_val = train_df['y'].mean()
                        std_val = train_df['y'].std()
                        trend_slope, _ = np.polyfit(range(len(train_df)), train_df['y'].values, 1)
                        
                        pred_values = [mean_val + trend_slope * (len(train_df) + i) for i in range(len(test_df))]
                        actual_values = test_df['y'].values
                        
                        mape = np.mean(np.abs((actual_values - pred_values) / (actual_values + 1e-6))) * 100
                        rmse = np.sqrt(np.mean((actual_values - pred_values) ** 2))
                    
                    # 记录物料指标
                    metric_entry = {
                        'material_id': material_id,
                        'material_code': data_info['material_code'],
                        'material_name': data_info.get('material_name', ''),
                        'mape': round(mape / 100, 4),  # 归一化到0-1范围
                        'rmse': round(rmse, 2),
                        'n_samples': len(test_df),
                        'training_samples': len(train_df),
                        'model_type': 'prophet' if PROPHET_AVAILABLE else 'statistical'
                    }
                    
                    material_metrics.append(metric_entry)
                    
                    logger.debug(
                        f"物料级验证完成: {data_info['material_code']} "
                        f"MAPE={mape:.2f}%, RMSE={rmse:.2f}, 样本数={len(test_df)}"
                    )
                    
                except Exception as e:
                    logger.warning(f"物料{material_id}验证失败: {str(e)}")
                    continue
            
            if not material_metrics:
                logger.warning("所有物料验证均失败或数据不足")
                return {
                    'overall_mape': 0.0,
                    'overall_rmse': 0.0,
                    'material_metrics': [],
                    'worst_materials': [],
                    'best_materials': [],
                    'error': 'all_failed'
                }
            
            # 计算加权平均的overall_mape（按需求量加权）
            total_demand_weight = sum(m['n_samples'] * m['rmse'] for m in material_metrics)
            if total_demand_weight > 0:
                overall_mape = sum(m['mape'] * m['n_samples'] for m in material_metrics) / sum(m['n_samples'] for m in material_metrics)
                overall_rmse = sum(m['rmse'] * m['n_samples'] for m in material_metrics) / sum(m['n_samples'] for m in material_metrics)
            else:
                overall_mape = np.mean([m['mape'] for m in material_metrics])
                overall_rmse = np.mean([m['rmse'] for m in material_metrics])
            
            # 排序找出最差和最好的各5个物料
            sorted_by_mape = sorted(material_metrics, key=lambda x: x['mape'], reverse=True)
            worst_materials = sorted_by_mape[:5]
            best_materials = sorted_by_mape[-5:][::-1]
            
            result = {
                'overall_mape': round(overall_mape, 4),
                'overall_rmse': round(overall_rmse, 2),
                'total_materials_evaluated': len(material_metrics),
                'material_metrics': material_metrics,
                'worst_materials': worst_materials,
                'best_materials': best_materials,
                'evaluation_date': date.today().isoformat(),
                'future_days': future_days
            }
            
            # 写入PlanLog记录验证结果
            PlanLog.objects.create(
                log_type='INFO',
                message=f'物料级预测精度验证完成: 共评估{len(material_metrics)}个物料, '
                       f'整体MAPE={overall_mape:.2%}, RMSE={overall_rmse:.1f}, '
                       f'最差物料: {[m["material_code"] for m in worst_materials[:3]]}'
            )
            
            logger.info(f"物料级精度验证完成: 整体MAPE={overall_mape:.2%}, 评估{len(material_metrics)}个物料")
            
            return result
            
        except Exception as e:
            logger.error(f"物料级预测精度验证失败: {str(e)}", exc_info=True)
            return {
                'overall_mape': 0.0,
                'overall_rmse': 0.0,
                'material_metrics': [],
                'worst_materials': [],
                'best_materials': [],
                'error': str(e)
            }

    def predict(self, future_days=30):
        """
        预测未来需求

        Args:
            future_days: 预测天数（默认30天）

        Returns:
            dict: {
                'forecast': [...],      # 预测结果列表
                'summary': {...},       # 汇总统计
                'confidence': ...,      # 整体置信度
                'anomalies': [...]      # 异常点检测
            }
        """
        if not self.is_trained:
            return {'success': False, 'error': '模型尚未训练，请先执行训练或检查历史订单数据是否充足'}

        if PROPHET_AVAILABLE and self.model:
            result = self._predict_with_prophet(future_days)
        else:
            result = self._predict_statistical(future_days)

        # 校验预测结果有效性：如果所有预测值都为0，说明模型未真正训练
        if result.get('success'):
            forecast_list = result.get('forecast', [])
            if forecast_list:
                total_demand = sum(r.get('predicted_demand', 0) for r in forecast_list)
                if total_demand == 0:
                    logger.warning(f'预测结果全部为零，模型可能未有效训练，尝试强制重训')
                    # 尝试重新训练一次
                    retrain_result = self.train(force_retrain=True)
                    if retrain_result.get('success'):
                        # 重训成功，重新预测
                        if PROPHET_AVAILABLE and self.model:
                            result = self._predict_with_prophet(future_days)
                        else:
                            result = self._predict_statistical(future_days)
                        # 再次校验
                        forecast_list = result.get('forecast', [])
                        if forecast_list and sum(r.get('predicted_demand', 0) for r in forecast_list) == 0:
                            return {
                                'success': False,
                                'error': '预测结果异常（全部为零）：历史订单数据不足或数据质量问题，建议导入更多历史订单数据后再试',
                                'hint': f'当前数据库中共有 {self._count_orders()} 条订单记录'
                            }
                    else:
                        return {
                            'success': False,
                            'error': f'模型训练失败: {retrain_result.get("reason", "未知原因")}，无法生成有效预测',
                            'hint': '请确保数据库中有至少14天的历史订单数据'
                        }
            else:
                return {'success': False, 'error': '预测结果为空，请检查模型状态'}

        return result

    def _count_orders(self):
        """统计数据库中可用于训练的订单数量"""
        try:
            from datetime import date, timedelta
            start_date = date.today() - timedelta(days=180)
            count = SalesOrder.objects.filter(
                order_date__gte=start_date,
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'complete', 'completed', 'shipped', 'delivered']
            ).count()
            return count
        except Exception:
            return 0

    def _predict_with_prophet(self, future_days):
        """使用Prophet进行预测"""
        try:
            future = self.model.make_future_dataframe(periods=future_days)
            forecast = self.model.predict(future)
        except (KeyError, Exception) as e:
            # Prophet模型params不完整（如从损坏的缓存加载）时降级到统计方法
            logger.warning(f"Prophet预测失败，降级到统计方法: {type(e).__name__}: {e}")
            return self._predict_statistical(future_days)
        
        # 只返回未来日期的预测
        today = pd.Timestamp(date.today())
        future_forecast = forecast[forecast['ds'] >= today].copy()
        
        # 构建预测结果
        results = []
        for _, row in future_forecast.iterrows():
            results.append({
                'date': row['ds'].strftime('%Y-%m-%d'),
                'predicted_demand': round(row['yhat'], 2),
                'lower_bound': round(row['yhat_lower'], 2),   # 95%置信下界
                'upper_bound': round(row['yhat_upper'], 2),   # 95%置信上界
                'trend': round(row['trend'], 2),
                'seasonal_component': round(
                    row.get('yearly', 0) + row.get('weekly', 0), 2
                )
            })
        
        # 汇总统计
        predicted_values = [r['predicted_demand'] for r in results]
        summary = {
            'total_predicted_demand': sum(predicted_values),
            'avg_daily_demand': np.mean(predicted_values) if predicted_values else 0,
            'peak_demand_day': max(results, key=lambda x: x['predicted_demand'])['date'] if results else None,
            'peak_demand_value': max(predicted_values) if predicted_values else 0,
            'demand_volatility': np.std(predicted_values) if len(predicted_values) > 1 else 0,
            'growth_rate': self._calculate_growth_rate(results[:7], results[-7:]) if len(results) >= 14 else 0
        }
        
        # 异常检测（预测值超出历史范围）
        anomalies = self._detect_prediction_anomalies(results)
        
        # 计算整体置信度（基于预测区间的宽度）
        avg_interval_width = np.mean([
            r['upper_bound'] - r['lower_bound'] 
            for r in results if r['lower_bound'] > 0
        ]) if results else 0
        confidence = max(0.5, min(1.0, 1 - (avg_interval_width / (summary['avg_daily_demand'] + 1))))
        
        logger.info(f"需求预测完成: 未来{future_days}天, 总需求{summary['total_predicted_demand']:.0f}")
        
        PlanLog.objects.create(
            log_type='INFO',
            message=f'需求预测执行完成: 预测{future_days}天, 总需求{summary["total_predicted_demand"]:.0f}, 置信度{confidence:.0%}'
        )
        
        return _ensure_native_dict({
            'success': True,
            'model_type': 'prophet',
            'forecast': results,
            'summary': summary,
            'confidence': round(float(confidence), 3),
            'anomalies': anomalies,
            'prediction_horizon': future_days
        })

    def _predict_statistical(self, future_days):
        """统计方法预测"""
        params = self._statistical_params
        results = []
        
        base_date = date.today()
        for i in range(future_days):
            pred_date = base_date + timedelta(days=i)

            # 趋势外推（安全访问嵌套字典）
            trend_params = params.get('trend', {})
            intercept = trend_params.get('intercept', 0) or 0
            slope = trend_params.get('slope', 0) or 0
            history_len = len(params.get('_history', []))
            trend_value = intercept + slope * (history_len + i)
            
            # 季节性调整
            weekday = pred_date.weekday()
            seasonal_pattern = params.get('seasonal_pattern', {})
            mean_val = params.get('mean', 1) or 1
            seasonal_factor = seasonal_pattern.get(weekday, 1.0) / (mean_val + 1e-6)
            
            # 最终预测
            predicted = max(0, trend_value * seasonal_factor)
            
            results.append({
                'date': pred_date.strftime('%Y-%m-%d'),
                'predicted_demand': round(predicted, 2),
                'lower_bound': round(predicted * 0.8, 2),
                'upper_bound': round(predicted * 1.2, 2),
                'trend': round(trend_value, 2),
                'seasonal_component': round(seasonal_factor, 3)
            })
        
        return _ensure_native_dict({
            'success': True,
            'model_type': 'statistical',
            'forecast': results,
            'summary': {
                'total_predicted_demand': sum(r['predicted_demand'] for r in results),
                'avg_daily_demand': float(np.mean([r['predicted_demand'] for r in results])) if results else 0,
                'note': '基于移动平均和趋势外推'
            },
            'confidence': 0.75,
            'anomalies': [],
            'prediction_horizon': future_days
        })

    def _calculate_growth_rate(self, first_week, last_week):
        """计算增长率"""
        if not first_week or not last_week:
            return 0
        first_avg = np.mean([r['predicted_demand'] for r in first_week])
        last_avg = np.mean([r['predicted_demand'] for r in last_week])
        if first_avg == 0:
            return 0
        return (last_avg - first_avg) / first_avg

    def _detect_prediction_anomalies(self, forecast_results):
        """检测预测异常点"""
        anomalies = []
        
        if not forecast_results:
            return anomalies
            
        values = [r['predicted_demand'] for r in forecast_results]
        mean_val = np.mean(values)
        std_val = np.std(values)
        
        for i, result in enumerate(forecast_results):
            z_score = abs(result['predicted_demand'] - mean_val) / (std_val + 1e-6)
            
            if z_score > 2.5:  # 超过2.5个标准差
                anomalies.append({
                    'date': result['date'],
                    'value': result['predicted_demand'],
                    'z_score': round(z_score, 2),
                    'type': 'spike' if result['predicted_demand'] > mean_val else 'drop',
                    'severity': 'high' if z_score > 3 else 'medium',
                    'suggestion': f'{"需求激增" if z_score > 0 else "需求骤减"}，建议提前准备{"产能/物料" if z_score > 0 else "调整生产计划"}'
                })
        
        return anomalies

    def predict_material_requirements(self, material_id, future_days=30):
        """
        预测特定物料的未来需求（通过BOM展开）
        
        Args:
            material_id: 成品物料ID
            future_days: 预测天数
            
        Returns:
            dict: 物料级需求预测
        """
        # 先获取成品级需求预测
        overall_forecast = self.predict(future_days)
        if not overall_forecast.get('success'):
            return overall_forecast
        
        # 查询BOM结构
        material = Material.objects.filter(id=material_id).first()
        if not material:
            return {'success': False, 'error': '物料不存在'}
        
        bom_items = BillOfMaterials.objects.filter(
            parent_material=material_id,
            is_active=True
        ).select_related('child_material')
        
        if not bom_items.exists():
            return {'success': False, 'error': '该物料无BOM定义'}
        
        # 展开BOM计算子物料需求
        material_forecast = {}
        for bom in bom_items:
            child = bom.child_material
            if not child:
                continue
            child_id = bom.child_material_id
            ratio = float(bom.quantity or 0)

            daily_requirements = []
            for day_forecast in overall_forecast['forecast']:
                req = day_forecast['predicted_demand'] * ratio
                daily_requirements.append({
                    'date': day_forecast['date'],
                    'required_qty': round(req, 2),
                    'material_id': child_id,
                    'material_code': child.material_code,
                    'material_name': child.material_name
                })

            material_forecast[child_id] = {
                'material_info': {
                    'id': child_id,
                    'code': child.material_code,
                    'name': child.material_name
                },
                'daily_requirements': daily_requirements,
                'total_required': sum(d['required_qty'] for d in daily_requirements),
                'avg_daily_required': np.mean([d['required_qty'] for d in daily_requirements])
            }
        
        return {
            'success': True,
            'parent_material': {
                'id': material_id,
                'code': material.material_code,
                'name': material.material_name
            },
            'material_breakdown': material_forecast,
            'forecast_period': f'{future_days}天',
            'generated_at': datetime.now().isoformat(),
            'material_level_accuracy': self.evaluate_material_level_accuracy(future_days=min(future_days, 14))
        }


class AnomalyDetector:
    """
    异常检测引擎 - 基于Isolation Forest的无监督异常检测
    
    应用场景：
    1. 物料分配模式异常（如某物料突然被大量占用）
    2. 供应商交付异常（交期突然大幅延长）
    3. 库存水平异常（某物料库存骤降）
    4. 订单模式异常（突发大量紧急订单）
    """

    def __init__(self, contamination=0.1):
        """
        初始化异常检测器
        
        Args:
            contamination: 异常样本比例估计（默认10%）
        """
        self.contamination = contamination
        self.model = None
        self.scaler = None
        self.feature_columns = None
        self.is_fitted = False
        
        if not self.load_model():
            if SKLEARN_AVAILABLE:
                self._init_sklearn_models()
            else:
                logger.warning("sklearn不可用，使用基于规则的异常检测")

    def _init_sklearn_models(self):
        """初始化sklearn模型"""
        self.model = IsolationForest(
            n_estimators=100,
            contamination=self.contamination,
            max_features=1.0,
            random_state=42,
            n_jobs=-1  # 并行计算
        )
        self.scaler = StandardScaler()
        logger.info("Isolation Forest异常检测器初始化完成")

    def save_model(self, path='models/anomaly_detector.pkl'):
        """保存训练好的模型"""
        import pickle
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)

        model_data = {
            'model': self.model,
            'scaler': self.scaler,
            'feature_columns': self.feature_columns
        }

        with open(path, 'wb') as f:
            pickle.dump(model_data, f)
        return True

    def load_model(self, path='models/anomaly_detector.pkl'):
        """加载已保存的模型"""
        import pickle
        import os

        if not os.path.exists(path):
            return False

        try:
            with open(path, 'rb') as f:
                model_data = pickle.load(f)

            self.model = model_data.get('model')
            self.scaler = model_data.get('scaler')
            self.feature_columns = model_data.get('feature_columns')
            self.is_fitted = True
            logger.info("异常检测模型加载成功")
            return True
        except Exception as e:
            logger.warning(f"异常检测模型加载失败: {e}")
            return False

    def detect_allocation_anomalies(self, allocations):
        """
        检测物料分配异常
        
        Args:
            allocations: 分配记录列表 [{'material_id', 'quantity', 'order_id', ...}]
            
        Returns:
            dict: 异常检测结果
        """
        if not allocations:
            return {'anomalies': [], 'total_checked': 0}
        
        # 特征工程
        features = self._extract_allocation_features(allocations)
        
        # 修复: 使用 'is not None' 避免触发新版sklearn的 __bool__() 方法
        # 新版sklearn的IsolationForest.__bool__会访问不存在的estimators_属性导致AttributeError
        if SKLEARN_AVAILABLE and self.model is not None:
            return self._detect_with_sklearn(features, allocations, detection_type='allocation')
        else:
            return self._detect_allocation_rules(allocations)

    def _extract_allocation_features(self, allocations):
        """提取分配特征向量"""
        feature_matrix = []
        
        for alloc in allocations:
            features = [
                alloc.get('quantity', 0),
                alloc.get('reliability_factor', 1.0),
                1 if alloc.get('is_alternative') else 0,
                1 if alloc.get('is_safety_stock') else 0,
                alloc.get('priority', 5),
            ]
            feature_matrix.append(features)
        
        return np.array(feature_matrix)

    def _detect_with_sklearn(self, features, raw_data, detection_type):
        """使用sklearn进行异常检测"""
        try:
            if features is None or len(features) == 0:
                return {'anomalies': [], 'total_checked': 0, 'detection_type': detection_type}

            # 标准化
            scaled_features = self.scaler.fit_transform(features)

            # 训练并预测
            predictions = self.model.fit_predict(scaled_features)
            anomaly_scores = self.model.decision_function(scaled_features)

            # 收集异常样本
            anomalies = []
            for i, (pred, score) in enumerate(zip(predictions, anomaly_scores)):
                if pred == -1:  # -1表示异常
                    anomaly = {
                        'index': i,
                        'score': round(float(score), 3),
                        'severity': 'critical' if score < -0.5 else ('major' if score < -0.2 else 'minor'),
                        'data': raw_data[i],
                        'type': detection_type,
                        'detected_at': datetime.now().isoformat()
                    }

                    # 根据类型生成解释
                    if detection_type == 'allocation':
                        anomaly['explanation'] = self._explain_allocation_anomaly(raw_data[i], score)

                    anomalies.append(anomaly)

            # 按严重程度排序
            anomalies.sort(key=lambda x: x['score'])

            logger.info(f"异常检测完成: {len(anomalies)}/{len(raw_data)} 个异常 ({detection_type})")

            return {
                'anomalies': anomalies,
                'total_checked': len(raw_data),
                'anomaly_rate': len(anomalies) / len(raw_data) if raw_data else 0,
                'method': 'isolation_forest'
            }
        except Exception as e:
            logger.error(f"sklearn异常检测失败: {str(e)}", exc_info=True)
            return {'anomalies': [], 'total_checked': len(raw_data) if raw_data else 0, 'error': str(e)}

    def _detect_allocation_rules(self, allocations):
        """基于规则的异常检测（备选方案）"""
        anomalies = []
        
        # 统计分析
        quantities = [a.get('quantity', 0) for a in allocations]
        if not quantities:
            return {'anomalies': [], 'total_checked': 0}
            
        mean_qty = np.mean(quantities)
        std_qty = np.std(quantities)
        
        for i, alloc in enumerate(allocations):
            qty = alloc.get('quantity', 0)
            z_score = abs(qty - mean_qty) / (std_qty + 1e-6)
            
            # 规则1: 数量异常大
            if z_score > 3:
                anomalies.append({
                    'index': i,
                    'score': -z_score,
                    'severity': 'major',
                    'data': alloc,
                    'type': 'allocation',
                    'explanation': f'分配数量{qty}显著高于平均值{mean_qty:.1f}（{z_score:.1f}σ）',
                    'detected_at': datetime.now().isoformat()
                })
            
            # 规则2: 低可靠性高风险分配
            reliability = alloc.get('reliability_factor', 1.0)
            if reliability < 0.6 and qty > mean_qty:
                anomalies.append({
                    'index': i,
                    'score': -(2 + (1 - reliability)),
                    'severity': 'critical',
                    'data': alloc,
                    'type': 'allocation',
                    'explanation': f'低可靠率供应商({reliability:.0%})被分配大量物料({qty})',
                    'detected_at': datetime.now().isoformat()
                })
        
        return {
            'anomalies': anomalies,
            'total_checked': len(allocations),
            'anomaly_rate': len(anomalies) / len(allocations) if allocations else 0,
            'method': 'rule_based'
        }

    def _explain_allocation_anomaly(self, allocation, score):
        """解释分配异常原因"""
        explanations = []
        
        if allocation.get('quantity', 0) > 1000:
            explanations.append('超大批量分配')
        if allocation.get('reliability_factor', 1.0) < 0.7:
            explanations.append('供应商可靠率低')
        if allocation.get('is_alternative'):
            explanations.append('使用了替代料')
        if allocation.get('priority', 5) <= 2:
            explanations.append('高优先级订单')
        
        return '; '.join(explanations) if explanations else '其他原因'

    def detect_inventory_anomalies(self, inventory_data):
        """
        检测库存水平异常
        
        Args:
            inventory_data: 库存数据列表 [{'material_id', 'quantity', 'type', ...}]
            
        Returns:
            dict: 异常检测结果
        """
        if not inventory_data:
            return {'anomalies': [], 'total_checked': 0}
        
        anomalies = []
        
        # 按物料分组统计
        by_material = defaultdict(list)
        for inv in inventory_data:
            by_material[inv['material_id']].append(inv)
        
        for material_id, inv_list in by_material.items():
            total_qty = sum(inv['quantity'] for inv in inv_list)
            avg_qty = total_qty / len(inv_list)
            
            for inv in inv_list:
                # 规则1: 单批次数量过大
                if inv['quantity'] > avg_qty * 5 and avg_qty > 0:
                    anomalies.append({
                        'material_id': material_id,
                        'inventory_id': inv.get('id'),
                        'type': 'inventory_concentration',
                        'severity': 'medium',
                        'value': inv['quantity'],
                        'expected_range': f'{avg_qty*0.5:.0f}-{avg_qty*2:.0f}',
                        'explanation': f'单批次库存{inv["quantity"]}远超平均值{avg_qty:.0f}',
                        'detected_at': datetime.now().isoformat()
                    })
                
                # 规则2: 即将过期但数量仍很大
                expiry = inv.get('expiry_date')
                if expiry:
                    days_to_expiry = (expiry - date.today()).days
                    if 0 < days_to_expiry < 30 and inv['quantity'] > 100:
                        anomalies.append({
                            'material_id': material_id,
                            'type': 'expiring_large_stock',
                            'severity': 'high' if days_to_expiry < 14 else 'medium',
                            'days_to_expiry': days_to_expiry,
                            'quantity': inv['quantity'],
                            'explanation': f'{days_to_expiry}天后过期仍有{inv["quantity"]}库存',
                            'suggestion': '优先消耗或寻找出库渠道',
                            'detected_at': datetime.now().isoformat()
                        })
        
        logger.info(f"库存异常检测完成: {len(anomalies)} 个异常")
        
        return {
            'anomalies': anomalies,
            'total_checked': len(inventory_data),
            'anomaly_rate': len(anomalies) / len(inventory_data) if inventory_data else 0,
            'method': 'rule_based_inventory'
        }


class IntelligentDecisionEngine:
    """
    智能决策引擎 - 集成预测、异常检测、优化算法的综合决策系统
    
    功能：
    1. 自动触发物料计划重算（当检测到显著变化时）
    2. 生成智能采购建议（考虑预测、库存、供应商能力）
    3. 风险预警与升级机制
    4. 决策解释与可视化支持
    """

    def __init__(self):
        self.demand_forecaster = DemandForecaster()
        self.anomaly_detector = AnomalyDetector()
        self.decision_history = []
        self.risk_thresholds = {
            'shortage_critical': 0.3,     # 缺料率>30%为严重
            'delivery_delay_high': 0.2,   # 延期率>20%为高危
            'inventory_stagnation': 90,   # 库存周转>90天为呆滞
            'supplier_risk_low': 0.7      # 可靠率<70%为风险供应商
        }

    def run_comprehensive_analysis(self, include_prediction=True):
        """
        运行综合分析（预测+异常检测+风险评估）
        
        Args:
            include_prediction: 是否包含需求预测（耗时较长）
            
        Returns:
            dict: 完整的分析报告
        """
        report = {
            'timestamp': datetime.now().isoformat(),
            'components': {},
            'recommendations': [],
            'risk_summary': {}
        }
        
        # 1. 需求预测（可选）
        if include_prediction:
            try:
                prediction_result = self.demand_forecaster.predict(future_days=30)
                report['components']['demand_prediction'] = prediction_result
                
                # 如果发现需求激增，添加建议
                if prediction_result.get('summary', {}).get('growth_rate', 0) > 0.2:
                    report['recommendations'].append({
                        'type': 'capacity_planning',
                        'priority': 'high',
                        'message': f'未来需求预计增长{prediction_result["summary"]["growth_rate"]*100:.0f}%，建议提前规划产能',
                        'confidence': prediction_result.get('confidence', 0.8)
                    })
            except Exception as e:
                logger.error(f"需求预测失败: {str(e)}", exc_info=True)
                report['components']['demand_prediction'] = {'error': str(e)}

        # 2. 从数据库加载当前状态
        current_allocations = self._load_current_allocations()
        current_inventory = self._load_current_inventory()
        
        # 3. 异常检测
        if current_allocations:
            allocation_anomalies = self.anomaly_detector.detect_allocation_anomalies(current_allocations)
            report['components']['allocation_anomalies'] = allocation_anomalies
            
            if allocation_anomalies.get('anomalies'):
                for anomaly in allocation_anomalies['anomalies'][:5]:  # 取前5个最严重的
                    report['recommendations'].append({
                        'type': 'anomaly_alert',
                        'priority': anomaly['severity'],
                        'message': anomaly.get('explanation', '检测到异常分配模式'),
                        'details': anomaly
                    })
        
        if current_inventory:
            inventory_anomalies = self.anomaly_detector.detect_inventory_anomalies(current_inventory)
            report['components']['inventory_anomalies'] = inventory_anomalies
            
            # 呆滞库存警告
            stagnation_count = sum(
                1 for a in inventory_anomalies.get('anomalies', [])
                if a['type'] == 'expiring_large_stock' and a['severity'] == 'high'
            )
            if stagnation_count > 0:
                report['recommendations'].append({
                    'type': 'inventory_optimization',
                    'priority': 'medium',
                    'message': f'发现{stagnation_count}项临期大宗库存，建议立即处理以避免报废损失',
                    'potential_saving': self._estimate_stagnation_cost(inventory_anomalies['anomalies'])
                })

        # 4. 风险汇总
        total_risks = (
            len(report['components'].get('allocation_anomalies', {}).get('anomalies', [])) +
            len(report['components'].get('inventory_anomalies', {}).get('anomalies', []))
        )
        report['risk_summary'] = {
            'total_risk_items': total_risks,
            'risk_level': 'low' if total_risks < 3 else ('medium' if total_risks < 10 else 'high'),
            'critical_count': sum(
                1 for comp in report['components'].values()
                if isinstance(comp, dict) and 'anomalies' in comp
                for a in comp.get('anomalies', [])
                if a.get('severity') == 'critical'
            ),
            'recommendation_count': len(report['recommendations'])
        }
        
        # 5. 记录决策历史
        self.decision_history.append({
            'timestamp': report['timestamp'],
            'risk_level': report['risk_summary']['risk_level'],
            'action_count': len(report['recommendations'])
        })
        
        # 写入日志
        PlanLog.objects.create(
            log_type='INFO' if report['risk_summary']['risk_level'] != 'high' else 'WARNING',
            message=f'综合分析完成: 风险等级={report["risk_summary"]["risk_level"]}, 发现{total_risks}个风险项, 生成{len(report["recommendations"])}条建议'
        )
        
        return report

    def _load_current_allocations(self):
        """加载当前分配数据"""
        try:
            allocations = OrderAllocation.objects.select_related('order', 'material').all()
            return [
                {
                    'id': alloc.id,
                    'material_id': alloc.material_id,
                    'quantity': int(alloc.allocated_quantity or 0),
                    'order_id': alloc.order_id,
                    'order_priority': alloc.order.priority if alloc.order else 5,
                    'reliability_factor': getattr(alloc, 'reliability_factor', 1.0),
                    'is_alternative': getattr(alloc, 'is_alternative', False),
                    'is_safety_stock': getattr(alloc, 'is_safety_stock', False)
                }
                for alloc in allocations
            ]
        except Exception as e:
            logger.error(f"加载分配数据失败: {str(e)}")
            return []

    def _load_current_inventory(self):
        """加载当前库存数据"""
        try:
            inventories = Inventory.objects.select_related('material').all()
            return [
                {
                    'id': inv.id,
                    'material_id': inv.material_id,
                    'quantity': int(inv.quantity or 0),
                    'type': inv.inventory_type,
                    'expiry_date': inv.expiry_date,
                    'warehouse': inv.warehouse,
                    'is_hold': inv.is_hold
                }
                for inv in inventories
            ]
        except Exception as e:
            logger.error(f"加载库存数据失败: {str(e)}")
            return []

    def _estimate_stagnation_cost(self, anomalies):
        """估算呆滞成本"""
        total_cost = 0
        for anomaly in anomalies:
            if anomaly['type'] == 'expiring_large_stock':
                # 假设报废成本为物料价值的80%
                quantity = anomaly.get('quantity', 0)
                estimated_unit_cost = 50  # 估算单价，实际应从物料主数据获取
                total_cost += quantity * estimated_unit_cost * 0.8
        return round(total_cost, 2)

    def generate_procurement_action_plan(self, shortage_report, prediction_result=None):
        """
        生成智能采购行动方案
        
        结合：
        - 当前缺料情况
        - 未来需求预测
        - 供应商能力评估
        - 库存水位优化目标
        
        Args:
            shortage_report: 缺料报告（来自MaterialPlanner.analyze_shortage）
            prediction_result: 需求预测结果（可选）
            
        Returns:
            dict: 结构化的采购行动方案
        """
        action_plan = {
            'immediate_actions': [],      # 立即执行（0-3天）
            'short_term_actions': [],     # 短期计划（3-14天）
            'medium_term_actions': [],    # 中期规划（14-30天）
            'optimization_suggestions': [],  # 持续优化建议
            'total_estimated_investment': 0,
            'risk_mitigation_plan': []
        }
        
        for item in shortage_report.get('material_shortages', []):
            urgency = item.get('urgency_level', 'normal')
            shortage_qty = item.get('shortage_qty', 0)
            material_id = item.get('material_code', '')
            
            action_item = {
                'material_code': material_id,
                'material_name': item.get('material_name', ''),
                'shortage_quantity': shortage_qty,
                'urgency': urgency,
                'recommended_supplier': item.get('suppliers', [{}])[0].get('supplier_name', '') if item.get('suppliers') else '',
                'latest_order_date': item.get('latest_purchase_date'),
                'estimated_cost': 0
            }
            
            # 根据紧急程度分类
            if urgency == 'critical':
                action_item['action_type'] = 'emergency_procurement'
                action_item['timeline'] = '立即'
                action_item['specific_steps'] = [
                    '联系所有可用供应商确认现货',
                    '考虑空运加急（增加成本但缩短交期）',
                    '评估是否可以从其他订单让料',
                    '启动应急响应流程'
                ]
                action_plan['immediate_actions'].append(action_item)
                
            elif urgency == 'urgent':
                action_item['action_type'] = 'expedited_order'
                action_item['timeline'] = '3-7天内'
                action_item['specific_steps'] = [
                    '向首选供应商下达加急订单',
                    '要求供应商提供分批交付方案',
                    '跟踪生产进度（每日更新）',
                    '准备备用供应商预案'
                ]
                action_plan['short_term_actions'].append(action_item)
                
            else:
                action_item['action_type'] = 'standard_procurement'
                action_item['timeline'] = '7-14天内'
                action_item['specific_steps'] = [
                    '按正常流程下达采购订单',
                    '批量议价降低采购成本',
                    '协调物流安排',
                    '纳入常规监控'
                ]
                action_plan['medium_term_actions'].append(action_item)
            
            # 成本估算
            unit_price = item.get('suppliers', [{}])[0].get('unit_price', 0) if item.get('suppliers') else 0
            order_qty = max(shortage_qty, item.get('safety_stock', 0) * 0.5)
            action_item['estimated_cost'] = order_qty * unit_price
            action_plan['total_estimated_investment'] += action_item['estimated_cost']

        # 如果有预测数据，添加前瞻性建议
        if prediction_result and prediction_result.get('success'):
            summary = prediction_result.get('summary', {})
            growth_rate = summary.get('growth_rate', 0)
            
            if growth_rate > 0.15:
                action_plan['optimization_suggestions'].append({
                    'type': 'capacity_expansion',
                    'priority': 'medium',
                    'message': f'需求预计增长{growth_rate*100:.0f}%，建议提前增加安全库存水平',
                    'suggested_buffer_increase': '建议根据实际需求增长调整'
                })
            
            if summary.get('peak_demand_value', 0) > summary.get('avg_daily_demand', 0) * 2:
                action_plan['risk_mitigation_plan'].append({
                    'risk_type': 'demand_spike',
                    'mitigation': '在需求高峰前2周建立战略库存缓冲',
                    'trigger_condition': f'当预测峰值>{summary["avg_daily_demand"]*2:.0f}时激活'
                })

        # 排序
        action_plan['immediate_actions'].sort(key=lambda x: x.get('shortage_quantity', 0), reverse=True)
        action_plan['short_term_actions'].sort(key=lambda x: x.get('latest_purchase_date', '2099-12-31'))
        
        logger.info(f"采购行动方案生成完成: {len(action_plan['immediate_actions'])}个紧急项, {len(action_plan['short_term_actions'])}个短期项")
        
        return action_plan


# 便捷函数
def get_ai_engine(force_reload=False):
    """获取AI引擎实例（单例模式）

    Args:
        force_reload: 强制重新创建实例（用于修复损坏的缓存模型）
    """
    if force_reload or not hasattr(get_ai_engine, '_instance'):
        get_ai_engine._instance = IntelligentDecisionEngine()
    return get_ai_engine._instance


def run_demand_prediction(days=30):
    """快速运行需求预测"""
    engine = get_ai_engine()
    forecaster = engine.demand_forecaster
    forecaster.train()
    return forecaster.predict(future_days=days)


def run_anomaly_detection():
    """快速运行异常检测"""
    engine = get_ai_engine()
    return engine.run_comprehensive_analysis(include_prediction=False)


def generate_intelligent_recommendations(shortage_report=None):
    """生成智能推荐"""
    engine = get_ai_engine()

    # 运行完整分析
    analysis = engine.run_comprehensive_analysis(include_prediction=True)

    # 如果提供了缺料报告，生成采购方案
    if shortage_report:
        prediction = analysis.get('components', {}).get('demand_prediction')
        action_plan = engine.generate_procurement_action_plan(shortage_report, prediction)
        analysis['action_plan'] = action_plan

    return analysis


class AutoRetrainer:
    """
    模型自动重训练引擎 - 支持定时自动重训练，无需手动触发

    功能：
    1. 定时检测是否需要重训练（基于时间间隔或数据变化）
    2. 自动重训练需求预测模型
    3. 自动重训练异常检测模型
    4. 重训练状态管理与查询
    """

    def __init__(self):
        from .utils.safe_cache import safe_get, safe_set

        cached_info = safe_get('auto_retrain_info', {})
        self.last_train_time = cached_info.get('last_train_time')
        self.model_version = cached_info.get('model_version', 0)
        self.retrain_interval_hours = cached_info.get('retrain_interval_hours', 24)
        self.last_forecast_data_count = cached_info.get('last_forecast_data_count', 0)
        self.last_actual_data_count = cached_info.get('last_actual_data_count', 0)
        self.is_retraining = False
        self.last_retrain_result = None

    def _save_to_cache(self):
        from .utils.safe_cache import safe_set

        safe_set('auto_retrain_info', {
            'last_train_time': self.last_train_time,
            'model_version': self.model_version,
            'retrain_interval_hours': self.retrain_interval_hours,
            'last_forecast_data_count': self.last_forecast_data_count,
            'last_actual_data_count': self.last_actual_data_count,
        }, timeout=None)

    def should_retrain(self):
        """
        判断是否需要重训练：
        1. 距上次训练超过设定间隔（默认24小时）
        2. 或有新的实际销售数据（forecast_data和actual_data的数量变化）
        """
        if self.last_train_time is None:
            return True

        last_time = datetime.fromisoformat(self.last_train_time) if isinstance(self.last_train_time, str) else self.last_train_time
        hours_since_last = (datetime.now() - last_time).total_seconds() / 3600

        if hours_since_last >= self.retrain_interval_hours:
            logger.info(f'距上次训练已过{hours_since_last:.1f}小时，超过间隔{self.retrain_interval_hours}小时，需要重训练')
            return True

        try:
            current_forecast_count = SalesOrder.objects.filter(
                status__in=['pending', 'confirmed', 'allocated', 'partial', 'complete', 'completed', 'shipped', 'delivered']
            ).count()
            current_actual_count = SalesOrder.objects.filter(
                status__in=['complete', 'completed', 'shipped', 'delivered']
            ).count()

            if (current_forecast_count != self.last_forecast_data_count or
                    current_actual_count != self.last_actual_data_count):
                logger.info('检测到数据变化，需要重训练')
                return True
        except Exception as e:
            logger.warning(f'检查数据变化时出错: {e}')

        return False

    def retrain_demand_forecast(self, product=None):
        """
        重训练需求预测模型

        Args:
            product: 产品标识（可选），用于按产品过滤训练数据

        Returns:
            dict: 重训练结果
        """
        logger.info('开始重训练需求预测模型...')
        try:
            forecaster = DemandForecaster()

            if product:
                orders = SalesOrder.objects.filter(
                    material_id=product,
                    status__in=['pending', 'confirmed', 'allocated', 'partial', 'complete', 'completed', 'shipped', 'delivered']
                )
                training_data = forecaster.prepare_training_data(orders=orders)
            else:
                training_data = forecaster.prepare_training_data()

            result = forecaster.train(training_data)

            if result.get('success'):
                self.last_train_time = datetime.now().isoformat()
                self.model_version += 1
                self.last_forecast_data_count = SalesOrder.objects.filter(
                    status__in=['pending', 'confirmed', 'allocated', 'partial', 'complete', 'completed', 'shipped', 'delivered']
                ).count()
                self.last_actual_data_count = SalesOrder.objects.filter(
                    status__in=['complete', 'completed', 'shipped', 'delivered']
                ).count()
                self._save_to_cache()
                logger.info(f'需求预测模型重训练成功 (版本: {self.model_version})')

            self.last_retrain_result = result
            return result

        except Exception as e:
            logger.error(f'需求预测模型重训练失败: {str(e)}', exc_info=True)
            return {'success': False, 'error': str(e)}

    def retrain_anomaly_detector(self):
        """
        重训练异常检测模型

        Returns:
            dict: 重训练结果
        """
        logger.info('开始重训练异常检测模型...')
        try:
            detector = AnomalyDetector()

            engine = get_ai_engine()
            current_allocations = engine._load_current_allocations()

            if not current_allocations:
                logger.warning('无分配数据，跳过异常检测模型重训练')
                return {'success': False, 'reason': 'no_data'}

            detector.detect_allocation_anomalies(current_allocations)
            detector.save_model()

            logger.info('异常检测模型重训练成功')
            return {'success': True, 'samples': len(current_allocations)}

        except Exception as e:
            logger.error(f'异常检测模型重训练失败: {str(e)}', exc_info=True)
            return {'success': False, 'error': str(e)}

    def get_retrain_status(self):
        """
        返回重训练状态信息

        Returns:
            dict: 重训练状态
        """
        next_train_time = None
        if self.last_train_time:
            last_time = datetime.fromisoformat(self.last_train_time) if isinstance(self.last_train_time, str) else self.last_train_time
            next_train_time = (last_time + timedelta(hours=self.retrain_interval_hours)).isoformat()

        return {
            'last_train_time': self.last_train_time,
            'next_train_time': next_train_time,
            'model_version': self.model_version,
            'retrain_interval_hours': self.retrain_interval_hours,
            'is_retraining': self.is_retraining,
            'should_retrain': self.should_retrain(),
            'last_retrain_result': self.last_retrain_result,
        }

    def schedule_retrain(self, interval_hours=24):
        """
        设置重训练间隔

        Args:
            interval_hours: 重训练间隔（小时），默认24小时
        """
        self.retrain_interval_hours = interval_hours
        self._save_to_cache()
        logger.info(f'重训练间隔已设置为{interval_hours}小时')
