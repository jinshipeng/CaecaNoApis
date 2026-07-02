from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from prediction.models.supply_chain_models import PlanLog

@login_required
def view_logs(request):
    """查看系统日志"""
    logs = PlanLog.objects.all().order_by('-created_at')[:100]
    return render(request, 'logs.html', {'logs': logs})