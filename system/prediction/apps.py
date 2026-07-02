from django.apps import AppConfig
import os
import sys


class PredictionConfig(AppConfig):
    name = "prediction"
    verbose_name = "供应链智能运营系统"
    
    def ready(self):
        # SQLite数据库不需要额外初始化
        pass
