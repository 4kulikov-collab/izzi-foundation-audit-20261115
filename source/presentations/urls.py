from django.urls import path

from . import views

app_name = "presentations"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("requests/new/", views.request_new, name="request_new"),
    path("requests/<uuid:pk>/", views.request_edit, name="request_edit"),
    path("requests/<uuid:pk>/preview/", views.preview, name="preview"),
    path("requests/<uuid:pk>/template/", views.change_template, name="change_template"),
    path("requests/<uuid:pk>/price/", views.reprice, name="reprice"),
    path("requests/<uuid:pk>/approve/", views.approve, name="approve"),
    path("requests/<uuid:pk>/publish/", views.publish, name="publish"),
    path("releases/<uuid:pk>/archive/", views.archive, name="archive"),
    path("releases/<uuid:pk>/archive/pdf/", views.archive_pdf, name="archive_pdf"),
    path("releases/<uuid:pk>/revoke/", views.revoke, name="revoke"),
    path("s/<str:token>/", views.share, name="share"),
    path("s/<str:token>/pdf/", views.share_pdf, name="share_pdf"),
]
