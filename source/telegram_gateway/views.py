"""Authenticated, bounded webhook ingress; disabled unless explicitly injected by server config."""
import json
import secrets

from django.conf import settings
from django.http import JsonResponse
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables

from .adapter import Binding, GatewayError, accept

MAX_UPDATE_BYTES = 64 * 1024


def response(data, status=200):
    from config.diagnostics import emit
    emit("telegram", data.get("status"))
    if "reply_state" in data:
        emit("reply", data["reply_state"])
    result = JsonResponse(data, status=status, json_dumps_params={'ensure_ascii': False})
    result['Cache-Control'] = 'no-store'
    result['X-Content-Type-Options'] = 'nosniff'
    return result


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


@transaction.non_atomic_requests
@csrf_exempt
@sensitive_variables()
def webhook(request):
    # No Django session/username fallback; no token in path, query string or response.
    try:
        binding = getattr(settings, 'TELEGRAM_GATEWAY', None)
        if not isinstance(binding, Binding) or not binding.valid() or binding.mode != 'webhook':
            return response({'status': 'disabled', 'text': 'Telegram-вход отключён.'}, 503)
        if request.method != 'POST':
            return response({'status': 'method_not_allowed', 'text': 'Требуется POST.'}, 405)
        secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not isinstance(secret, str) or not secret.isascii() or not secrets.compare_digest(secret, binding.webhook_secret):
            return response({'status': 'access_denied', 'text': 'Доступ к этому входу не разрешён.'}, 403)
        if request.content_type != 'application/json':
            return response({'status': 'invalid_type', 'text': 'Требуется JSON.'}, 415)
        raw = request.read(MAX_UPDATE_BYTES + 1)
        if len(raw) > MAX_UPDATE_BYTES:
            return response({'status': 'update_too_large', 'text': 'Событие превышает допустимый размер.'}, 413)
        try:
            update = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            return response({'status': 'invalid_json', 'text': 'Некорректное событие JSON.'}, 400)
        return response(accept(update, binding))
    except GatewayError as error:
        return response({'status': error.code, 'text': error.text}, error.status)
    except Exception:
        # Never expose transport exceptions (they may contain bot tokens or download URLs).
        return response({'status': 'temporary_failure', 'text': 'Временная ошибка обработки; повторите доставку события.'}, 503)
