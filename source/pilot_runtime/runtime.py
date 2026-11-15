"""Closed-pilot settings/readiness/stop boundary, reusing the accepted application."""
import os
from pathlib import Path
import runpy
import threading

from .configuration import PilotError, state_directory
from .secrets_boundary import SecretBundle


class GuardedTransport:
    def __init__(self, transport, state):
        self.transport, self.state = transport, Path(state)
        self._active, self._lock = 0, threading.Lock()

    def __repr__(self):
        return '<GuardedTransport>'

    @property
    def active(self):
        with self._lock:
            return self._active

    def _enter(self):
        with self._lock:
            if (self.state/'STOP').exists():
                raise PilotError('Пилот остановлен.')
            self._active += 1

    def _leave(self):
        with self._lock:
            self._active -= 1

    def iter_file(self, *args, **kwargs):
        self._enter()
        try:
            for chunk in self.transport.iter_file(*args, **kwargs):
                if (self.state/'STOP').exists():
                    raise PilotError('Пилот остановлен.')
                yield chunk
        finally:
            self._leave()

    def send_message(self, *args, **kwargs):
        self._enter()
        try:
            return self.transport.send_message(*args, **kwargs)
        finally:
            self._leave()

    def send_buttons(self,*args,**kwargs):
        self._enter()
        try:return self.transport.send_buttons(*args,**kwargs)
        finally:self._leave()

    def answer_callback(self,*args,**kwargs):
        self._enter()
        try:return self.transport.answer_callback(*args,**kwargs)
        finally:self._leave()


def configure(config, root, *, initialize=False, secrets=None):
    from django.conf import settings
    if settings.configured:
        raise PilotError('Профиль пилота должен запускаться отдельным процессом.')
    root = Path(root).resolve()
    state = state_directory(root, create=initialize)
    if (state/'STOP').exists():
        raise PilotError('Стоп-флаг установлен; подготовка и запуск заблокированы.')
    if not initialize and not (state/'prototype.sqlite3').is_file():
        raise PilotError('База пилота не подготовлена.')
    if initialize and config.telegram_enabled:
        raise PilotError('Подготовка разрешена только при выключенном Telegram.')
    if config.telegram_enabled and type(secrets) is not SecretBundle:
        raise PilotError('Нет проверенного набора секретов в памяти.')
    # Execute accepted settings only AFTER forcing the owned synthetic data directory.
    old = os.environ.get('TRAVELHUB_DATA_DIR')
    os.environ['TRAVELHUB_DATA_DIR'] = str(state)
    try:
        base = runpy.run_path(str(root/'config/settings.py'))
    finally:
        if old is None:
            os.environ.pop('TRAVELHUB_DATA_DIR', None)
        else:
            os.environ['TRAVELHUB_DATA_DIR'] = old
    values = {key: value for key, value in base.items() if key.isupper()}
    values.update(DEBUG=False, ALLOWED_HOSTS=[config.host], CSRF_TRUSTED_ORIGINS=[config.origin],
        SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True, USE_X_FORWARDED_HOST=False,
        SECURE_PROXY_SSL_HEADER=None, SECURE_SSL_REDIRECT=False,  # Fail closed in boundary; don't redirect webhook bodies.
        DATA_UPLOAD_MAX_MEMORY_SIZE=6*1024*1024, FILE_UPLOAD_MAX_MEMORY_SIZE=6*1024*1024,
        PILOT_CONFIG=config, PILOT_STATE=state, PILOT_TRANSPORT=None, TELEGRAM_GATEWAY=None,
        SANDBOX_DIAGNOSTICS_ENABLED=True, SANDBOX_DIAGNOSTICS_SCOPE='live-unverified',
        PILOT_ENFORCE_ORGANIZATION=True)
    middlewares = list(values['MIDDLEWARE'])
    position = middlewares.index('django.contrib.auth.middleware.AuthenticationMiddleware') + 1
    middlewares.insert(position, 'pilot_runtime.runtime.PilotBoundary')
    values['MIDDLEWARE'] = middlewares
    settings.configure(**values)
    import django
    django.setup()
    if config.telegram_enabled:
        activate(config, secrets)
    return state


