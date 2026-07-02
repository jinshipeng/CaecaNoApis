from .models import Notification


def notification_context(request):
    unread_count = 0
    if request.user.is_authenticated:
        try:
            unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
        except Exception:
            pass
    return {'unread_count': unread_count}
