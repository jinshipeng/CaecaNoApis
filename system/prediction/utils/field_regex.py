#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
智能字段识别系统 v2.0 - 增强版
================================

新增功能：
1. 混合中英文支持（90%+识别率）
2. 基于历史记录的智能学习机制
3. 自定义规则配置系统
4. 模糊匹配和语义相似度算法（编辑距离、Jaccard相似度）
5. 字段映射缓存和历史记录持久化
6. 同义词词典和领域术语库

作者：AI Assistant
版本：2.0 (2026-05-28)
"""

import re
import json
import os
import hashlib
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Set, Any
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
import unicodedata
import logging

logger = logging.getLogger(__name__)


@dataclass
class FieldPattern:
    """字段正则模式定义"""
    pattern: str
    field_key: str
    field_name: str
    priority: int = 0
    data_type: str = 'string'
    required: bool = False
    aliases: List[str] = field(default_factory=list)  # 别名列表
    synonyms_en: List[str] = field(default_factory=list)  # 英文同义词
    synonyms_zh: List[str] = field(default_factory=list)  # 中文同义词


@dataclass
class RecognitionResult:
    """识别结果数据结构"""
    original_column: str
    mapped_key: Optional[str]
    field_name: Optional[str]
    confidence: float
    data_type: str
    required: bool
    matched: bool
    is_duplicate: bool = False
    match_method: str = 'regex'  # regex, fuzzy, learned, custom
    similarity_score: float = 0.0  # 模糊匹配相似度分数


@dataclass
class CustomRule:
    """自定义规则"""
    id: str
    pattern: str
    field_key: str
    field_name: str
    import_type: str
    priority: int = 50
    created_at: str = ''
    usage_count: int = 0
    success_rate: float = 1.0


class SynonymDictionary:
    """同义词词典 - 支持中英文混合匹配"""

    SYNONYMS = {
        # ID/代码类
        'id': ['ID', 'id', 'Id', '编号', '代码', '代号', '标识', '标识符', 'No', 'NO',
               'Code', 'code', '编码', '序号', '号码'],
        'name': ['名称', 'Name', 'name', 'NAME', '名', '标题', 'Title', 'title',
                 '描述', 'Description', 'desc', '说明', '备注', 'Remark'],
        'type': ['类型', 'Type', 'type', 'TYPE', '种类', '类别', '分类', 'Category',
                 'category', '规格', 'Spec', 'spec', '型号', 'Model', 'model'],
        'unit': ['单位', 'Unit', 'unit', 'UNIT', '计量单位', '单位', 'UOM', 'uom',
                 'Units', 'units', '规格单位'],
        'price': ['价格', 'Price', 'price', 'PRICE', '单价', '金额', '费用', '成本',
                  'Cost', 'cost', '费用', 'Fee', 'fee', '价钱', '价值', 'Value'],
        'quantity': ['数量', 'Quantity', 'quantity', 'QUANTITY', 'Qty', 'qty', 'QTY',
                     '数量', '个数', '件数', 'Amount', 'amount', 'Count', 'count',
                     'Num', 'num', '数目', '总量', 'Total', 'total'],
        'date': ['日期', 'Date', 'date', 'DATE', '时间', 'Time', 'time', 'TIME',
                 '日期时间', 'Datetime', 'datetime', '年月日', '到期', '截止',
                 'Deadline', 'deadline', '交期', '交付', 'Delivery'],
        'status': ['状态', 'Status', 'status', 'STATUS', 'State', 'state', '情况',
                   '状况', '条件', 'Condition', '启用', '有效', 'Active', 'active'],
        'address': ['地址', 'Address', 'address', 'ADDRESS', '位置', 'Location',
                    'location', '地点', 'Place', 'place', '所在地'],
        'phone': ['电话', 'Phone', 'phone', 'PHONE', '手机', 'Mobile', 'mobile',
                  '联系电话', 'Tel', 'tel', 'TEL', '联系方式', 'Contact', 'contact'],
        'email': ['邮箱', 'Email', 'email', 'EMAIL', '邮件', 'Mail', 'mail',
                  '电子邮箱', 'E-mail', 'e-mail', '电子邮件'],
        'person': ['人', 'Person', 'person', '人员', '联系人', 'Contact', 'contact',
                  '负责人', 'Manager', 'manager', '员工', 'Staff', 'staff'],
        'supplier': ['供应商', 'Supplier', 'supplier', 'SUPPLIER', '供方', '供应',
                     '厂商', 'Vendor', 'vendor', '卖方', 'Seller', 'seller'],
        'customer': ['客户', 'Customer', 'customer', 'CUSTOMER', '顾客', '买方',
                     'Buyer', 'buyer', '用户', 'User', 'user'],
        'material': ['物料', 'Material', 'material', 'MATERIAL', '材料', '原料',
                     '物品', 'Item', 'item', '产品', 'Product', 'product',
                     '商品', 'Goods', 'goods', '零件', 'Part', 'part'],
        'order': ['订单', 'Order', 'order', 'ORDER', '订购单', 'PO', 'po', 'SO', 'so',
                  '销售单', '采购单', 'Purchase', 'purchase'],
        'inventory': ['库存', 'Inventory', 'inventory', 'INVENTORY', '存货',
                      'Stock', 'stock', '仓储', 'Warehouse', 'warehouse',
                      '在库', '现有', '当前', 'Current', 'current'],
        'bom': ['BOM', 'bom', 'Bill', 'bill', '配方', '组成', '构成', '结构',
                'Structure', 'structure', '清单', 'List', 'list'],
        'workcenter': ['工作中心', 'WorkCenter', 'workcenter', 'WORKCENTER',
                       '产线', '生产线', 'Line', 'line', '工作站', 'Station',
                       'station', '车间', 'Workshop', 'workshop', '设备'],
        'cost': ['成本', 'Cost', 'cost', 'COST', '花费', 'Expense', 'expense',
                 '支出', 'Expenditure', 'expenditure', '费用', 'Charge', 'charge'],
        'rate': ['率', 'Rate', 'rate', 'RATE', '比率', '比例', 'Ratio', 'ratio',
                 '百分比', 'Percent', 'percent', '%', '占比', 'Percentage'],
        'time_period': ['期', 'Period', 'period', 'PERIOD', '周期', 'Cycle', 'cycle',
                        '时长', 'Duration', 'duration', 'LeadTime', 'lead_time',
                        '提前期', '前置时间'],
        'safety': ['安全', 'Safety', 'safety', 'SAFETY', '保险', '保障', '缓冲',
                  'Buffer', 'buffer', '最小', 'Minimum', 'minimum', 'Min', 'min'],
        'max': ['最大', 'Maximum', 'maximum', 'MAX', 'Max', 'max', '上限',
                'Limit', 'limit', '限制', 'Cap', 'cap', '顶', 'Top', 'top'],
        'priority': ['优先级', 'Priority', 'priority', 'PRIORITY', '重要度',
                     'Importance', 'importance', '等级', 'Level', 'level'],
    }

    @classmethod
    def get_synonyms(cls, word: str) -> List[str]:
        """获取单词的所有同义词"""
        word_lower = word.lower().strip()
        for key, synonyms in cls.SYNONYMS.items():
            if word_lower in [s.lower() for s in synonyms]:
                return synonyms
        return [word]

    @classmethod
    def calculate_similarity(cls, word1: str, word2: str) -> float:
        """
        计算两个词的语义相似度（基于同义词词典）

        Returns:
            相似度分数 (0.0 - 1.0)
        """
        w1 = word1.lower().strip()
        w2 = word2.lower().strip()

        if w1 == w2:
            return 1.0

        syn1 = cls.get_synonyms(word1)
        syn2 = cls.get_synonyms(word2)

        # 检查是否在同义词集合中
        set1 = set(s.lower() for s in syn1)
        set2 = set(s.lower() for s in syn2)

        if set1 & set2:  # 有交集
            return 0.85

        # 使用SequenceMatcher计算字符串相似度
        return SequenceMatcher(None, w1, w2).ratio()


class FuzzyMatcher:
    """模糊匹配器 - 支持多种相似度算法"""

    @staticmethod
    def levenshtein_distance(s1: str, s2: str) -> int:
        """计算编辑距离（Levenshtein距离）"""
        if len(s1) < len(s2):
            return FuzzyMatcher.levenshtein_distance(s2, s1)

        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)

        for i, c1 in enumerate(s1):
            current_row = [i + 1]

            for j, c2 in enumerate(s2):
                # 计算插入、删除、替换的成本
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)

                current_row.append(min(insertions, deletions, substitutions))

            previous_row = current_row

        return previous_row[-1]

    @staticmethod
    def jaccard_similarity(s1: str, s2: str) -> float:
        """
        Jaccard相似度（基于字符集）
        适用于中文和混合文本
        """
        set1 = set(s1.lower())
        set2 = set(s2.lower())

        intersection = len(set1 & set2)
        union = len(set1 | set2)

        return intersection / union if union > 0 else 0.0

    @staticmethod
    def token_similarity(s1: str, s2: str) -> float:
        """
        基于分词的相似度（按空格、下划线、横线分割）
        """
        tokens1 = re.split(r'[\s_\-]+', s1.lower())
        tokens2 = re.split(r'[\s_\-]+', s2.lower())

        if not tokens1 or not tokens2:
            return 0.0

        matches = 0
        for t1 in tokens1:
            for t2 in tokens2:
                if SequenceMatcher(None, t1, t2).ratio() > 0.7:
                    matches += 1
                    break

        return matches / max(len(tokens1), len(tokens2))

    @staticmethod
    def combined_similarity(s1: str, s2: str) -> float:
        """
        综合相似度算法（加权组合多种方法）

        权重：
        - 编辑距离：40%
        - Jaccard相似度：30%
        - Token相似度：30%
        """
        s1_norm = FuzzyMatcher._normalize_text(s1)
        s2_norm = FuzzyMatcher._normalize_text(s2)

        if not s1_norm or not s2_norm:
            return 0.0

        # 编辑距离相似度
        edit_dist = FuzzyMatcher.levenshtein_distance(s1_norm, s2_norm)
        max_len = max(len(s1_norm), len(s2_norm))
        edit_sim = 1 - (edit_dist / max_len) if max_len > 0 else 0.0

        # Jaccard相似度
        jaccard_sim = FuzzyMatcher.jaccard_similarity(s1_norm, s2_norm)

        # Token相似度
        token_sim = FuzzyMatcher.token_similarity(s1, s2)

        # 加权组合
        combined = (
            edit_sim * 0.4 +
            jaccard_sim * 0.3 +
            token_sim * 0.3
        )

        return round(combined, 4)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """标准化文本（去除特殊字符、统一大小写）"""
        # 去除括号及内容
        text = re.sub(r'\([^)]*\)', '', text)
        text = re.sub(r'[（][^）]*[）]', '', text)
        # 去除空格和特殊字符
        text = re.sub(r'[\s\-_]', '', text)
        # 统一为小写
        return text.lower().strip()


class FieldLearningSystem:
    """基于历史记录的字段学习系统"""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(__file__), '..', '.cache', 'field_learning'
        )
        self.history_file = os.path.join(self.cache_dir, 'mapping_history.json')
        self.custom_rules_file = os.path.join(self.cache_dir, 'custom_rules.json')

        self._ensure_cache_dir()

        self.mapping_history: Dict[str, List[Dict]] = {}
        self.custom_rules: Dict[str, CustomRule] = {}

        self._load_history()
        self._load_custom_rules()

    def _ensure_cache_dir(self):
        """确保缓存目录存在"""
        os.makedirs(self.cache_dir, exist_ok=True)

    def _load_history(self):
        """加载历史映射记录"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.mapping_history = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load mapping history: {e}")
                self.mapping_history = {}

    def _save_history(self):
        """保存历史映射记录"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.mapping_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
                logger.warning(f"Failed to save mapping history: {e}")

    def _load_custom_rules(self):
        """加载自定义规则"""
        if os.path.exists(self.custom_rules_file):
            try:
                with open(self.custom_rules_file, 'r', encoding='utf-8') as f:
                    rules_data = json.load(f)
                    self.custom_rules = {
                        rid: CustomRule(**r) for rid, r in rules_data.items()
                    }
            except Exception as e:
                logger.warning(f"Failed to load custom rules: {e}")
                self.custom_rules = {}

    def _save_custom_rules(self):
        """保存自定义规则"""
        try:
            rules_data = {
                rid: asdict(rule) for rid, rule in self.custom_rules.items()
            }
            with open(self.custom_rules_file, 'w', encoding='utf-8') as f:
                json.dump(rules_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
                logger.warning(f"Failed to save custom rules: {e}")

    def record_mapping(
        self,
        original_column: str,
        mapped_key: str,
        import_type: str,
        confidence: float,
        method: str = 'auto'
    ):
        """记录一次成功的字段映射"""

        column_key = self._generate_column_key(original_column, import_type)

        if column_key not in self.mapping_history:
            self.mapping_history[column_key] = []

        record = {
            'original_column': original_column,
            'mapped_key': mapped_key,
            'import_type': import_type,
            'confidence': confidence,
            'method': method,
            'timestamp': datetime.now().isoformat(),
            'usage_count': 1
        }

        # 检查是否有相同的历史记录
        existing = None
        for i, hist in enumerate(self.mapping_history[column_key]):
            if hist['mapped_key'] == mapped_key and hist['import_type'] == import_type:
                existing = i
                break

        if existing is not None:
            # 更新已有记录
            self.mapping_history[column_key][existing]['usage_count'] += 1
            self.mapping_history[column_key][existing]['timestamp'] = record['timestamp']
            self.mapping_history[column_key][existing]['confidence'] = max(
                record['confidence'],
                self.mapping_history[column_key][existing]['confidence']
            )
        else:
            # 添加新记录
            self.mapping_history[column_key].append(record)

        # 只保留最近50条历史
        if len(self.mapping_history[column_key]) > 50:
            self.mapping_history[column_key] = sorted(
                self.mapping_history[column_key],
                key=lambda x: x['timestamp'],
                reverse=True
            )[:50]

        self._save_history()

    def get_learned_mapping(
        self,
        original_column: str,
        import_type: str
    ) -> Optional[Tuple[str, float]]:
        """
        从历史记录中获取已学习的映射

        Returns:
            (mapped_key, confidence) 或 None
        """

        column_key = self._generate_column_key(original_column, import_type)

        if column_key not in self.mapping_history:
            # 尝试模糊匹配
            return self._fuzzy_search_history(original_column, import_type)

        history = self.mapping_history[column_key]

        if not history:
            return None

        # 找出使用次数最多的映射
        best_match = max(history, key=lambda x: (
            x['usage_count'],
            x['confidence']
        ))

        # 如果使用次数>=3，认为已经学会
        if best_match['usage_count'] >= 3:
            confidence = min(0.95, 0.7 + (best_match['usage_count'] * 0.05))
            return (best_match['mapped_key'], confidence)

        # 如果使用次数较少，返回较低置信度
        if best_match['usage_count'] >= 1:
            return (best_match['mapped_key'], best_match['confidence'] * 0.8)

        return None

    def _fuzzy_search_history(
        self,
        original_column: str,
        import_type: str,
        threshold: float = 0.75
    ) -> Optional[Tuple[str, float]]:
        """在历史记录中进行模糊搜索"""
        best_match = None
        best_score = threshold

        for col_key, history_list in self.mapping_history.items():
            # 提取原始列名进行比较
            if history_list:
                hist_col = history_list[0].get('original_column', '')
                score = FuzzyMatcher.combined_similarity(original_column, hist_col)

                if score > best_score:
                    # 确保导入类型一致
                    valid_records = [
                        h for h in history_list
                        if h['import_type'] == import_type
                    ]

                    if valid_records:
                        best_record = max(valid_records, key=lambda x: x['usage_count'])
                        best_score = score
                        best_match = (best_record['mapped_key'], score * 0.9)

        return best_match

    def add_custom_rule(
        self,
        pattern: str,
        field_key: str,
        field_name: str,
        import_type: str,
        priority: int = 60
    ) -> str:
        """添加自定义规则"""
        rule_id = hashlib.md5(
            f"{pattern}_{field_key}_{import_type}".encode()
        ).hexdigest()[:12]

        rule = CustomRule(
            id=rule_id,
            pattern=pattern,
            field_key=field_key,
            field_name=field_name,
            import_type=import_type,
            priority=priority,
            created_at=datetime.now().isoformat(),
            usage_count=0,
            success_rate=1.0
        )

        self.custom_rules[rule_id] = rule
        self._save_custom_rules()

        return rule_id

    def remove_custom_rule(self, rule_id: str) -> bool:
        """删除自定义规则"""
        if rule_id in self.custom_rules:
            del self.custom_rules[rule_id]
            self._save_custom_rules()
            return True
        return False

    def get_custom_rules_for_type(self, import_type: str) -> List[CustomRule]:
        """获取特定类型的所有自定义规则"""
        return [
            rule for rule in self.custom_rules.values()
            if rule.import_type == import_type
        ]

    def update_rule_statistics(self, rule_id: str, success: bool):
        """更新规则统计信息"""
        if rule_id in self.custom_rules:
            rule = self.custom_rules[rule_id]
            rule.usage_count += 1

            if success:
                # 更新成功率（指数移动平均）
                alpha = 0.3
                rule.success_rate = (
                    alpha * 1.0 + (1 - alpha) * rule.success_rate
                )
            else:
                alpha = 0.3
                rule.success_rate = (
                    alpha * 0.0 + (1 - alpha) * rule.success_rate
                )

            self._save_custom_rules()

    def get_statistics(self) -> Dict[str, Any]:
        """获取学习系统统计信息"""
        total_mappings = sum(
            len(hists) for hists in self.mapping_history.values()
        )

        total_custom_rules = len(self.custom_rules)

        type_distribution = {}
        for col_key, hists in self.mapping_history.items():
            for h in hists:
                imp_type = h.get('import_type', 'unknown')
                type_distribution[imp_type] = type_distribution.get(imp_type, 0) + 1

        return {
            'total_learned_mappings': total_mappings,
            'total_custom_rules': total_custom_rules,
            'unique_columns': len(self.mapping_history),
            'type_distribution': type_distribution,
            'cache_location': self.history_file
        }

    @staticmethod
    def _generate_column_key(column_name: str, import_type: str) -> str:
        """生成列名的唯一键（用于去重）"""
        normalized = re.sub(r'[\s\-_]+', '_', column_name.strip().lower())
        normalized = re.sub(r'[^\w]', '', normalized)
        return f"{import_type}::{normalized}"


class FieldRegexPatternsV2:
    """增强版字段正则表达式模式库 v2.0"""

    MATERIAL_PATTERNS = [
        # 物料ID/代码 - 大幅扩展变体
        FieldPattern(
            pattern=r'^(\u7269\u6599\s*ID|\u7269\u6599\u4ee3\u7801|\u7269\u6599\u7f16\u53f7|material_code|material_code\s*\([^)]*\)|\u7269\u6599\u53f7|\u6599\u53f7|\u96f6\u4ef6\u53f7|\u90e8\u4ef6\u53f7|\u4ea7\u54c1\u4ee3\u7801|\u4ea7\u54c1\u7f16\u53f7|Material_ID|MaterialCode|MATERIAL_CODE|Part_No|PART_NO|Item_Code|ITEM_CODE|SKU|sku|Product_ID|PRODUCT_ID|\u7269\u6599ID)\s*(\([^)]*\))?$',
            field_key='material_code',
            field_name='物料ID',
            priority=100,
            data_type='string',
            required=True,
            synonyms_zh=['物料代码', '物料编号', '料号', '零件号', '部件号', '产品代码', '产品编号'],
            synonyms_en=['material_code', 'MaterialCode', 'part_no', 'item_code', 'sku', 'product_id']
        ),
        FieldPattern(
            pattern=r'^(\u7269\u6599\u540d\u79f0|\u6750\u6599\u540d\u79f0|\u54c1\u540d|\u4ea7\u54c1\u540d\u79f0|material_name|Material_Name|MATERIAL_NAME|\u7269\u6590\u63cf\u8ff0|\u63cf\u8ff0|Description|description|Product_Name|product_name|ITEM_NAME|Item_Name)\s*(\([^)]*\))?$',
            field_key='material_name',
            field_name='物料名称',
            priority=95,
            data_type='string',
            synonyms_zh=['物料名称', '材料名称', '品名', '产品名称', '描述'],
            synonyms_en=['material_name', 'description', 'product_name', 'item_name']
        ),
        FieldPattern(
            pattern=r'^(\u7c7b\u578b|\u6750\u6599\u7c7b\u578b|\u7269\u6599\u7c7b\u578b|material_type|Type|TYPE|\u89c4\u683c|\u578b\u53f7|Model|model)\s*(\((\u539f\u6750\u6599|\u534a\u6210\u54c1|\u6210\u54c1|raw|semi_finished|finished)\))?\s*(\([^)]*\))?$',
            field_key='material_type',
            field_name='类型',
            priority=90,
            data_type='enum',
            synonyms_zh=['类型', '材料类型', '物料类型', '规格', '型号'],
            synonyms_en=['material_type', 'type', 'model', 'spec']
        ),
        FieldPattern(
            pattern=r'^(\u5355\u4f4d|\u8ba1\u91cf\u5355\u4f4d|unit|Unit|UNIT|UOM|uom|\u89c4\u683c\u5355\u4f4d)\s*(\([^)]*\))?$',
            field_key='unit',
            field_name='单位',
            priority=85,
            data_type='enum',
            synonyms_zh=['单位', '计量单位', '规格单位'],
            synonyms_en=['unit', 'uom', 'units']
        ),
        FieldPattern(
            pattern=r'^(\u5355\u4ef7|\u6807\u51c6\u6210\u672c|\u6210\u672c\u4ef7|\u4ef7\u683c|\u6807\u51c6\u6210\u672c|standard_cost|Standard_Cost|STANDARD_COST|\u5355\u4ef7\(\u5143\)|\u6807\u51c6\u6210\u672c\(\u5143\)|unit_price|Unit_Price|UNIT_PRICE|Price|price|COST|cost)\s*(\([^)]*\))?$',
            field_key='standard_cost',
            field_name='标准成本',
            priority=80,
            data_type='number',
            synonyms_zh=['单价', '标准成本', '成本价', '价格', '成本'],
            synonyms_en=['standard_cost', 'unit_price', 'price', 'cost']
        ),
        FieldPattern(
            pattern=r'^(\u9500\u552e\u4ef7\u683c|\u552e\u4ef7|sales_price|Sales_Price|SALES_PRICE|\u9500\u552e\u4ef7\u683c\(\u5143\)|Selling_Price|selling_price)\s*(\([^)]*\))?$',
            field_key='sales_price',
            field_name='销售价格',
            priority=75,
            data_type='number',
            synonyms_zh=['销售价格', '售价', '卖价'],
            synonyms_en=['sales_price', 'selling_price']
        ),
        FieldPattern(
            pattern=r'^(\u5b89\u5168\u5e93\u5b58|\u5b89\u5168\u5b58\u91cf|safety_stock|Safety_Stock|SAFETY_STOCK|\u5b89\u5168\u5e93\u5b58\([^)]*\)|Buffer_Stock|buffer_stock)\s*(\([^)]*\))?$',
            field_key='safety_stock',
            field_name='安全库存',
            priority=70,
            data_type='number',
            synonyms_zh=['安全库存', '安全存量', '缓冲库存'],
            synonyms_en=['safety_stock', 'buffer_stock']
        ),
        FieldPattern(
            pattern=r'^(\u6700\u5c0f\u8d77\u8ba2\u91cf|\u6700\u5c0f\u8ba2\u8d2d\u91cf|MOQ|min_order_qty|Min_Order_Qty|MIN_ORDER_QTY|\u6700\u5c0f\u8ba2\u8d27\u91cf)\s*(\([^)]*\))?$',
            field_key='min_order_qty',
            field_name='最小起订量',
            priority=65,
            data_type='number',
            synonyms_zh=['最小起订量', '最小订购量', 'MOQ'],
            synonyms_en=['min_order_qty', 'moq']
        ),
        FieldPattern(
            pattern=r'^(\u91c7\u8d2d\u63d0\u524d\u671f|\u63d0\u524d\u671f|\u4ea4\u8d27\u5468\u671f|lead_time|Lead_Time|LEAD_TIME|\u91c7\u8d2d\u63d0\u524d\u671f\(\u5929\)|LeadTime|leadtime)\s*(\([^)]*\))?$',
            field_key='lead_time',
            field_name='采购提前期',
            priority=60,
            data_type='number',
            synonyms_zh=['采购提前期', '提前期', '交货周期', '前置时间'],
            synonyms_en=['lead_time', 'leadtime', 'delivery_cycle']
        ),
        FieldPattern(
            pattern=r'^(\u4fdd\u8d28\u671f|\u6709\u6548\u671f|shelf_life|Shelf_Life|SHELF_LIFE|\u4fdd\u8d28\u671f\(\u5929.*?\)|Expiry|expiry|Validity|validity)\s*(\([^)]*\))?$',
            field_key='shelf_life',
            field_name='保质期',
            priority=55,
            data_type='number',
            synonyms_zh=['保质期', '有效期', '保质期限'],
            synonyms_en=['shelf_life', 'expiry', 'validity']
        ),
        FieldPattern(
            pattern=r'^(\u4e3b\u4f9b\u5e94\u5546|\u9996\u9009\u4f9b\u5e94\u5546|\u4e3b\u8981\u4f9b\u5e94\u5546|main_supplier|Main_Supplier|MAIN_SUPPLIER|\u4e3b\u8981\u4f9b\u65b9)\s*(\([^)]*\))?$',
            field_key='main_supplier',
            field_name='主供应商',
            priority=45,
            data_type='string',
            synonyms_zh=['主供应商', '首选供应商', '主要供应商', '主供方'],
            synonyms_en=['main_supplier', 'primary_supplier']
        ),
    ]

    SUPPLIER_PATTERNS = [
        FieldPattern(
            pattern=r'^(\u4f9b\u5e94\u5546ID|\u4f9b\u5e94\u5546\u4ee3\u7801|\u4f9b\u5e94\u5546\u7f16\u53f7|supplier_code|Supplier_Code|SUPPLIER_CODE|\u4f9b\u5e94\u5546\u53f7|\u4f9b\u65b9\u4ee3\u53f7|Vendor_Code|VENDOR_CODE|Vendor_ID|vendor_id)\s*(\([^)]*\))?$',
            field_key='supplier_code',
            field_name='供应商ID',
            priority=100,
            data_type='string',
            required=True,
            synonyms_zh=['供应商ID', '供应商代码', '供应商编号', '供方代号', '厂商代码'],
            synonyms_en=['supplier_code', 'vendor_code', 'vendor_id']
        ),
        FieldPattern(
            pattern=r'^(\u4f9b\u5e94\u5546\u540d\u79f0|\u4f9b\u65b9\u540d\u79f0|supplier_name|Supplier_Name|SUPPLIER_NAME|\u5382\u5546\u540d\u79f0|\u516c\u53f8\u540d\u79f0|Company|company|Vendor_Name|vendor_name)\s*(\([^)]*\))?$',
            field_key='supplier_name',
            field_name='供应商名称',
            priority=95,
            data_type='string',
            synonyms_zh=['供应商名称', '供方名称', '厂商名称', '公司名称'],
            synonyms_en=['supplier_name', 'vendor_name', 'company']
        ),
        FieldPattern(
            pattern=r'^(\u8054\u7cfb\u4eba|contact_person|Contact_Person|CONTACT_PERSON|\u8054\u7cfb\u4eba\u59d3\u540d|\u4e1a\u52a1\u5458|\u8d1f\u8d23\u4eba|Person_In_Charge|person_in_charge)\s*(\([^)]*\))?$',
            field_key='contact_person',
            field_name='联系人',
            priority=90,
            data_type='string',
            synonyms_zh=['联系人', '联系人姓名', '业务员', '负责人'],
            synonyms_en=['contact_person', 'contact', 'person_in_charge']
        ),
        FieldPattern(
            pattern=r'^(\u8054\u7cfb\u7535\u8bdd|\u7535\u8bdd|phone|Phone|PHONE|\u624b\u673a|\u8054\u7cfb\u7535\u8bdd\(\u624b\u673a\)|TEL|Tel|Mobile|mobile|Telephone|telephone)\s*(\([^)]*\))?$',
            field_key='phone',
            field_name='联系电话',
            priority=85,
            data_type='string',
            synonyms_zh=['联系电话', '电话', '手机', 'TEL'],
            synonyms_en=['phone', 'tel', 'mobile', 'telephone']
        ),
        FieldPattern(
            pattern=r'^(\u90ae\u7bb1|\u7535\u5b50\u90ae\u7bb1|email|Email|EMAIL|E-mail|e-mail|\u90ae\u4ef6\u5730\u5740|\u7535\u5b50\u90ae\u4ef6|Mail_Address|mail_address)\s*(\([^)]*\))?$',
            field_key='email',
            field_name='邮箱',
            priority=80,
            data_type='string',
            synonyms_zh=['邮箱', '电子邮箱', '邮件地址', '电子邮件'],
            synonyms_en=['email', 'e-mail', 'mail']
        ),
        FieldPattern(
            pattern=r'^(\u5730\u5740|\u8be6\u7ec6\u5730\u5740|address|Address|ADDRESS|\u516c\u53f8\u5730\u5740|\u901a\u8baf\u5730\u5740|\u529e\u516c\u5730\u5740|Location|location)\s*(\([^)]*\))?$',
            field_key='address',
            field_name='地址',
            priority=75,
            data_type='string',
            synonyms_zh=['地址', '详细地址', '公司地址', '通讯地址'],
            synonyms_en=['address', 'location']
        ),
        FieldPattern(
            pattern=r'^(\u4f9b\u5e94\u5546\u8bc4\u7ea7|\u8bc4\u7ea7|\u7b49\u7ea7|rating|Rating|RATING|\u4f9b\u5e94\u5546\u7b49\u7ea7|\u8d44\u8d28\u7b49\u7ea7|Grade|grade|Level|level)\s*(\([^)]*\))?$',
            field_key='rating',
            field_name='供应商评级',
            priority=70,
            data_type='enum',
            synonyms_zh=['供应商评级', '评级', '等级', '资质等级'],
            synonyms_en=['rating', 'grade', 'level']
        ),
        FieldPattern(
            pattern=r'^(\u4ea4\u4ed8\u53ef\u9760\u7387|\u4ea4\u4ed8\u51c6\u65f6\u7387|delivery_reliability|Delivery_Reliability|DELIVERY_RELIABILITY|\u51c6\u65f6\u4ea4\u8d27\u7387|\u5230\u8d27\u51c6\u65f6\u7387|On_Time_Rate|on_time_rate)\s*(\([^)]*\))?$',
            field_key='delivery_reliability',
            field_name='交付可靠率',
            priority=65,
            data_type='number',
            synonyms_zh=['交付可靠率', '交付准时率', '准时交付率', '到货准时率'],
            synonyms_en=['delivery_reliability', 'on_time_rate']
        ),
        FieldPattern(
            pattern=r'^(\u6b63\u5e38\u4ea4\u671f|\u6807\u51c6\u4ea4\u671f|\u4ea4\u8d27\u5468\u671f|normal_lead_time|Normal_Lead_Time|NORMAL_LEAD_TIME|\u6b63\u5e38\u4ea4\u671f\(\u5929\)|\u4ea4\u671f\(\u5929\)|Standard_Lead_Time|standard_lead_time)\s*(\([^)]*\))?$',
            field_key='normal_lead_time',
            field_name='正常交期',
            priority=60,
            data_type='number',
            synonyms_zh=['正常交期', '标准交期', '交货周期', '标准交期'],
            synonyms_en=['normal_lead_time', 'standard_lead_time']
        ),
    ]

    BOM_PATTERNS = [
        FieldPattern(
            pattern=r'^(\u6210\u54c1ID|\u7236\u4ef6ID|\u7236\u7269\u6599ID|\u7236\u7269\u6599\u4ee3\u7801|parent_code|Parent_Code|PARENT_CODE|\u6210\u54c1\u4ee3\u7801|\u4e0a\u7ea7\u7269\u6599ID|\u4ea7\u54c1ID|Product_ID|product_id|Parent_Material|parent_material)\s*(\([^)]*\))?$',
            field_key='parent_code',
            field_name='成品ID',
            priority=100,
            data_type='string',
            required=True,
            synonyms_zh=['成品ID', '父件ID', '父物料ID', '上级物料ID', '产品ID'],
            synonyms_en=['parent_code', 'parent_material', 'product_id']
        ),
        FieldPattern(
            pattern=r'^(\u6784\u6210\u539f\u6750\u6599ID|\u5b50\u4ef6ID|\u5b50\u7269\u6599ID|\u5b50\u7269\u6599\u4ee3\u7801|child_code|Child_Code|CHILD_CODE|\u539f\u6599ID|\u96f6\u90e8\u4ef6ID|\u7ec4\u4ef6ID|\u4e0b\u7ea7\u7269\u6599ID|Component_ID|component_id|Child_Material|child_material)\s*(\([^)]*\))?$',
            field_key='child_code',
            field_name='构成原材料ID',
            priority=95,
            data_type='string',
            required=True,
            synonyms_zh=['构成原材料ID', '子件ID', '子物料ID', '原料ID', '零部件ID', '组件ID'],
            synonyms_en=['child_code', 'child_material', 'component_id']
        ),
        FieldPattern(
            pattern=r'^(\u5355\u4f4d\u7528\u91cf|\u7528\u91cf|\u6570\u91cf|quantity|Quantity|QUANTITY|\u6bcf\u7ec4\u4ef6\u7528\u91cf|\u5355\u8017|\u6d88\u8017\u91cf|Usage|usage|Qty_Per|qty_per)\s*(\([^)]*\))?$',
            field_key='quantity',
            field_name='单位用量',
            priority=90,
            data_type='number',
            synonyms_zh=['单位用量', '用量', '数量', '每组件用量', '单耗'],
            synonyms_en=['quantity', 'usage', 'qty_per']
        ),
        FieldPattern(
            pattern=r'^(\u5355\u4f4d|\u8ba1\u91cf\u5355\u4f4d|unit|Unit|UNIT|UOM|uom)\s*(\([^)]*\))?$',
            field_key='unit',
            field_name='单位',
            priority=85,
            data_type='enum'
        ),
        FieldPattern(
            pattern=r'^(BOM\u5c42\u7ea7|\u5c42\u7ea7|bom_level|Bom_Level|BOM_LEVEL|\u5c42\u7ea7\u53f7|\u5c42\u6b21|Level|level|Hierarchy|hierarchy)\s*(\([^)]*\))?$',
            field_key='bom_level',
            field_name='BOM层级',
            priority=80,
            data_type='number',
            synonyms_zh=['BOM层级', '层级', '层次', '级别'],
            synonyms_en=['bom_level', 'level', 'hierarchy']
        ),
        FieldPattern(
            pattern=r'^(\u7528\u91cf\u5360\u6bd4|\u5360\u6bd4|usage_ratio|Usage_Ratio|USAGE_RATIO|\u7528\u91cf\u5360\u6bd4\(%\)|\u767e\u5206\u6bd4|\u6bd4\u4f8b|Ratio|ratio|Percentage|percentage)\s*(\([^)]*\))?$',
            field_key='usage_ratio',
            field_name='用量占比',
            priority=75,
            data_type='number',
            synonyms_zh=['用量占比', '占比', '百分比', '比例'],
            synonyms_en=['usage_ratio', 'ratio', 'percentage']
        ),
        FieldPattern(
            pattern=r'^(\u62a5\u5e9f\u7387|\u635f\u8017\u7387|scrap_rate|Scrap_Rate|SCRAP_RATE|\u5e9f\u54c1\u7387|Waste_Rate|waste_rate)\s*(\([^)]*\))?$',
            field_key='scrap_rate',
            field_name='报废率',
            priority=70,
            data_type='number',
            synonyms_zh=['报废率', '损耗率', '废品率'],
            synonyms_en=['scrap_rate', 'waste_rate']
        ),
    ]

    INVENTORY_PATTERNS = [
        FieldPattern(
            pattern=r'^(\u7269\u6599ID|\u7269\u6599\u4ee3\u7801|material_code|Material_Code|MATERIAL_CODE|\u7269\u6599\u7f16\u53f7|\u6599\u53f7)\s*(\([^)]*\))?$',
            field_key='material_code',
            field_name='物料ID',
            priority=100,
            data_type='string',
            required=True
        ),
        FieldPattern(
            pattern=r'^(\u7269\u6599\u540d\u79f0|material_name|Material_Name|\u7269\u6599\u540d|\u54c1\u540d)\s*(\([^)]*\))?$',
            field_key='material_name',
            field_name='物料名称',
            priority=95,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u5728\u5e93\u6570\u91cf|\u5e93\u5b58\u6570\u91cf|\u5f53\u524d\u5e93\u5b58|quantity|Quantity|QUANTITY|\u73b0\u6709\u5e93\u5b58|\u5b9e\u9645\u5e93\u5b58|\u8d26\u9762\u5e93\u5b58|\u6570\u91cf|On_Hand|on_hand|Stock_Qty|stock_qty)\s*(\([^)]*\))?$',
            field_key='quantity',
            field_name='在库数量',
            priority=90,
            data_type='number',
            synonyms_zh=['在库数量', '库存数量', '当前库存', '现有库存', '实际库存'],
            synonyms_en=['quantity', 'on_hand', 'stock_qty']
        ),
        FieldPattern(
            pattern=r'^(\u5e93\u5b58\u7c7b\u578b|inventory_type|Inventory_Type|INVENTORY_TYPE|\u5b58\u8d27\u7c7b\u522b|\u4ed3\u5e93\u7c7b\u578b|Stock_Type|stock_type)\s*(\([^)]*\))?$',
            field_key='inventory_type',
            field_name='库存类型',
            priority=85,
            data_type='enum'
        ),
        FieldPattern(
            pattern=r'^(\u4fdd\u8d28\u671f\u5230\u671f|\u6709\u6548\u671f\u81f3|\u8fc7\u671f\u65e5\u671f|expiry_date|Expiry_Date|EXPIRY_DATE|\u5931\u6548\u65e5\u671f|\u5230\u671f\u65e5|Due_Date|due_date)\s*(\([^)]*\))?$',
            field_key='expiry_date',
            field_name='保质期到期日',
            priority=80,
            data_type='date',
            synonyms_zh=['保质期到期日', '有效期至', '过期日期', '到期日'],
            synonyms_en=['expiry_date', 'expiration_date', 'due_date']
        ),
        FieldPattern(
            pattern=r'^(\u4ed3\u5e93|\u4ed3\u5e93\u540d\u79f0|warehouse|Warehouse|WAREHOUSE|\u5b58\u50a8\u4f4d\u7f6e|\u5e93\u4f4d|\u5b58\u653e\u5730|Location|location)\s*(\([^)]*\))?$',
            field_key='warehouse',
            field_name='仓库',
            priority=75,
            data_type='string',
            synonyms_zh=['仓库', '仓库名称', '存储位置', '库位'],
            synonyms_en=['warehouse', 'location']
        ),
        FieldPattern(
            pattern=r'^(\u6279\u6b21\u53f7|\u6279\u53f7|batch_no|Batch_No|BATCH_NO|\u6279\u6b21|Lot_No|LOT_NO|LotNumber|lot_number)\s*(\([^)]*\))?$',
            field_key='batch_no',
            field_name='批次号',
            priority=70,
            data_type='string',
            synonyms_zh=['批次号', '批号', '批次'],
            synonyms_en=['batch_no', 'lot_no', 'lot_number']
        ),
    ]

    # 客户导入字段模式
    CUSTOMER_PATTERNS = [
        FieldPattern(
            pattern=r'^(\u5ba2\u6237ID|\u5ba2\u6237\u4ee3\u7801|\u5ba2\u6237\u7f16\u53f7|customer_code|Customer_Code|CUSTOMER_CODE|\u5ba2\u6237\u53f7|\u5ba2\u6237ID)\s*(\([^)]*\))?$',
            field_key='customer_code',
            field_name='客户ID',
            priority=100,
            data_type='string',
            required=True,
            synonyms_zh=['客户代码', '客户编号', '客户号'],
            synonyms_en=['customer_code', 'customer_id']
        ),
        FieldPattern(
            pattern=r'^(\u5ba2\u6237\u540d\u79f0|\u5ba2\u6237|customer_name|Customer_Name|CUSTOMER_NAME|\u4e70\u65b9\u540d\u79f0|Buyer_Name|buyer_name|Client|client)\s*(\([^)]*\))?$',
            field_key='customer_name',
            field_name='客户名称',
            priority=95,
            data_type='string',
            required=True,
            synonyms_zh=['客户名称', '买方名称'],
            synonyms_en=['customer_name', 'buyer_name', 'client']
        ),
        FieldPattern(
            pattern=r'^(\u8054\u7cfb\u4eba|\u8054\u7cfb|contact_person|Contact_Person|CONTACT_PERSON|\u8d1f\u8d23\u4eba|Manager|manager)\s*(\([^)]*\))?$',
            field_key='contact_person',
            field_name='联系人',
            priority=85,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u8054\u7cfb\u7535\u8bdd|\u7535\u8bdd|phone|Phone|PHONE|\u624b\u673a|Mobile|mobile|\u8054\u7cfb\u65b9\u5f0f|Tel|tel|TEL)\s*(\([^)]*\))?$',
            field_key='phone',
            field_name='联系电话',
            priority=80,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u90ae\u7bb1|\u7535\u5b50\u90ae\u7bb1|email|Email|EMAIL|\u90ae\u4ef6\u5730\u5740|Mail|mail)\s*(\([^)]*\))?$',
            field_key='email',
            field_name='邮箱',
            priority=75,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u5730\u5740|address|Address|ADDRESS|\u6240\u5728\u5730|location|Location)\s*(\([^)]*\))?$',
            field_key='address',
            field_name='地址',
            priority=70,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u4fe1\u7528\u989d\u5ea6|credit_limit|Credit_Limit|CREDIT_LIMIT|\u989d\u5ea6|\u9650\u989d)\s*(\([^)]*\))?$',
            field_key='credit_limit',
            field_name='信用额度',
            priority=65,
            data_type='number'
        ),
        FieldPattern(
            pattern=r'^(\u5ba2\u6237\u7c7b\u578b|\u7c7b\u578b|customer_type|Type|TYPE)\s*(\([^)]*\))?$',
            field_key='customer_type',
            field_name='客户类型',
            priority=60,
            data_type='enum'
        ),
    ]

    # 采购订单导入字段模式
    PURCHASE_PATTERNS = [
        FieldPattern(
            pattern=r'^(\u91c7\u8d2d\u8ba2\u5355\u53f7|\u91c7\u8d2d\u5355\u53f7|\u91c7\u8d2d\u8ba2\u5355ID|po_no|PO_No|PO_NUMBER|purchase_order_no|Purchase_Order_No|PURCHASE_ORDER_NO)\s*(\([^)]*\))?$',
            field_key='po_no',
            field_name='采购订单号',
            priority=100,
            data_type='string',
            required=True,
            synonyms_zh=['采购单号', '采购编号', 'PO号'],
            synonyms_en=['po_no', 'purchase_order_no', 'ponumber']
        ),
        FieldPattern(
            pattern=r'^(\u4f9b\u5e94\u5546\u4ee3\u7801|\u4f9b\u5e94\u5546\u4ee3\u53f7|supplier_code|Supplier_Code|SUPPLIER_CODE|\u4f9b\u65b9\u4ee3\u7801|vendor_code|Vendor_Code)\s*(\([^)]*\))?$',
            field_key='supplier_code',
            field_name='供应商代码',
            priority=95,
            data_type='string',
            required=True
        ),
        FieldPattern(
            pattern=r'^(\u4f9b\u5e94\u5546\u540d\u79f0|supplier_name|Supplier_Name|SUPPLIER_NAME|\u4f9b\u65b9\u540d\u79f0|vendor_name|Vendor_Name)\s*(\([^)]*\))?$',
            field_key='supplier_name',
            field_name='供应商名称',
            priority=90,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u7269\u6599\u4ee3\u7801|\u7269\u6599\u4ee3\u53f7|material_code|Material_Code|MATERIAL_CODE|\u6599\u53f7|Part_No|SKU|sku)\s*(\([^)]*\))?$',
            field_key='material_code',
            field_name='物料代码',
            priority=88,
            data_type='string',
            required=True
        ),
        FieldPattern(
            pattern=r'^(\u7269\u6599\u540d\u79f0|material_name|Material_Name|\u54c1\u540d|\u89c4\u683c\u578b\u53f7)\s*(\([^)]*\))?$',
            field_key='material_name',
            field_name='物料名称',
            priority=85,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u6570\u91cf|quantity|Quantity|QTY|qty|\u8ba2\u8d2d\u6570\u91cf|\u8ba2\u5355\u6570\u91cf)\s*(\([^)]*\))?$',
            field_key='quantity',
            field_name='数量',
            priority=82,
            data_type='number'
        ),
        FieldPattern(
            pattern=r'^(\u5355\u4ef7|unit_price|Unit_Price|UNIT_PRICE|\u4ef7\u683c|Price|price)\s*(\([^)]*\))?$',
            field_key='unit_price',
            field_name='单价',
            priority=80,
            data_type='number'
        ),
        FieldPattern(
            pattern=r'^(\u4ea4\u671f\u5929\u6570|\u4ea4\u8d27\u5929\u6570|delivery_days|lead_time|Lead_Time|LEAD_TIME)\s*(\([^)]*\))?$',
            field_key='delivery_days',
            field_name='交期天数',
            priority=72,
            data_type='number'
        ),
        FieldPattern(
            pattern=r'^(\u4e0b\u5355\u65e5\u671f|order_date|Order_Date|ORDER_DATE|\u8ba2\u8d2d\u65e5\u671f)\s*(\([^)]*\))?$',
            field_key='order_date',
            field_name='下单日期',
            priority=70,
            data_type='date'
        ),
        FieldPattern(
            pattern=r'^(\u72b6\u6001|status|Status|STATE|\u8ba2\u5355\u72b6\u6001)\s*(\([^)]*\))?$',
            field_key='status',
            field_name='状态',
            priority=65,
            data_type='enum'
        ),
    ]

    ORDER_PATTERNS = [
        FieldPattern(
            pattern=r'^(\u8ba2\u5355ID|\u8ba2\u5355\u53f7|\u8ba2\u5355\u7f16\u53f7|order_no|Order_No|ORDER_NO|\u8ba2\u5355\u4ee3\u7801|SO|so|\u9500\u552e\u8ba2\u5355\u53f7|\u5355\u53f7|Order_ID|order_id|Order_Number|order_number)\s*(\([^)]*\))?$',
            field_key='order_no',
            field_name='订单ID',
            priority=100,
            data_type='string',
            required=True,
            synonyms_zh=['订单ID', '订单号', '订单编号', '单号', '销售订单号'],
            synonyms_en=['order_no', 'order_id', 'so', 'order_number']
        ),
        FieldPattern(
            pattern=r'^(\u6210\u54c1ID|\u4ea7\u54c1ID|\u7269\u6599\u4ee3\u7801|material_code|Material_Code|MATERIAL_CODE|\u6210\u54c1\u4ee3\u7801|\u4ea7\u54c1\u4ee3\u7801|SKU|sku|Product_ID|product_id|Item_Code|item_code)\s*(\([^)]*\))?$',
            field_key='material_code',
            field_name='成品ID',
            priority=95,
            data_type='string',
            required=True
        ),
        FieldPattern(
            pattern=r'^(\u6210\u54c1\u540d\u79f0|\u4ea7\u54c1\u540d\u79f0|material_name|Material_Name|\u7269\u6599\u540d\u79f0|\u54c1\u540d|Product_Name|product_name)\s*(\([^)]*\))?$',
            field_key='material_name',
            field_name='成品名称',
            priority=90,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u5ba2\u6237\u540d\u79f0|\u5ba2\u6237|customer_name|Customer_Name|CUSTOMER_NAME|\u4e70\u65b9|\u8d2d\u8d27\u65b9|\u4e0b\u5355\u5ba2\u6237|Buyer|buyer|Client|client)\s*(\([^)]*\))?$',
            field_key='customer_name',
            field_name='客户名称',
            priority=85,
            data_type='string',
            synonyms_zh=['客户名称', '客户', '买方', '购货方'],
            synonyms_en=['customer_name', 'buyer', 'client']
        ),
        FieldPattern(
            pattern=r'^(\u8ba2\u5355\u6570\u91cf|\u6570\u91cf|quantity|Quantity|QUANTITY|Qty|qty|\u8ba2\u8d2d\u6570\u91cf|\u4e0b\u5355\u6570\u91cf|\u9700\u6c42\u6570\u91cf|Order_Qty|order_qty)\s*(\([^)]*\))?$',
            field_key='quantity',
            field_name='订单数量',
            priority=80,
            data_type='number',
            synonyms_zh=['订单数量', '数量', '订购数量', '下单数量'],
            synonyms_en=['quantity', 'qty', 'order_qty']
        ),
        FieldPattern(
            pattern=r'^(\u5355\u4ef7|unit_price|Unit_Price|UNIT_PRICE|\u5355\u4ef7\(\u5143\)|\u4ea7\u54c1\u5355\u4ef7|\u6210\u4ea4\u4ef7\u683c|Price|price)\s*(\([^)]*\))?$',
            field_key='unit_price',
            field_name='单价',
            priority=75,
            data_type='number',
            synonyms_zh=['单价', '产品单价', '成交价格'],
            synonyms_en=['unit_price', 'price']
        ),
        FieldPattern(
            pattern=r'^(\u603b\u91d1\u989d|\u603b\u91d1\u989d\(\u5143\)|total_amount|Total_Amount|TOTAL_AMOUNT|\u91d1\u989d|\u5408\u8ba1\u91d1\u989d|\u8ba2\u5355\u603b\u989d|\u4ea4\u6613\u603b\u989d|Total|total|Amount|amount)\s*(\([^)]*\))?$',
            field_key='total_amount',
            field_name='总金额',
            priority=70,
            data_type='number',
            synonyms_zh=['总金额', '金额', '合计金额', '订单总额'],
            synonyms_en=['total_amount', 'total', 'amount']
        ),
        FieldPattern(
            pattern=r'^(\u8981\u6c42\u4ea4\u671f|\u671f\u671b\u4ea4\u671f|\u9700\u6c42\u65e5\u671f|demand_date|Demand_Date|DEMAND_DATE|\u4ea4\u8d27\u65e5\u671f|\u627f\u8bfa\u4ea4\u671f|\u5e94\u4ea4\u65e5\u671f|Due_Date|due_date|Delivery_Date|delivery_date)\s*(\([^)]*\))?$',
            field_key='demand_date',
            field_name='要求交期',
            priority=65,
            data_type='date',
            synonyms_zh=['要求交期', '期望交期', '需求日期', '交货日期'],
            synonyms_en=['demand_date', 'due_date', 'delivery_date']
        ),
        FieldPattern(
            pattern=r'^(\u4e0b\u5355\u65e5\u671f|\u8ba2\u5355\u65e5\u671f|order_date|Order_Date|ORDER_DATE|\u521b\u5efa\u65e5\u671f|\u5236\u5355\u65e5\u671f|\u63a5\u5355\u65e5\u671f|Create_Date|create_date|Order_Time|order_time)\s*(\([^)]*\))?$',
            field_key='order_date',
            field_name='下单日期',
            priority=60,
            data_type='date',
            synonyms_zh=['下单日期', '订单日期', '创建日期', '制单日期'],
            synonyms_en=['order_date', 'create_date', 'order_time']
        ),
        FieldPattern(
            pattern=r'^(\u4f18\u5148\u7ea7|priority|Priority|PRIORITY|\u7d27\u6025\u7a0b\u5ea6|\u4f18\u5148\u7ea7\(1\u6700\u9ad8-5\u6700\u4f4e\)|Importance|importance)\s*(\([^)]*\))?$',
            field_key='priority',
            field_name='优先级',
            priority=55,
            data_type='number',
            synonyms_zh=['优先级', '紧急程度', '重要性'],
            synonyms_en=['priority', 'importance']
        ),
        FieldPattern(
            pattern=r'^(\u72b6\u6001|\u8ba2\u5355\u72b6\u6001|status|Status|STATUS|\u5f53\u524d\u72b6\u6001|\u5904\u7406\u72b6\u6001|State|state|Order_Status|order_status)\s*(\([^)]*\))?$',
            field_key='status',
            field_name='状态',
            priority=50,
            data_type='enum',
            synonyms_zh=['状态', '订单状态', '当前状态', '处理状态'],
            synonyms_en=['status', 'state', 'order_status']
        ),
        FieldPattern(
            pattern=r'^(\u5907\u6ce8|\u8fd0\u8f93\u65b9\u5f0f|shipping_method|Shipping_Method|SHIPPING_METHOD|\u53d1\u8d27\u65b9\u5f0f|\u7269\u6d41\u65b9\u5f0f|\u5907\u6ce8\(\u7a7a\u8fd0/\u6d77\u8fd0\)|Ship_Method|ship_method|Logistics|logistics)\s*(\([^)]*\))?$',
            field_key='shipping_method',
            field_name='运输方式',
            priority=45,
            data_type='enum',
            synonyms_zh=['运输方式', '发货方式', '物流方式'],
            synonyms_en=['shipping_method', 'ship_method', 'logistics']
        ),
    ]

    WORKCENTER_PATTERNS = [
        FieldPattern(
            pattern=r'^(\u4ea7\u7ebfID|\u5de5\u4f5c\u4e2d\u5fc3ID|\u4ea7\u7ebf\u4ee3\u7801|work_center_code|Work_Center_Code|WORK_CENTER_CODE|\u5de5\u4f5c\u7ad9ID|\u8f66\u95f4ID|\u7ebf\u4f53ID|WC_ID|wc_id|Line_ID|line_id|Workcenter_Code|workcenter_code)\s*(\([^)]*\))?$',
            field_key='work_center_code',
            field_name='产线ID',
            priority=100,
            data_type='string',
            required=True,
            synonyms_zh=['产线ID', '工作中心ID', '产线代码', '工作站ID', '车间ID', '线体ID'],
            synonyms_en=['work_center_code', 'wc_id', 'line_id', 'workcenter_code']
        ),
        FieldPattern(
            pattern=r'^(\u4ea7\u7ebf\u540d\u79f0|\u5de5\u4f5c\u4e2d\u5fc3\u540d\u79f0|work_center_name|Work_Center_Name|WORK_CENTER_NAME|\u5de5\u4f5c\u7ad9\u540d\u79f0|\u8f66\u95f4\u540d\u79f0|\u7ebf\u4f53\u540d\u79f0|WC_Name|wc_name|Line_Name|line_name)\s*(\([^)]*\))?$',
            field_key='work_center_name',
            field_name='产线名称',
            priority=95,
            data_type='string',
            synonyms_zh=['产线名称', '工作中心名称', '工作站名称', '车间名称', '线体名称'],
            synonyms_en=['work_center_name', 'wc_name', 'line_name']
        ),
        FieldPattern(
            pattern=r'^(\u53ef\u751f\u4ea7\u4ea7\u54c1|\u4ea7\u80fd\u4ea7\u54c1|available_products|Available_Products|AVAILABLE_PRODUCTS|\u751f\u4ea7\u4ea7\u54c1\u5217\u8868|\u4ea7\u54c1\u8303\u56f4|Products|products)\s*(\([^)]*\))?$',
            field_key='available_products',
            field_name='可生产产品',
            priority=90,
            data_type='string'
        ),
        FieldPattern(
            pattern=r'^(\u6bcf\u65e5\u53ef\u7528\u5de5\u65f6|\u65e5\u5de5\u65f6|daily_available_hours|Daily_Available_Hours|DAILY_AVAILABLE_HOURS|\u6bcf\u5929\u5de5\u65f6|\u65e5\u5747\u5de5\u65f6|Hours_Per_Day|hours_per_day)\s*(\([^)]*\))?$',
            field_key='daily_available_hours',
            field_name='每日可用工时',
            priority=85,
            data_type='number'
        ),
        FieldPattern(
            pattern=r'^(\u73ed\u6b21\u6570|\u73ed\u6b21|shift_count|Shift_Count|SHIFT_COUNT|\u73ed\u5236|\u6bcf\u5929\u73ed\u6b21|Shifts|shifts)\s*(\([^)]*\))?$',
            field_key='shift_count',
            field_name='班次数',
            priority=80,
            data_type='number'
        ),
        FieldPattern(
            pattern=r'^(\u6bcf\u73ed\u5de5\u65f6|\u73ed\u6b21\u65f6\u957f|hours_per_shift|Hours_Per_Shift|HOURS_PER_SHIFT|\u5355\u73ed\u65f6\u95f4|\u6bcf\u73ed\u5c0f\u65f6\u6570|Shift_Hours|shift_hours)\s*(\([^)]*\))?$',
            field_key='hours_per_shift',
            field_name='每班工时',
            priority=75,
            data_type='number'
        ),
        FieldPattern(
            pattern=r'^(\u6bcf\u5468\u751f\u4ea7\u5929\u6570|\u5468\u5de5\u4f5c\u65e5|production_days_per_week|Production_Days_Per_Week|PRODUCTION_DAYS_PER_WEEK|\u6bcf\u5468\u5f00\u5de5\u5929\u6570|Working_Days|working_days)\s*(\([^)]*\))?$',
            field_key='production_days_per_week',
            field_name='每周生产天数',
            priority=70,
            data_type='number'
        ),
        FieldPattern(
            pattern=r'^(\u5b9a\u7f16\u4eba\u6570|\u7f16\u5236\u4eba\u6570|planned_headcount|Planned_Headcount|PLANNED_HEADCOUNT|\u8ba1\u5212\u4eba\u6570|\u989d\u5b9a\u4eba\u6570|\u5b9a\u5458|Planned_Staff|planned_staff)\s*(\([^)]*\))?$',
            field_key='planned_headcount',
            field_name='定编人数',
            priority=65,
            data_type='number',
            synonyms_zh=['定编人数', '编制人数', '计划人数', '额定人数', '定员'],
            synonyms_en=['planned_headcount', 'planned_staff']
        ),
        FieldPattern(
            pattern=r'^(\u5728\u5c97\u4eba\u6570|\u5b9e\u6709\u4eba\u6570|actual_headcount|Actual_Headcount|ACTUAL_HEADCOUNT|\u5b9e\u9645\u4eba\u6570|\u73b0\u6709\u4eba\u6570|\u5728\u518c\u4eba\u6570|Actual_Staff|actual_staff)\s*(\([^)]*\))?$',
            field_key='actual_headcount',
            field_name='在岗人数',
            priority=60,
            data_type='number',
            synonyms_zh=['在岗人数', '实有人数', '实际人数', '现有人数', '在册人数'],
            synonyms_en=['actual_headcount', 'actual_staff']
        ),
        FieldPattern(
            pattern=r'^(\u65e5\u4ea7\u80fd\u4e0a\u9650|\u6700\u5927\u4ea7\u80fd|daily_capacity_limit|Daily_Capacity_Limit|DAILY_CAPACITY_LIMIT|\u65e5\u4ea7\u80fd\u529b\u4e0a\u9650|\u65e5\u4ea7\u91cf\u4e0a\u9650|Max_Capacity|max_capacity)\s*(\([^)]*\))?$',
            field_key='daily_capacity_limit',
            field_name='日产能上限',
            priority=55,
            data_type='number',
            synonyms_zh=['日产能上限', '最大产能', '日产能力上限'],
            synonyms_en=['daily_capacity_limit', 'max_capacity']
        ),
        FieldPattern(
            pattern=r'^(\u6362\u7ebf\u65f6\u95f4|\u5207\u6362\u65f6\u95f4|changeover_time|Changeover_Time|CHANGEOVER_TIME|\u673a\u578b\u8f6c\u6362\u65f6\u95f4|Setup_Time|setup_time)\s*(\([^)]*\))?$',
            field_key='changeover_time',
            field_name='换线时间',
            priority=50,
            data_type='number',
            synonyms_zh=['换线时间', '切换时间', '机型转换时间'],
            synonyms_en=['changeover_time', 'setup_time']
        ),
        FieldPattern(
            pattern=r'^(\u72b6\u6001|\u542f\u7528\u72b6\u6001|is_active|Is_Active|IS_ACTIVE|\u662f\u5426\u542f\u7528|\u8fd0\u884c\u72b6\u6001|Active|active|Running|running)\s*(\([^)]*\))?$',
            field_key='status',
            field_name='状态',
            priority=45,
            data_type='enum',
            synonyms_zh=['状态', '启用状态', '是否启用', '运行状态'],
            synonyms_en=['status', 'is_active', 'active', 'running']
        ),
    ]

    # ==================== 系统配置模式 ====================
    CONFIG_PATTERNS = [
        # 工厂日历字段
        FieldPattern(
            pattern=r'^(\u65e5\u671f|Date|date|DATE|\u65e5\u5b50|\u65e5\u671f\u65f6\u95f4)\s*(\([^)]*\))?$',
            field_key='date',
            field_name='日期',
            priority=100,
            data_type='date',
            required=True,
            aliases=['日期'],
            synonyms_zh=['日期', '日子', '日期时间', '工作日日期'],
            synonyms_en=['date', 'day', 'calendar_date']
        ),
        FieldPattern(
            pattern=r'^(\u662f\u5426\u5de5\u4f5c\u65e5|is_workday|Is_WorkDay|IS_WORKDAY|\u5de5\u4f5c\u65e5|workday|Work_Day|WORK_DAY)\s*(\([^)]*\))?$',
            field_key='is_workday',
            field_name='是否工作日',
            priority=90,
            data_type='boolean',
            required=True,
            aliases=['工作日'],
            synonyms_zh=['是否工作日', '工作日', '上班日', '工作日标志'],
            synonyms_en=['is_workday', 'workday', 'working_day']
        ),
        FieldPattern(
            pattern=r'^(\u73ed\u6b21\u7c7b\u578b|\u73ed\u6b21|shift_type|Shift_Type|SHIFT_TYPE|\u73ed\u522b|shift)\s*(\([^)]*\))?$',
            field_key='shift_type',
            field_name='班次类型',
            priority=70,
            data_type='enum',
            aliases=['班次'],
            synonyms_zh=['班次类型', '班次', '班别', '轮班类型'],
            synonyms_en=['shift_type', 'shift', 'shift_class']
        ),
        FieldPattern(
            pattern=r'^(\u5de5\u5382\u4ee3\u7801|factory_code|Factory_Code|FACTORY_CODE|\u5382\u533a|plant_code|Plant_Code)\s*(\([^)]*\))?$',
            field_key='factory_code',
            field_name='工厂代码',
            priority=60,
            data_type='string',
            aliases=['工厂代码', '工厂'],
            synonyms_zh=['工厂代码', '工厂代号', '厂区', '车间代码'],
            synonyms_en=['factory_code', 'factory_id', 'plant_code']
        ),

        # 工厂调拨字段
        FieldPattern(
            pattern=r'^(\u8c03\u62d7\u7f16\u53f7|transfer_no|Transfer_No|TRANSFER_NO|\u8c03\u62d7\u5355\u53f7|transfer_id|Transfer_ID)\s*(\([^)]*\))?$',
            field_key='transfer_no',
            field_name='调拨编号',
            priority=100,
            data_type='string',
            required=True,
            aliases=['调拨编号', '调拨单号'],
            synonyms_zh=['调拨编号', '调拨单号', '调拨ID', '转移编号'],
            synonyms_en=['transfer_no', 'transfer_id', 'allocation_no']
        ),
        FieldPattern(
            pattern=r'^(\u6e90\u5de5\u5382|from_factory|From_Factory|FROM_FACTORY|\u51fa\u5382|source_factory|Source_Factory)\s*(\([^)]*\))?$',
            field_key='from_factory',
            field_name='源工厂',
            priority=85,
            data_type='string',
            required=True,
            aliases=['源工厂', '出厂'],
            synonyms_zh=['源工厂', '出厂', '来源工厂', '发货工厂'],
            synonyms_en=['from_factory', 'source_factory', 'origin_factory']
        ),
        FieldPattern(
            pattern=r'^(\u76ee\u6807\u5de5\u5382|to_factory|To_Factory|TO_FACTORY|\u5165\u5382|dest_factory|Dest_Factory)\s*(\([^)]*\))?$',
            field_key='to_factory',
            field_name='目标工厂',
            priority=85,
            data_type='string',
            required=True,
            aliases=['目标工厂', '入厂'],
            synonyms_zh=['目标工厂', '入厂', '目的工厂', '收货工厂'],
            synonyms_en=['to_factory', 'dest_factory', 'target_factory']
        ),
        FieldPattern(
            pattern=r'^(\u8c03\u62d7\u6570\u91cf|quantity|Quantity|QUANTITY|\u8f6c\u79fb\u6570\u91cf|transfer_qty|Transfer_Qty)\s*(\([^)]*\))?$',
            field_key='quantity',
            field_name='调拨数量',
            priority=80,
            data_type='number',
            required=True,
            aliases=['调拨数量', '数量'],
            synonyms_zh=['调拨数量', '转移数量', '搬运数量', '调拨量'],
            synonyms_en=['quantity', 'transfer_qty', 'allocation_qty']
        ),
        FieldPattern(
            pattern=r'^(\u8c03\u62d7\u65e5\u671f|transfer_date|Transfer_Date|TRANSFER_DATE|\u53d1\u8d27\u65e5\u671f)\s*(\([^)]*\))?$',
            field_key='transfer_date',
            field_name='调拨日期',
            priority=75,
            data_type='date',
            aliases=['调拨日期'],
            synonyms_zh=['调拨日期', '发货日期', '转移日期'],
            synonyms_en=['transfer_date', 'shipment_date']
        ),
        FieldPattern(
            pattern=r'^(\u9884\u8ba1\u5230\u8fbe\u65e5\u671f|expected_arrival|Expected_Arrival|EXPECTED_ARRIVAL|\u5230\u8fbe\u65e5\u671f|arrival_date|Arrival_Date)\s*(\([^)]*\))?$',
            field_key='expected_arrival_date',
            field_name='预计到达日期',
            priority=70,
            data_type='date',
            aliases=['预计到达日期', '到达日期'],
            synonyms_zh=['预计到达日期', '到达日期', '预计到货日', '交货日期'],
            synonyms_en=['expected_arrival', 'arrival_date', 'eta']
        ),
        FieldPattern(
            pattern=r'^(\u5173\u8054\u8ba2\u5355|related_order|Related_Order|RELATED_ORDER|\u5173\u8054\u8ba2\u5355\u53f7|order_no|Order_No)\s*(\([^)]*\))?$',
            field_key='related_order',
            field_name='关联订单',
            priority=55,
            data_type='string',
            aliases=['关联订单'],
            synonyms_zh=['关联订单', '相关订单', '对应订单', '来源订单'],
            synonyms_en=['related_order', 'associated_order', 'source_order']
        ),
        FieldPattern(
            pattern=r'^(\u8c03\u62d7\u539f\u56e0|reason|Reason|REASON|\u539f\u56e0|cause|Cause)\s*(\([^)]*\))?$',
            field_key='reason',
            field_name='调拨原因',
            priority=45,
            data_type='string',
            aliases=['调拨原因', '原因'],
            synonyms_zh=['调拨原因', '原因', '调拨理由', '备注'],
            synonyms_en=['reason', 'cause', 'remark', 'note']
        ),

        # 优先级规则字段
        FieldPattern(
            pattern=r'^(\u89c4\u5219\u540d\u79f0|rule_name|Rule_Name|RULE_NAME|\u89c4\u5219\u540d|name|Name)\s*(\([^)]*\))?$',
            field_key='name',
            field_name='规则名称',
            priority=100,
            data_type='string',
            required=True,
            aliases=['规则名称', '名称'],
            synonyms_zh=['规则名称', '规则名', '策略名称', '配置名称'],
            synonyms_en=['rule_name', 'name', 'strategy_name']
        ),
        FieldPattern(
            pattern=r'^(\u7b56\u7565\u7c7b\u578b|strategy|Strategy|STRATEGY|\u7b56\u7565|strategy_type|Strategy_Type)\s*(\([^)]*\))?$',
            field_key='strategy',
            field_name='策略类型',
            priority=90,
            data_type='enum',
            required=True,
            aliases=['策略类型', '策略'],
            synonyms_zh=['策略类型', '策略', '算法类型', '计算方式'],
            synonyms_en=['strategy', 'strategy_type', 'algorithm']
        ),
        FieldPattern(
            pattern=r'^(\u7d27\u6025\u5ea6\u6743\u91cd|urgency_weight|Urgency_Weight|URGENCY_WEIGHT|\u7d27\u6025\u6743\u91cd)\s*(\([^)]*\))?$',
            field_key='urgency_weight',
            field_name='紧急度权重',
            priority=80,
            data_type='number',
            aliases=['紧急度权重'],
            synonyms_zh=['紧急度权重', '紧急程度权重', '急单权重'],
            synonyms_en=['urgency_weight', 'urgent_weight']
        ),
        FieldPattern(
            pattern=r'^(\u4ea4\u671f\u6743\u91cd|delivery_weight|Delivery_Weight|DELIVERY_WEIGHT|\u4ea4\u671f\u6743\u91cd)\s*(\([^)]*\))?$',
            field_key='delivery_weight',
            field_name='交期权重',
            priority=80,
            data_type='number',
            aliases=['交期权重'],
            synonyms_zh=['交期权重', '交付日期权重', '到期日权重'],
            synonyms_en=['delivery_weight', 'due_date_weight']
        ),
        FieldPattern(
            pattern=r'^(\u5ba2\u6237\u7b49\u7ea7\u6743\u91cd|customer_weight|Customer_Weight|CUSTOMER_WEIGHT|\u5ba2\u6237\u6743\u91cd)\s*(\([^)]*\))?$',
            field_key='customer_weight',
            field_name='客户等级权重',
            priority=75,
            data_type='number',
            aliases=['客户等级权重'],
            synonyms_zh=['客户等级权重', '客户重要性权重', '客户权重'],
            synonyms_en=['customer_weight', 'client_weight']
        ),
        FieldPattern(
            pattern=r'^(\u8ba2\u5355\u4ef7\u503c\u6743\u91cd|value_weight|Value_Weight|VALUE_WEIGHT|\u4ef7\u503c\u6743\u91cd)\s*(\([^)]*\))?$',
            field_key='value_weight',
            field_name='订单价值权重',
            priority=70,
            data_type='number',
            aliases=['订单价值权重'],
            synonyms_zh=['订单价值权重', '金额权重', '价值权重'],
            synonyms_en=['value_weight', 'amount_weight', 'price_weight']
        ),
        FieldPattern(
            pattern=r'^(\u4ea7\u54c1\u7ec4\u6743\u91cd|product_weight|Product_Weight|PRODUCT_WEIGHT|\u54c1\u7ec4\u6743\u91cd)\s*(\([^)]*\))?$',
            field_key='product_weight',
            field_name='产品组权重',
            priority=65,
            data_type='number',
            aliases=['产品组权重'],
            synonyms_zh=['产品组权重', '产品线权重', '品类权重'],
            synonyms_en=['product_weight', 'category_weight', 'product_group_weight']
        ),
        FieldPattern(
            pattern=r'^(\u72b6\u6001|\u662f\u5426\u542f\u7528|is_active|Is_Active|IS_ACTIVE|\u542f\u7528\u72b6\u6001|enabled|Enabled|ENABLED)\s*(\([^)]*\))?$',
            field_key='status',
            field_name='状态',
            priority=50,
            data_type='enum',
            aliases=['状态', '启用状态'],
            synonyms_zh=['状态', '是否启用', '启用状态', '激活状态'],
            synonyms_en=['status', 'is_active', 'enabled', 'active']
        ),

        # 通用数据类型标识字段
        FieldPattern(
            pattern=r'^(\u6570\u636e\u7c7b\u578b|data_type|Data_Type|DATA_TYPE|\u7c7b\u522b|type|Type|TYPE)\s*(\([^)]*\))?$',
            field_key='data_type',
            field_name='数据类型',
            priority=110,
            data_type='string',
            required=False,
            aliases=['数据类型', '类别'],
            synonyms_zh=['数据类型', '类别', '分类', '记录类型'],
            synonyms_en=['data_type', 'type', 'category', 'record_type']
        ),
    ]

    # 交期变更导入字段模式
    DELIVERY_CHANGE_PATTERNS = [
        FieldPattern(
            pattern=r'^(\u8ba2\u5355\u53f7|\u5173\u8054\u8ba2\u5355\u53f7|order_no|Order_No|ORDER_NO|\u9500\u552e\u8ba2\u5355\u53f7|SO|so)\s*(\([^)]*\))?$',
            field_key='order_no',
            field_name='订单号',
            priority=100,
            data_type='string',
            required=True,
            synonyms_zh=['订单号', '关联订单号', '销售订单号'],
            synonyms_en=['order_no', 'so']
        ),
        FieldPattern(
            pattern=r'^(\u91c7\u8d2d\u5355\u53f7|\u5173\u8054\u91c7\u8d2d\u5355\u53f7|po_no|PO_No|PURCHASE_ORDER_NO)\s*(\([^)]*\))?$',
            field_key='po_no',
            field_name='采购单号',
            priority=95,
            data_type='string',
            synonyms_zh=['采购单号', '关联采购单号'],
            synonyms_en=['po_no', 'purchase_order_no']
        ),
        FieldPattern(
            pattern=r'^(\u7269\u6599ID|\u7269\u6599\u4ee3\u7801|material_code|Material_Code)\s*(\([^)]*\))?$',
            field_key='material_code',
            field_name='物料ID',
            priority=90,
            data_type='string',
        ),
        FieldPattern(
            pattern=r'^(\u4f9b\u5e94\u5546\u4ee3\u7801|supplier_code|Supplier_Code)\s*(\([^)]*\))?$',
            field_key='supplier_code',
            field_name='供应商代码',
            priority=88,
            data_type='string',
        ),
        FieldPattern(
            pattern=r'^(\u53d8\u66f4\u7c7b\u578b|change_type|Change_Type|CHANGE_TYPE)\s*(\([^)]*\))?$',
            field_key='change_type',
            field_name='变更类型',
            priority=85,
            data_type='enum',
            synonyms_zh=['变更类型', '交期变更类型'],
            synonyms_en=['change_type']
        ),
        FieldPattern(
            pattern=r'^(\u539f\u4ea4\u4ed8\u65e5\u671f|\u539f\u5b9a\u4ea4\u4ed8\u65e5\u671f|original_date|Original_Date|ORIGINAL_DATE|\u539f\u5b9a\u65e5\u671f)\s*(\([^)]*\))?$',
            field_key='original_date',
            field_name='原交付日期',
            priority=80,
            data_type='date',
            synonyms_zh=['原交付日期', '原定交付日期', '原定日期'],
            synonyms_en=['original_date', 'original_delivery_date']
        ),
        FieldPattern(
            pattern=r'^(\u65b0\u4ea4\u4ed8\u65e5\u671f|\u65b0\u5b9a\u4ea4\u4ed8\u65e5\u671f|new_date|New_Date|NEW_DATE|\u65b0\u5b9a\u65e5\u671f)\s*(\([^)]*\))?$',
            field_key='new_date',
            field_name='新交付日期',
            priority=78,
            data_type='date',
            synonyms_zh=['新交付日期', '新定交付日期', '新定日期'],
            synonyms_en=['new_date', 'new_delivery_date']
        ),
        FieldPattern(
            pattern=r'^(\u53d8\u66f4\u5929\u6570|change_days|Change_Days)\s*(\([^)]*\))?$',
            field_key='change_days',
            field_name='变更天数',
            priority=75,
            data_type='number',
            synonyms_zh=['变更天数', '延期天数', '提前天数'],
            synonyms_en=['change_days']
        ),
        FieldPattern(
            pattern=r'^(\u53d8\u66f4\u539f\u56e0|\u539f\u56e0|reason|Reason|REASON)\s*(\([^)]*\))?$',
            field_key='reason',
            field_name='变更原因',
            priority=70,
            data_type='string',
            synonyms_zh=['变更原因', '原因'],
            synonyms_en=['reason']
        ),
        FieldPattern(
            pattern=r'^(\u53d8\u66f4\u6765\u6e90|change_by|Change_By)\s*(\([^)]*\))?$',
            field_key='change_by',
            field_name='变更来源',
            priority=65,
            data_type='string',
            synonyms_zh=['变更来源', '操作人'],
            synonyms_en=['change_by', 'operator']
        ),
        FieldPattern(
            pattern=r'^(\u53d8\u66f4\u65f6\u95f4|created_at|Created_At|CREATED_AT|\u521b\u5efa\u65f6\u95f4)\s*(\([^)]*\))?$',
            field_key='created_at',
            field_name='变更时间',
            priority=60,
            data_type='date',
            synonyms_zh=['变更时间', '创建时间'],
            synonyms_en=['created_at', 'change_time']
        ),
    ]

    @classmethod
    def get_all_patterns(cls) -> Dict[str, List[FieldPattern]]:
        """获取所有类型的字段模式"""
        return {
            'material': cls.MATERIAL_PATTERNS,
            'supplier': cls.SUPPLIER_PATTERNS,
            'customer': cls.CUSTOMER_PATTERNS,
            'bom': cls.BOM_PATTERNS,
            'inventory': cls.INVENTORY_PATTERNS,
            'order': cls.ORDER_PATTERNS,
            'purchase': cls.PURCHASE_PATTERNS,
            'workcenter': cls.WORKCENTER_PATTERNS,
            'config': cls.CONFIG_PATTERNS,  # 系统配置（工厂日历/调拨/优先级规则）
            'delivery_change': cls.DELIVERY_CHANGE_PATTERNS,  # 交期变更记录
        }

    @classmethod
    def get_patterns_by_type(cls, import_type: str) -> List[FieldPattern]:
        """根据导入类型获取对应的字段模式"""
        patterns_map = cls.get_all_patterns()
        return patterns_map.get(import_type.lower(), [])


class SmartFieldRecognizer:
    """
    智能字段识别器 v2.0 - 增强版

    新增特性：
    1. 多阶段识别策略（正则 → 学习 → 模糊 → 自定义规则）
    2. 混合中英文支持（90%+识别率）
    3. 基于历史记录的自学习能力
    4. 自定义规则扩展性
    5. 综合置信度评估
    """

    def __init__(self, enable_learning: bool = True, cache_dir: Optional[str] = None):
        """
        初始化智能识别器

        Args:
            enable_learning: 是否启用学习功能
            cache_dir: 缓存目录路径
        """
        self.patterns = FieldRegexPatternsV2()
        self.fuzzy_matcher = FuzzyMatcher()
        self.synonym_dict = SynonymDictionary()

        # 初始化学习系统
        self.learning_system = FieldLearningSystem(cache_dir) if enable_learning else None
        self.enable_learning = enable_learning

        # 正则表达式编译缓存
        self._compiled_cache: Dict[str, re.Pattern] = {}
        # 结果缓存（避免重复计算）
        self._result_cache: Dict[str, RecognitionResult] = {}

    def _compile_pattern(self, pattern_str: str) -> re.Pattern:
        """编译并缓存正则表达式"""
        if pattern_str not in self._compiled_cache:
            self._compiled_cache[pattern_str] = re.compile(
                pattern_str,
                re.IGNORECASE | re.UNICODE
            )
        return self._compiled_cache[pattern_str]

    def recognize_field_multi_stage(
        self,
        column_name: str,
        patterns: List[FieldPattern],
        import_type: str
    ) -> RecognitionResult:
        """
        多阶段字段识别（核心算法）

        识别优先级：
        1. 正则表达式精确匹配
        2. 历史学习结果
        3. 自定义规则
        4. 模糊匹配（综合相似度）
        5. 同义词语义匹配

        Args:
            column_name: CSV列名
            patterns: 字段模式列表
            import_type: 导入类型

        Returns:
            RecognitionResult对象
        """
        column_clean = column_name.strip()

        # 阶段1：正则表达式精确匹配
        regex_result = self._regex_match(column_clean, patterns)
        if regex_result and regex_result.confidence >= 0.9:
            return regex_result

        # 阶段2：检查历史学习结果
        if self.enable_learning and self.learning_system:
            learned_result = self._learned_match(column_clean, import_type)
            if learned_result and learned_result.confidence >= 0.8:
                return learned_result

        # 阶段3：检查自定义规则
        custom_result = self._custom_rule_match(column_clean, import_type)
        if custom_result and custom_result.confidence >= 0.75:
            return custom_result

        # 阶段4：模糊匹配
        fuzzy_result = self._fuzzy_match(column_clean, patterns)
        if fuzzy_result and fuzzy_result.confidence >= 0.65:
            return fuzzy_result

        # 阶段5：同义词语义匹配
        semantic_result = self._semantic_match(column_clean, patterns)
        if semantic_result:
            return semantic_result

        # 返回最佳匹配（即使置信度较低）
        best_result = max(
            filter(None, [regex_result, learned_result, custom_result, fuzzy_result]),
            key=lambda r: r.confidence,
            default=None
        )

        if best_result and best_result.confidence >= 0.4:
            return best_result

        # 完全未匹配
        return RecognitionResult(
            original_column=column_name,
            mapped_key=None,
            field_name=None,
            confidence=0.0,
            data_type='unknown',
            required=False,
            matched=False,
            match_method='none'
        )

    def _regex_match(
        self,
        column_name: str,
        patterns: List[FieldPattern]
    ) -> Optional[RecognitionResult]:
        """阶段1：正则表达式匹配"""
        best_match = None
        best_priority = -1

        for field_pattern in patterns:
            compiled_pattern = self._compile_pattern(field_pattern.pattern)
            if compiled_pattern.match(column_name):
                if field_pattern.priority > best_priority:
                    best_match = field_pattern
                    best_priority = field_pattern.priority

        if best_match:
            confidence = self._calculate_regex_confidence(
                column_name, best_match
            )

            return RecognitionResult(
                original_column=column_name,
                mapped_key=best_match.field_key,
                field_name=best_match.field_name,
                confidence=confidence,
                data_type=best_match.data_type,
                required=best_match.required,
                matched=True,
                match_method='regex'
            )

        return None

    def _learned_match(
        self,
        column_name: str,
        import_type: str
    ) -> Optional[RecognitionResult]:
        """阶段2：基于历史学习的匹配"""
        if not self.learning_system:
            return None

        result = self.learning_system.get_learned_mapping(column_name, import_type)

        if result:
            mapped_key, confidence = result
            # 根据key查找field_name
            all_patterns = self.patterns.get_all_patterns()
            field_name = mapped_key
            for ptype, plist in all_patterns.items():
                for p in plist:
                    if p.field_key == mapped_key:
                        field_name = p.field_name
                        break

            return RecognitionResult(
                original_column=column_name,
                mapped_key=mapped_key,
                field_name=field_name,
                confidence=confidence,
                data_type='string',
                required=False,
                matched=True,
                match_method='learned'
            )

        return None

    def _custom_rule_match(
        self,
        column_name: str,
        import_type: str
    ) -> Optional[RecognitionResult]:
        """阶段3：自定义规则匹配"""
        if not self.learning_system:
            return None

        custom_rules = self.learning_system.get_custom_rules_for_type(import_type)

        for rule in custom_rules:
            compiled = self._compile_pattern(rule.pattern)
            if compiled.match(column_name):
                # 根据规则成功率和使用次数调整置信度
                base_confidence = 0.7 + (rule.priority / 200)
                adjusted_confidence = base_confidence * rule.success_rate

                return RecognitionResult(
                    original_column=column_name,
                    mapped_key=rule.field_key,
                    field_name=rule.field_name,
                    confidence=min(adjusted_confidence, 0.95),
                    data_type='string',
                    required=False,
                    matched=True,
                    match_method='custom'
                )

        return None

    def _fuzzy_match(
        self,
        column_name: str,
        patterns: List[FieldPattern]
    ) -> Optional[RecognitionResult]:
        """阶段4：模糊匹配（使用综合相似度算法）"""
        best_match = None
        best_score = 0.6  # 最低阈值

        for field_pattern in patterns:
            # 与field_name进行模糊比较
            name_score = self.fuzzy_matcher.combined_similarity(
                column_name, field_pattern.field_name
            )

            # 与field_key进行模糊比较
            key_score = self.fuzzy_matcher.combined_similarity(
                column_name, field_pattern.field_key
            )

            # 与同义词进行比较
            synonym_scores = []
            for syn in field_pattern.synonyms_zh + field_pattern.synonyms_en:
                syn_score = self.fuzzy_matcher.combined_similarity(
                    column_name, syn
                )
                synonym_scores.append(syn_score)

            max_synonym_score = max(synonym_scores) if synonym_scores else 0

            # 取最高分
            final_score = max(name_score, key_score, max_synonym_score)

            if final_score > best_score:
                best_score = final_score
                best_match = (field_pattern, final_score)

        if best_match:
            field_pattern, score = best_match

            return RecognitionResult(
                original_column=column_name,
                mapped_key=field_pattern.field_key,
                field_name=field_pattern.field_name,
                confidence=score * 0.9,  # 模糊匹配略降置信度
                data_type=field_pattern.data_type,
                required=field_pattern.required,
                matched=True,
                match_method='fuzzy',
                similarity_score=score
            )

        return None

    def _semantic_match(
        self,
        column_name: str,
        patterns: List[FieldPattern]
    ) -> Optional[RecognitionResult]:
        """阶段5：同义词语义匹配"""
        best_match = None
        best_score = 0.5

        # 将列名分词
        column_tokens = self._tokenize(column_name)

        for field_pattern in patterns:
            # 获取该字段的所有同义词
            all_names = (
                [field_pattern.field_name, field_pattern.field_key] +
                field_pattern.synonyms_zh +
                field_pattern.synonyms_en
            )

            max_semantic_score = 0
            for name in all_names:
                name_tokens = self._tokenize(name)

                # 计算token级别的语义相似度
                token_scores = []
                for ct in column_tokens:
                    for nt in name_tokens:
                        sem_score = self.synonym_dict.calculate_similarity(ct, nt)
                        token_scores.append(sem_score)

                if token_scores:
                    avg_score = sum(token_scores) / len(token_scores)
                    max_semantic_score = max(max_semantic_score, avg_score)

            if max_semantic_score > best_score:
                best_score = max_semantic_score
                best_match = (field_pattern, max_semantic_score)

        if best_match and best_score >= 0.55:
            field_pattern, score = best_match

            return RecognitionResult(
                original_column=column_name,
                mapped_key=field_pattern.field_key,
                field_name=field_pattern.field_name,
                confidence=score * 0.85,
                data_type=field_pattern.data_type,
                required=field_pattern.required,
                matched=True,
                match_method='semantic',
                similarity_score=score
            )

        return None

    def _tokenize(self, text: str) -> List[str]:
        """简单分词（按空格、下划线、横线、大写字母边界分割）"""
        # 在大写字母前插入分隔符
        text = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', text)
        # 分割
        tokens = re.split(r'[\s_\-]+', text)
        # 过滤空token和短token
        return [t.strip() for t in tokens if len(t.strip()) >= 1]

    def _calculate_regex_confidence(
        self,
        column_name: str,
        pattern: FieldPattern
    ) -> float:
        """计算正则匹配的置信度"""
        confidence = 0.6  # 基础置信度

        # 完全匹配加分
        if column_name.lower() == pattern.field_name.lower():
            confidence += 0.35
        elif column_name.lower() in pattern.field_name.lower() or \
             pattern.field_name.lower() in column_name.lower():
            confidence += 0.15

        # 标准列名（无括号说明）加分
        if '(' not in column_name and ')' not in column_name:
            confidence += 0.05

        # 优先级转换为置信度加成
        confidence += min(pattern.priority / 250, 0.35)

        return min(confidence, 1.0)

    def recognize_columns(
        self,
        columns: List[str],
        import_type: str,
        use_cache: bool = True
    ) -> Dict[str, dict]:
        """
        识别所有列并返回映射关系

        Args:
            columns: CSV文件的所有列名
            import_type: 导入类型
            use_cache: 是否使用结果缓存

        Returns:
            详细的识别结果字典
        """
        patterns = self.patterns.get_patterns_by_type(import_type)
        result = {}
        matched_keys = set()

        for column in columns:
            # 检查缓存
            cache_key = f"{import_type}::{column}"
            if use_cache and cache_key in self._result_cache:
                cached = self._result_cache[cache_key]
                result[column] = cached.__dict__
                if cached.mapped_key:
                    matched_keys.add(cached.mapped_key)
                continue

            # 执行多阶段识别
            recognition = self.recognize_field_multi_stage(
                column, patterns, import_type
            )

            # 检查重复匹配
            is_duplicate = recognition.matched and recognition.mapped_key in matched_keys
            if recognition.matched and recognition.mapped_key:
                matched_keys.add(recognition.mapped_key)

            recognition.is_duplicate = is_duplicate

            # 缓存结果
            if use_cache:
                self._result_cache[cache_key] = recognition

            result[column] = recognition.__dict__

        # 记录到学习系统（如果启用）
        if self.enable_learning and self.learning_system:
            for column, info in result.items():
                if info['matched'] and info['confidence'] >= 0.7:
                    self.learning_system.record_mapping(
                        original_column=column,
                        mapped_key=info['mapped_key'],
                        import_type=import_type,
                        confidence=info['confidence'],
                        method=info.get('match_method', 'auto')
                    )

        return result

    def auto_detect_import_type(
        self,
        columns: List[str]
    ) -> Tuple[str, float]:
        """
        自动检测导入类型（增强版）

        结合正则匹配、模糊匹配和学习历史进行判断
        """
        type_scores = {}
        all_patterns = self.patterns.get_all_patterns()

        for import_type, patterns in all_patterns.items():
            match_count = 0
            required_matched = 0
            total_required = 0
            weighted_confidence_sum = 0

            for pattern in patterns:
                if pattern.required:
                    total_required += 1

            for column in columns:
                recognition = self.recognize_field_multi_stage(
                    column, patterns, import_type
                )

                if recognition.matched:
                    match_count += 1
                    weighted_confidence_sum += recognition.confidence

                    if recognition.required:
                        required_matched += 1

            # 计算综合得分
            match_ratio = match_count / len(columns) if columns else 0
            required_ratio = required_matched / total_required if total_required > 0 else 0.5
            avg_confidence = weighted_confidence_sum / match_count if match_count > 0 else 0

            # 加权评分
            score = (
                match_ratio * 0.35 +
                required_ratio * 0.35 +
                avg_confidence * 0.30
            )

            type_scores[import_type] = score

        # 找出最佳匹配
        if not type_scores:
            return ('unknown', 0.0)

        best_type = max(type_scores.keys(), key=lambda x: type_scores[x])
        best_score = type_scores[best_type]

        return (best_type, best_score)

    def generate_field_mapping(
        self,
        recognition_result: Dict[str, dict]
    ) -> Dict[str, str]:
        """从识别结果生成字段映射字典"""
        mapping = {}
        for original_col, info in recognition_result.items():
            if info['matched'] and not info.get('is_duplicate', False):
                mapping[original_col] = info['mapped_key']
        return mapping

    def validate_required_fields(
        self,
        recognition_result: Dict[str, dict],
        import_type: str
    ) -> Dict[str, list]:
        """验证必填字段是否都存在"""
        patterns = self.patterns.get_patterns_by_type(import_type)
        required_fields = [p.field_name for p in patterns if p.required]
        matched_fields = [
            info['field_name']
            for info in recognition_result.values()
            if info['matched'] and info['field_name']
        ]

        missing = [f for f in required_fields if f not in matched_fields]
        present = [f for f in required_fields if f in matched_fields]

        return {
            'missing': missing,
            'present': present,
            'all_present': len(missing) == 0
        }

    def add_custom_rule(
        self,
        pattern: str,
        field_key: str,
        field_name: str,
        import_type: str,
        priority: int = 60
    ) -> str:
        """添加自定义规则（便捷方法）"""
        if not self.learning_system:
            raise RuntimeError("Learning system is not enabled")

        return self.learning_system.add_custom_rule(
            pattern=pattern,
            field_key=field_key,
            field_name=field_name,
            import_type=import_type,
            priority=priority
        )

    def remove_custom_rule(self, rule_id: str) -> bool:
        """删除自定义规则"""
        if not self.learning_system:
            return False
        return self.learning_system.remove_custom_rule(rule_id)

    def get_statistics(self) -> Dict[str, Any]:
        """获取识别器统计信息"""
        stats = {
            'version': '2.0',
            'patterns_loaded': sum(
                len(p) for p in self.patterns.get_all_patterns().values()
            ),
            'cache_size': len(self._result_cache),
            'learning_enabled': self.enable_learning,
        }

        if self.learning_system:
            stats.update(self.learning_system.get_statistics())

        return stats

    def clear_cache(self):
        """清除所有缓存"""
        self._result_cache.clear()
        self._compiled_cache.clear()


# ==================== 全局实例和便捷函数 ====================

# 创建全局智能识别器实例（启用学习功能）
smart_recognizer = SmartFieldRecognizer(enable_learning=True)


def recognize_csv_columns(columns: List[str], import_type: str) -> Dict[str, dict]:
    """便捷函数：识别CSV列（使用智能识别器v2）"""
    return smart_recognizer.recognize_columns(columns, import_type)


def auto_detect_data_type(columns: List[str]) -> Tuple[str, float]:
    """便捷函数：自动检测数据类型（增强版）"""
    return smart_recognizer.auto_detect_import_type(columns)


def get_field_mapping(recognition_result: Dict[str, dict]) -> Dict[str, str]:
    """便捷函数：获取字段映射"""
    return smart_recognizer.generate_field_mapping(recognition_result)


def add_field_mapping_rule(
    pattern: str,
    field_key: str,
    field_name: str,
    import_type: str,
    priority: int = 60
) -> str:
    """便捷函数：添加自定义字段映射规则"""
    return smart_recognizer.add_custom_rule(
        pattern=pattern,
        field_key=field_key,
        field_name=field_name,
        import_type=import_type,
        priority=priority
    )


def detect_config_sub_type(columns: List[str], sample_data: List[Dict] = None) -> Tuple[str, float, Dict]:
    """
    智能检测系统配置CSV的子类型

    支持识别：
    - factory_calendar: 工厂日历（日期、是否工作日、班次类型）
    - factory_transfer: 工厂调拨（调拨编号、源工厂、目标工厂、数量）
    - priority_rule: 优先级规则（规则名称、策略类型、各种权重）
    - mixed: 混合数据（包含"数据类型"列，需要按行分组）

    Args:
        columns: CSV列名列表
        sample_data: 可选的前几行样本数据（用于检测"数据类型"列）

    Returns:
        (sub_type, confidence, details) - 子类型、置信度、详细信息
    """
    # 转换为小写便于匹配
    columns_lower = [str(c).lower().strip() for c in columns]
    columns_str = ' '.join(columns_lower)

    # 1. 检测是否有"数据类型"列（混合数据标识）
    has_data_type_col = any('数据类型' in c or 'data_type' in c.lower() for c in columns)

    if has_data_type_col and sample_data:
        # 从样本数据中提取不同的数据类型值
        data_types = set()
        for row in sample_data[:10]:  # 只检查前10行
            for col in columns:
                if '数据类型' in str(col) or 'data_type' in str(col).lower():
                    val = str(row.get(col, '')).strip()
                    if val and val not in ['', 'None', 'none']:
                        data_types.add(val)

        # 拆分后的独立表：按数据类型精确匹配
        if data_types == {'工厂日历'}:
            return ('factory_calendar', 0.98, {
                'reason': '工厂日历数据',
                'data_types': list(data_types),
            })
        elif data_types == {'工厂调拨'}:
            return ('factory_transfer', 0.98, {
                'reason': '工厂调拨数据',
                'data_types': list(data_types),
            })
        elif data_types == {'优先级规则'}:
            return ('priority_rule', 0.98, {
                'reason': '优先级规则数据',
                'data_types': list(data_types),
            })
        elif data_types == {'工程变更'}:
            return ('engineering_change', 0.98, {
                'reason': '工程变更(ECN)数据',
                'data_types': list(data_types),
            })
        elif data_types <= {'工厂日历', '工厂调拨'}:
            return ('factory_calendar_transfer', 0.95, {
                'reason': '工厂日历与调拨混合数据',
                'data_types': list(data_types),
            })
        elif data_types <= {'优先级规则', '工程变更'}:
            return ('config_rules_ecn', 0.95, {
                'reason': '规则与工程变更混合数据',
                'data_types': list(data_types),
            })
        elif len(data_types) > 1:
            return ('mixed', 0.95, {
                'reason': '检测到多个数据类型（旧版混合文件）',
                'data_types': list(data_types),
                'has_data_type_column': True
            })
        elif len(data_types) == 1:
            single_type = data_types.pop()
            type_mapping = {
                '工厂日历': 'factory_calendar',
                '工厂调拨': 'factory_transfer',
                '优先级规则': 'priority_rule',
                '工程变更': 'engineering_change',
                '交期变更记录': 'delivery_change',
            }
            detected = type_mapping.get(single_type, single_type.lower())
            return (detected, 0.90, {
                'reason': f'单一数据类型: {single_type}',
                'data_type_value': single_type
            })

    # 2. 根据列名特征判断子类型
    scores = {}

    # 工厂日历特征字段
    calendar_keywords = ['日期', 'date', '工作日', 'workday', '班次', 'shift', '工厂代码', 'factory_code']
    calendar_score = sum(1 for kw in calendar_keywords if kw in columns_str)
    scores['factory_calendar'] = calendar_score

    # 工厂调拨特征字段
    transfer_keywords = ['调拨编号', 'transfer_no', '源工厂', 'from_factory', '目标工厂', 'to_factory',
                        '调拨数量', 'quantity', '预计到达', 'arrival', '关联订单', 'related_order']
    transfer_score = sum(1 for kw in transfer_keywords if kw in columns_str)
    scores['factory_transfer'] = transfer_score

    # 优先级规则特征字段
    rule_keywords = ['规则名称', 'rule_name', '策略类型', 'strategy', '紧急度权重', 'urgency_weight',
                   '交期权重', 'delivery_weight', '客户等级权重', 'customer_weight',
                   '订单价值权重', 'value_weight', '产品组权重', 'product_weight']
    rule_score = sum(1 for kw in rule_keywords if kw in columns_str)
    scores['priority_rule'] = rule_score

    # 找出最佳匹配
    if not any(scores.values()):
        return ('unknown', 0.0, {'reason': '无法识别配置子类型'})

    best_type = max(scores.keys(), key=lambda x: scores[x])
    best_score = scores[best_type]

    # 计算置信度（基于得分占比）
    total_score = sum(scores.values())
    confidence = best_score / total_score if total_score > 0 else 0

    return (best_type, confidence, {
        'reason': f'基于列名特征匹配',
        'scores': scores,
        'has_data_type_column': has_data_type_col
    })


def get_recognizer_stats() -> Dict[str, Any]:
    """获取识别器统计信息"""
    return smart_recognizer.get_statistics()


if __name__ == '__main__':
    print("Smart Field Recognizer v2.0 initialized successfully!")
    print(f"\nStatistics: {get_recognizer_stats()}")
