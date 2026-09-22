from django.urls import path
from .views import ResetView, BuyView, StatusView

urlpatterns = [
    path('reset', ResetView.as_view(), name='reset'),
    path('buy', BuyView.as_view(), name='buy'),
    path('status', StatusView.as_view(), name='status'),
]
