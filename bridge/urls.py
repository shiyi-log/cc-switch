from django.urls import path, re_path

from . import views

urlpatterns = [
    path("api/health", views.health, name="bridge-health"),
    path("api/status", views.status, name="bridge-status"),
    path(
        "api/accounts/<uuid:account_id>/refresh",
        views.refresh_account,
        name="bridge-refresh-account",
    ),
    re_path(r"^v1/(?P<path>.*)$", views.proxy, name="bridge-proxy"),
]
