"""数据验证工具类"""
from datetime import datetime, date
from decimal import Decimal
import re


class Validator:
    """数据验证器"""

    @staticmethod
    def is_valid_email(email):
        """验证邮箱格式"""
        if not email:
            return False
        pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
        return re.match(pattern, email) is not None

    @staticmethod
    def is_valid_phone(phone):
        """验证手机号码"""
        if not phone:
            return False
        pattern = r'^1[3-9]\d{9}$'
        return re.match(pattern, phone) is not None

    @staticmethod
    def is_valid_material_code(code):
        """验证物料编码格式"""
        if not code:
            return False
        pattern = r'^[A-Za-z0-9_-]{3,50}$'
        return re.match(pattern, code) is not None

    @staticmethod
    def is_valid_order_no(order_no):
        """验证订单编号格式"""
        if not order_no:
            return False
        pattern = r'^[A-Za-z0-9_-]{5,50}$'
        return re.match(pattern, order_no) is not None

    @staticmethod
    def is_valid_decimal(value):
        """验证是否为有效数字"""
        if value is None:
            return False
        try:
            Decimal(str(value))
            return True
        except (ValueError, TypeError):
            return False

    @staticmethod
    def is_positive(value):
        """验证是否为正数"""
        if not Validator.is_valid_decimal(value):
            return False
        return Decimal(str(value)) > 0

    @staticmethod
    def is_future_date(d):
        """验证是否为未来日期"""
        if isinstance(d, date):
            return d > date.today()
        return False

    @staticmethod
    def validate_order_data(data):
        """验证订单数据"""
        errors = []
        
        if 'order_no' not in data or not data['order_no']:
            errors.append('订单编号不能为空')
        elif not Validator.is_valid_order_no(data['order_no']):
            errors.append('订单编号格式不正确')
        
        if 'customer_name' not in data or not data['customer_name'].strip():
            errors.append('客户名称不能为空')
        
        if 'material' not in data or not data['material']:
            errors.append('物料不能为空')
        
        if 'quantity' not in data:
            errors.append('订单数量不能为空')
        elif not Validator.is_positive(data['quantity']):
            errors.append('订单数量必须为正数')
        
        if 'demand_date' not in data or not data['demand_date']:
            errors.append('需求日期不能为空')
        
        if 'priority' in data and (data['priority'] < 1 or data['priority'] > 10):
            errors.append('优先级必须在1-10之间')
        
        return errors

    @staticmethod
    def validate_inventory_data(data):
        """验证库存数据"""
        errors = []
        
        if 'material' not in data or not data['material']:
            errors.append('物料不能为空')
        
        if 'quantity' not in data:
            errors.append('库存数量不能为空')
        elif not Validator.is_valid_decimal(data['quantity']):
            errors.append('库存数量必须为有效数字')
        
        if 'warehouse' in data and len(data['warehouse']) > 50:
            errors.append('仓库名称不能超过50个字符')
        
        if 'batch_no' in data and len(data['batch_no']) > 50:
            errors.append('批次号不能超过50个字符')
        
        return errors


class BusinessRuleEngine:
    """业务规则引擎"""

    @staticmethod
    def check_order_priority(order):
        """检查订单优先级是否符合规则"""
        if order.priority < 1 or order.priority > 10:
            return False, '优先级必须在1-10之间'
        return True, None

    @staticmethod
    def check_inventory_level(inventory):
        """检查库存水平是否符合安全库存要求"""
        safety_stock = inventory.material.safety_stock if inventory.material else 0
        if safety_stock and float(inventory.quantity or 0) < float(safety_stock):
            return False, f'库存数量({inventory.quantity})低于安全库存({safety_stock})'
        return True, None

    @staticmethod
    def check_order_delivery(order):
        """检查订单是否能按时交付"""
        required_date = order.demand_date - datetime.timedelta(days=order.shipping_days + 2)
        if required_date < date.today():
            return False, f'订单{order.order_no}的需求日期过于紧迫，可能无法按时交付'
        return True, None

    @staticmethod
    def check_supplier_lead_time(supplier_commitment):
        """检查供应商交货承诺是否合理"""
        if supplier_commitment.delivery_date < date.today():
            return False, f'供应商承诺交货日期已过期'
        return True, None

    @staticmethod
    def validate_all_rules(order=None, inventory=None, commitment=None):
        """验证所有业务规则"""
        errors = []
        
        if order:
            valid, msg = BusinessRuleEngine.check_order_priority(order)
            if not valid:
                errors.append(msg)
            valid, msg = BusinessRuleEngine.check_order_delivery(order)
            if not valid:
                errors.append(msg)
        
        if inventory:
            valid, msg = BusinessRuleEngine.check_inventory_level(inventory)
            if not valid:
                errors.append(msg)
        
        if commitment:
            valid, msg = BusinessRuleEngine.check_supplier_lead_time(commitment)
            if not valid:
                errors.append(msg)
        
        return errors