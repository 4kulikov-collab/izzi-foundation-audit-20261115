"""Restored SQLite is evidence, not an authorized live database. No unlock switch."""
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import quote

from django.core.exceptions import ImproperlyConfigured

TABLE = 'izzi_restore_quarantine'
STATE = 'blocked-pending-reauthorization'
BLOCKED = ('Восстановленная копия заблокирована. Снимок не подтверждает актуальные права доступа. '
           'Нужна отдельная ручная повторная авторизация; штатное снятие блокировки пока не реализовано.')


def _connect(path):
    return sqlite3.connect(f'file:{quote(Path(path).resolve().as_posix(), safe="/")}?mode=ro', uri=True)


def require_runtime_allowed(path):
    """Run before generating a signing key or loading apps, even for login/public links."""
    path = Path(path)
    if not path.exists():
        return
    try:
        with closing(_connect(path)) as db:
            present = db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (TABLE,)).fetchone()
    except (OSError, sqlite3.Error):
        raise ImproperlyConfigured('Не удалось безопасно проверить локальную базу перед запуском.') from None
    if present:
        # An empty or malformed marker must not silently lift quarantine.
        raise ImproperlyConfigured(BLOCKED)


def quarantine_copy(path):
    """Only a staged restored copy. Keep historical grants for audit, erase sessions."""
    with closing(sqlite3.connect(path)) as db:
        with db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('DELETE FROM django_session')
            db.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} (version INTEGER NOT NULL, state TEXT NOT NULL)')
            db.execute(f'DELETE FROM {TABLE}')
            db.execute(f'INSERT INTO {TABLE} VALUES (?,?)', (1, STATE))
        db.execute('VACUUM')
    require_quarantined(path)


def require_quarantined(path):
    with closing(_connect(path)) as db:
        if db.execute(f'SELECT version,state FROM {TABLE}').fetchall() != [(1, STATE)]:
            raise ValueError('Invalid restored authorization quarantine')
        if db.execute('SELECT count(*) FROM django_session').fetchone()[0]:
            raise ValueError('Restored sessions are not allowed')
