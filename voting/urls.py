from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CampaignViewSet, RoundViewSet, VoteViewSet, ParticipantViewSet, CurrentRoundResults

router = DefaultRouter()
router.register(r'campaigns', CampaignViewSet, basename='campaign')
router.register(r'rounds', RoundViewSet, basename='round')
router.register(r'votes', VoteViewSet, basename='vote')
router.register(r'participants', ParticipantViewSet, basename='participant')

urlpatterns = [
    path('api/', include(router.urls)),
    path('results/', CurrentRoundResults.as_view(), name='results'),

]
