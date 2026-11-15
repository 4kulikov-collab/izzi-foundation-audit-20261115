from django.urls import path
from . import views

app_name = 'worksheet_reviews'
urlpatterns = [
    path('', views.review_list, name='list'),
    path('new/', views.upload, name='upload'),
    path('<uuid:pk>/', views.detail, name='detail'),
    path('<uuid:pk>/delete/', views.delete_confirm, name='delete_confirm'),
    path('<uuid:pk>/sheet/', views.sheet, name='sheet'),
    path('<uuid:pk>/sheet/<int:number>/', views.sheet_version, name='sheet_version'),
]
