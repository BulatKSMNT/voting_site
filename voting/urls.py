from django.urls import path
from .views import (
    CurrentRoundResults,
    AddVoteAPIView,
    ActiveRoundParticipants,
    ActiveRoundInfo,
    ActiveRoundsList,  # раскомментируй, если используешь
    StartRoundAPIView,
    EndRoundAPIView,
    AddParticipantAPIView,
    CreateCampaignAPIView, ActiveCampaignsList
)

urlpatterns = [
    # Страница результатов (HTML)
    path('results/', CurrentRoundResults.as_view(), name='results'),

    # API для голосования
    path('vote/', AddVoteAPIView.as_view(), name='add-vote'),

    # API для бота
    path('active-participants/', ActiveRoundParticipants.as_view(), name='active-participants'),
    path('active-round-info/', ActiveRoundInfo.as_view(), name='active-round-info'),
    path('active-rounds/', ActiveRoundsList.as_view(), name='active-rounds'),

    # Админ-действия
    path('start-round/', StartRoundAPIView.as_view(), name='start-round'),
    path('end-round/', EndRoundAPIView.as_view(), name='end-round'),
    path('add-participant/', AddParticipantAPIView.as_view(), name='add-participant'),
    path('create-campaign/', CreateCampaignAPIView.as_view(), name='create-campaign'),
    path('active-campaigns/', ActiveCampaignsList.as_view(), name='active-campaigns'),
]