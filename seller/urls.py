from django.contrib import admin
from django.urls import path, include
from tickets.views import IndexView

urlpatterns = [
    path('', IndexView.as_view(), name='index'),
    path('admin/', admin.site.urls),
    path('api/', include('tickets.urls')),
]
