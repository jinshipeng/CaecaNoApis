import os
import shutil
from datetime import datetime
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse


BACKUP_DIR = os.path.join(settings.BASE_DIR, 'backups')


def _ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)


def _get_backup_list():
    _ensure_backup_dir()
    backups = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.endswith('.sqlite3'):
            filepath = os.path.join(BACKUP_DIR, f)
            stat = os.stat(filepath)
            backups.append({
                'filename': f,
                'size': stat.st_size,
                'created_at': datetime.fromtimestamp(stat.st_ctime),
            })
    return backups


@login_required
def backup_list(request):
    backups = _get_backup_list()
    return render(request, 'backup_list.html', {
        'backups': backups,
        'page_title': '数据备份',
        'page_subtitle': '管理数据库备份文件',
    })


@login_required
def create_backup(request):
    if request.method == 'POST':
        _ensure_backup_dir()
        db_path = settings.DATABASES['default']['NAME']
        if not os.path.exists(db_path):
            messages.error(request, '数据库文件不存在')
            return redirect('backup_list')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f'backup_{timestamp}.sqlite3'
        backup_path = os.path.join(BACKUP_DIR, backup_filename)
        try:
            shutil.copy2(db_path, backup_path)
            messages.success(request, f'备份创建成功: {backup_filename}')
        except Exception as e:
            messages.error(request, f'备份创建失败: {str(e)}')
    return redirect('backup_list')


@login_required
def restore_backup(request, filename):
    if request.method == 'POST':
        backup_path = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(backup_path):
            messages.error(request, '备份文件不存在')
            return redirect('backup_list')
        db_path = settings.DATABASES['default']['NAME']
        try:
            shutil.copy2(backup_path, db_path)
            messages.success(request, f'已从备份 {filename} 恢复数据库')
        except Exception as e:
            messages.error(request, f'恢复失败: {str(e)}')
    return redirect('backup_list')


@login_required
def delete_backup(request, filename):
    if request.method == 'POST':
        backup_path = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(backup_path):
            messages.error(request, '备份文件不存在')
            return redirect('backup_list')
        try:
            os.remove(backup_path)
            messages.success(request, f'备份 {filename} 已删除')
        except Exception as e:
            messages.error(request, f'删除失败: {str(e)}')
    return redirect('backup_list')
