from django.db import models
from django.contrib.auth.models import User
from datetime import datetime


class UserProfile(models.Model):
    GENDER_CHOICES = (
        ('M', '男'),
        ('F', '女'),
    )
    
    user = models.OneToOneField(User, on_delete=models.CASCADE, verbose_name='用户')
    full_name = models.CharField(max_length=100, blank=True, null=True, verbose_name='姓名')
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True, null=True, verbose_name='性别')
    birth_date = models.DateField(blank=True, null=True, verbose_name='出生年月')
    employee_id = models.CharField(max_length=50, blank=True, null=True, verbose_name='员工号')
    department = models.CharField(max_length=100, blank=True, null=True, verbose_name='所属部门')
    phone = models.CharField(max_length=20, blank=True, null=True, verbose_name='联系电话')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Meta:
        verbose_name = '用户个人信息'
        verbose_name_plural = '用户个人信息'
        db_table = '用户个人信息'
    
    def __str__(self):
        full_name = f"{self.user.first_name} {self.user.last_name}".strip()
        return f'{self.user.username} - {full_name or "未设置"}'


def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)


def add_user_to_department_group(sender, instance, created, **kwargs):
    from django.contrib.auth.models import Group
    
    current_groups = instance.user.groups.all()
    
    all_departments = set(UserProfile.objects.exclude(department__isnull=True).exclude(department__exact='').values_list('department', flat=True))
    department_group_names = list(all_departments)
    
    if instance.department:
        target_group, _ = Group.objects.get_or_create(name=instance.department)
        
        if target_group not in current_groups:
            instance.user.groups.add(target_group)
        
        for group in current_groups:
            if group.name in department_group_names and group.name != instance.department:
                instance.user.groups.remove(group)
    else:
        for group in current_groups:
            if group.name in department_group_names:
                instance.user.groups.remove(group)


class SystemConfigModel(models.Model):
    key = models.CharField(max_length=100, unique=True, db_index=True, verbose_name='配置键')
    value = models.TextField(blank=True, default='', verbose_name='配置值')
    description = models.CharField(max_length=500, blank=True, default='', verbose_name='配置描述')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '系统配置'
        verbose_name_plural = '系统配置'
        db_table = 'prediction_systemconfigmodel'
    
    def __str__(self):
        return self.key


from django.db.models.signals import post_save
post_save.connect(create_user_profile, sender=User)
post_save.connect(add_user_to_department_group, sender=UserProfile)


class ImportHistory(models.Model):
    IMPORT_TYPE_CHOICES = (
        ('material', '物料数据'),
        ('supplier', '供应商数据'),
        ('customer', '客户数据'),
        ('bom', 'BOM数据'),
        ('inventory', '库存数据'),
        ('order', '订单数据'),
        ('purchase', '采购订单数据'),
        ('workcenter', '工作中心数据'),
        ('config', '系统配置数据'),
        ('delivery_change', '交期变更记录'),
        ('factory_calendar_transfer', '工厂日历调拨'),
        ('config_rules_ecn', '规则与工程变更'),
    )

    STATUS_CHOICES = (
        ('success', '导入成功'),
        ('partial', '部分成功'),
        ('error', '导入失败'),
    )

    import_type = models.CharField(max_length=30, choices=IMPORT_TYPE_CHOICES, db_index=True, verbose_name='导入类型')
    file_name = models.CharField(max_length=255, verbose_name='文件名')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='success', db_index=True, verbose_name='状态')
    imported_count = models.IntegerField(default=0, verbose_name='导入条数')
    updated_count = models.IntegerField(default=0, verbose_name='更新条数')
    error_count = models.IntegerField(default=0, verbose_name='错误条数')
    error_details = models.TextField(blank=True, null=True, verbose_name='错误详情')
    imported_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='操作人')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='导入时间')

    class Meta:
        verbose_name = '导入历史'
        verbose_name_plural = '导入历史记录'
        db_table = 'import_history'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['import_type', 'status'], name='import_type_status_idx'),
            models.Index(fields=['status', 'created_at'], name='import_status_created_idx'),
        ]

    def __str__(self):
        return f'{self.get_import_type_display()} - {self.file_name} ({self.created_at.strftime("%Y-%m-%d %H:%M")})'
