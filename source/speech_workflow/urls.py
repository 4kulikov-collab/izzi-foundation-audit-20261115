from django.urls import path

from . import views

app_name = 'speech_workflow'
urlpatterns = [
    path('<uuid:pk>/inquiry-draft/confirm/', views.inquiry_draft_confirm, name='confirm_inquiry_draft'),
    path('', views.submission_list, name='list'),
    path('new/', views.upload, name='upload'),
    path('<uuid:pk>/', views.detail, name='detail'),
    path('<uuid:pk>/audio/', views.audio_source, name='audio'),
    path('<uuid:pk>/transcript/edit/', views.transcript_edit, name='edit_transcript'),
    path('<uuid:pk>/transcript/confirm/', views.transcript_confirm, name='confirm_transcript'),
    path('<uuid:pk>/observation/', views.observation_create, name='observation'),
    path('<uuid:pk>/cancel/', views.cancel, name='cancel'),
    path('<uuid:pk>/transcript/revoke/', views.transcript_revoke, name='revoke_transcript'),
]
