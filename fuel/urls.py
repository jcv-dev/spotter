from django.urls import path

from . import views

urlpatterns = [
    path("api/route/", views.RouteAPIView.as_view(), name="route-api"),
    path("map/", views.MapView.as_view(), name="map"),
    path("health/", views.health, name="health"),
]
