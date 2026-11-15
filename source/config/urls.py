from django.contrib.auth import views as auth_views
from django.urls import include, path

from telegram_gateway.views import webhook as telegram_webhook

urlpatterns = [
    path('integrations/telegram/update/', telegram_webhook, name='telegram-webhook'),
    path('accounts/login/', auth_views.LoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('cards/', include('travelcards.urls')),
    path('voice-notes/', include('speech_workflow.urls')),
    path('worksheet-reviews/', include('worksheet_reviews.urls')),
    path('inquiries/', include('inquiries.urls')),
    path('', include('presentations.urls')),
]
