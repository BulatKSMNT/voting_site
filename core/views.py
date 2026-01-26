from django.shortcuts import render
from voting.models import Campaign, Round


def home(request):
    # Собираем данные для отображения на главной
    active_campaigns = Campaign.objects.filter(is_active=True).order_by('-created_at')
    recent_rounds = Round.objects.select_related('campaign').order_by('-started_at')[:5]

    context = {
        'active_campaigns': active_campaigns,
        'recent_rounds': recent_rounds,
        'total_campaigns': Campaign.objects.count(),
        'total_rounds': Round.objects.count(),
    }
    return render(request, 'core/home.html', context)