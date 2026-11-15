from django.urls import path
from . import views, director_views
app_name = 'inquiries'
urlpatterns = [
    path('director/', director_views.overview, name='director_list'),
    path('director/<uuid:pk>/', director_views.detail, name='director_detail'),
    path('director/versions/<uuid:pk>/pdf/', director_views.pdf, name='director_pdf'),
    path('voice/<uuid:draft_id>/selection/', views.voice_selection, name='voice_selection'),
    path('', views.inquiry_list, name='list'),
    path('<uuid:pk>/', views.inquiry_detail, name='detail'),
    path('versions/<uuid:pk>/source/', views.version_source, name='source'),
    path('<uuid:work_id>/selection/', views.selection, name='selection'),
]
