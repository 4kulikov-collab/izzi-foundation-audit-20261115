"""Bounded allowlisted diagnostic codes only: no text, IDs, hashes, URLs or file names."""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
from datetime import datetime, timezone
from django.conf import settings

CODES = {
    'telegram': {'worksheet_saved', 'image_rejected', 'work_deleted', 'disabled',
        'method_not_allowed', 'access_denied', 'invalid_type', 'update_too_large', 'invalid_json',
        'invalid_update', 'unsupported_update', 'invalid_message', 'unsupported_message',
        'image_required', 'invalid_photo', 'image_too_large', 'invalid_file', 'update_conflict',
        'message_conflict', 'review_command', 'file_unavailable', 'transaction_boundary', 'retry', 'temporary_failure'},
    'reply': {'pending', 'attempted', 'sent', 'unknown', 'suppressed'},
    'ocr': {'recognized', 'unavailable', 'timeout', 'error', 'no_text', 'no_fields_found', 'unsupported_pdf'},
    'review': {'saved', 'confirmed', 'invalid_command', 'stale_review', 'pending_review', 'missing_review'},
}
LOCK = threading.Lock()


def emit(stage, code):
    if not getattr(settings, 'SANDBOX_DIAGNOSTICS_ENABLED', False):
        return
    if not isinstance(stage,str) or not isinstance(code,str) or stage not in CODES or code not in CODES[stage]:
        return
    path = Path(settings.DATA_DIR)/'diagnostics.jsonl'
    event = {'minute': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ'), 'stage': stage, 'code': code, 'scope':getattr(settings,'SANDBOX_DIAGNOSTICS_SCOPE','offline-rehearsal')}
    if event['scope'] not in ('offline-rehearsal','live-unverified'):return
    handler = None
    try:
        with LOCK:
            if path.is_symlink() or path.with_name(path.name+'.1').is_symlink():
                return
            handler = RotatingFileHandler(path, maxBytes=65536, backupCount=1, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            # Suppress logging's default stderr traceback on disk failure.
            handler.handleError = lambda record: None
            handler.emit(logging.LogRecord('sandbox', logging.INFO, '', 0,
                json.dumps(event, ensure_ascii=False, separators=(',', ':')), (), None))
    except (OSError, ValueError):
        pass  # Diagnostics cannot destroy an accepted original or leak a failure context.
    finally:
        if handler:
            handler.close()


def read_events(directory):
    result = []
    for name in ('diagnostics.jsonl.1','diagnostics.jsonl'):
        path = Path(directory)/name
        if not path.exists():
            continue
        if path.is_symlink() or path.stat().st_size > 66000:
            raise ValueError('Журнал недоступен или повреждён; содержимое скрыто.')
        try:
            events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            import re
            for event in events:
                if (type(event) is not dict or set(event) != {'minute','stage','code','scope'} or
                    event['scope'] not in ('offline-rehearsal','live-unverified') or event['stage'] not in CODES or event['code'] not in CODES[event['stage']] or
                    not isinstance(event['minute'],str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\dZ',event['minute'])):
                    raise ValueError()
            result.extend(events)
        except (ValueError, TypeError, KeyError, UnicodeError, OSError):
            raise ValueError('Журнал недоступен или повреждён; содержимое скрыто.') from None
    return result