def activate(config, secrets):
    from django.conf import settings
    from telegram_gateway.adapter import Binding
    from telegram_gateway.http_transport import TelegramHTTPTransport
    if not config.telegram_enabled or type(secrets) is not SecretBundle:
        raise PilotError('Нет отдельного включения и проверенных секретов.')
    if readiness()['status'] != 'ready':
        raise PilotError('Включение запрещено: база/владелец не готовы или установлен STOP.')
    from .preflight import local_ocr_ready
    if not local_ocr_ready():
        raise PilotError('Русская локальная OCR-модель недоступна; Telegram не включён.')
    current = settings.PILOT_CONFIG
    if any(getattr(config, field) != getattr(current, field) for field in ('origin','owner_id','telegram_user_id','chat_id','gateway')):
        raise PilotError('Привязка отличается от проверенной при запуске.')
    if not config.approval_reference or not all(config.confirmations.values()):
        raise PilotError('Нет полного набора подтверждений включения.')
    transport = GuardedTransport(TelegramHTTPTransport(secrets.token, enabled=True), settings.PILOT_STATE)
    settings.PILOT_CONFIG = config
    settings.PILOT_TRANSPORT = transport
    settings.TELEGRAM_GATEWAY = Binding(config.gateway, config.owner_id, config.telegram_user_id, config.chat_id,
                                        secrets.webhook, transport, config.origin, in_chat_review=True)


def readiness():
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor
    state = Path(settings.PILOT_STATE)
    transport = getattr(settings, 'PILOT_TRANSPORT', None)
    result = {'status': 'blocked', 'gateway': 'on' if settings.TELEGRAM_GATEWAY else 'off',
              'transport_inflight': transport.active if transport else 0, 'live_telegram_verified': False}
    if (state/'STOP').exists():
        result['status'] = 'stopped'
        return result
    try:
        if not (state/'prototype.sqlite3').is_file():
            return result
        connection = connections['default']
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            return result
        if not get_user_model().objects.filter(pk=settings.PILOT_CONFIG.owner_id, is_active=True, is_staff=False, is_superuser=False).exists():
            return result
        if getattr(settings,'PILOT_ENFORCE_ORGANIZATION',False):
            from inquiries.models import OrganizationIdentity
            if not OrganizationIdentity.objects.filter(slot=1).exists():return result
        connection.ensure_connection()
    except Exception:
        return result
    result['status'] = 'ready'
    return result


class PilotBoundary:
    """Private UI enforcement is also required at the approved proxy; this checks TLS/host/stop."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        from django.http import JsonResponse
        def reply(data, status):
            response = JsonResponse(data, status=status, json_dumps_params={'ensure_ascii':False})
            response['Cache-Control'] = 'no-store'
            response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
            return response
        try:
            if request.get_host() != settings.PILOT_CONFIG.host or not request.is_secure():
                return reply({'status':'blocked'}, 403)
        except Exception:
            return reply({'status':'blocked'}, 403)
        if request.path == '/__pilot__/health':
            state = readiness()
            return reply(state, 200 if state['status']=='ready' else 503)
        if (Path(settings.PILOT_STATE)/'STOP').exists():
            return reply({'status':'stopped', 'text':'Пилот остановлен.'}, 503)
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and (not user.is_active or user.pk != settings.PILOT_CONFIG.owner_id):
            return reply({'status':'access_denied'}, 403)
        # Public tourist link routes are not part of the private manager pilot.
        if request.path.startswith('/s/'):
            return reply({'status':'closed_pilot'}, 404)
        return self.get_response(request)


def serve_options():
    return dict(host='127.0.0.1', port=8765, threads=1, connection_limit=16, channel_timeout=45,
                max_request_body_size=6*1024*1024, max_request_header_size=16*1024,
                trusted_proxy='127.0.0.1', trusted_proxy_count=1,
                trusted_proxy_headers={'x-forwarded-proto'}, clear_untrusted_proxy_headers=True,
                expose_tracebacks=False)
