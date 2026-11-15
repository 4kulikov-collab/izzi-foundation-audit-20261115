from django.urls import path
from . import views

app_name = 'travelcards'
urlpatterns = [
    path('', views.card_list, name='list'),
    path('new/', views.upload, name='upload'),
    path('<uuid:pk>/', views.detail, name='detail'),
    path('<uuid:pk>/photo/', views.photo_replace, name='replace_photo'),
    path('<uuid:pk>/sources/<uuid:source_pk>/', views.source, name='source'),
    path('<uuid:pk>/confirm/', views.confirm, name='confirm'),
    path('<uuid:pk>/prepare/', views.prepare, name='prepare'),
]
