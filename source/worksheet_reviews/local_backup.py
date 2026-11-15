"""Local-only SQLite snapshot/restore helpers for the synthetic prototype."""
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.core.management.base import CommandError
from django.db import connections
from config.restore_guard import quarantine_copy


REQUIRED_TABLES = {
    'django_migrations', 'auth_user',
    'worksheet_reviews_worksheetreview', 'worksheet_reviews_hotelchoice',
    'worksheet_reviews_worksheetocrrevision', 'worksheet_reviews_worksheetfilerevision',
}
REQUIRED_COLUMNS = {
    'django_migrations': {'app', 'name', 'applied'},
    'auth_user': {'id', 'password', 'last_login', 'is_superuser', 'username', 'first_name',
                  'last_name', 'email', 'is_staff', 'is_active', 'date_joined'},
    'worksheet_reviews_worksheetreview': {'id', 'owner_id', 'work_code', 'worksheet_bytes',
                                          'worksheet_type', 'worksheet_sha256', 'expert_overview',
                                          'current_source_version', 'created_at', 'updated_at'},
    'worksheet_reviews_hotelchoice': {'id', 'review_id', 'position', 'name', 'reference_text'},
    'worksheet_reviews_worksheetocrrevision': {
        'id', 'review_id', 'number', 'source_sha256', 'fields', 'original_text', 'metadata',
        'content_hash', 'confirmed_at', 'confirmed_by_id', 'created_at',
    },
    'worksheet_reviews_worksheetfilerevision': {
        'id', 'review_id', 'number', 'worksheet_bytes', 'worksheet_type', 'worksheet_sha256',
        'created_by_id', 'created_at',
    },
}


def validate_external_path(raw_path, *, label):
    path = Path(raw_path).expanduser().resolve()
    project_root = Path(settings.BASE_DIR).resolve()
    try:
        path.relative_to(project_root)
    except ValueError:
        return path
    raise CommandError(f'{label} должен находиться вне дерева репозитория; backup нельзя коммитить в GitHub.')


def validate_database(path):
    quoted = quote(Path(path).resolve().as_posix(), safe='/')
    try:
        database = sqlite3.connect(f'file:{quoted}?mode=ro', uri=True)
        try:
            integrity = database.execute('PRAGMA integrity_check').fetchone()
            if not integrity or integrity[0] != 'ok':
                raise CommandError('SQLite integrity_check не прошёл.')
            from inquiries.backup_schema import validate_inquiry_schema, validate_model_schema
            validate_inquiry_schema(database)
            validate_model_schema(database, 'telegram_gateway')
            tables = {row[0] for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            missing = REQUIRED_TABLES - tables
            if missing:
                raise CommandError('Файл не содержит ожидаемую схему приложения.')

            for table, expected_columns in REQUIRED_COLUMNS.items():
                actual_columns = {row[1] for row in database.execute(f'PRAGMA table_info("{table}")')}
                if expected_columns - actual_columns:
                    raise CommandError('Столбцы SQLite несовместимы с текущей схемой приложения.')

            review_fks = {
                (row[2], row[3], row[4])
                for row in database.execute('PRAGMA foreign_key_list("worksheet_reviews_worksheetreview")')
            }
            hotel_fks = {
                (row[2], row[3], row[4])
                for row in database.execute('PRAGMA foreign_key_list("worksheet_reviews_hotelchoice")')
            }
            ocr_fks = {
                (row[2], row[3], row[4])
                for row in database.execute('PRAGMA foreign_key_list("worksheet_reviews_worksheetocrrevision")')
            }
            file_fks = {
                (row[2], row[3], row[4])
                for row in database.execute('PRAGMA foreign_key_list("worksheet_reviews_worksheetfilerevision")')
            }
            if ('auth_user', 'owner_id', 'id') not in review_fks or (
                'worksheet_reviews_worksheetreview', 'review_id', 'id'
            ) not in hotel_fks:
                raise CommandError('Связи владельца/работы несовместимы с текущей схемой.')
            if ('worksheet_reviews_worksheetreview', 'review_id', 'id') not in ocr_fks or (
                'auth_user', 'confirmed_by_id', 'id'
            ) not in ocr_fks:
                raise CommandError('Связи OCR-версий с работой/подтверждающим владельцем несовместимы.')
            if ('worksheet_reviews_worksheetreview', 'review_id', 'id') not in file_fks or (
                'auth_user', 'created_by_id', 'id'
            ) not in file_fks:
                raise CommandError('Связи версий исходника с работой/владельцем несовместимы.')

            def has_unique_index(table, expected_columns):
                for index in database.execute(f'PRAGMA index_list("{table}")'):
                    if index[2] and tuple(row[2] for row in database.execute(
                        f'PRAGMA index_info("{index[1]}")'
                    )) == expected_columns:
                        return True
                return False

            if not has_unique_index('worksheet_reviews_worksheetreview', ('owner_id', 'work_code')) or not has_unique_index(
                'worksheet_reviews_hotelchoice', ('review_id', 'position')
            ) or not has_unique_index('worksheet_reviews_worksheetocrrevision', ('review_id', 'number')) or not has_unique_index(
                'worksheet_reviews_worksheetfilerevision', ('review_id', 'number')
            ):
                raise CommandError('Уникальные ограничения не совпадают с текущей схемой.')
            from django.db.migrations.loader import MigrationLoader
            migration_graph = MigrationLoader(None).graph
            applied = set(database.execute('SELECT app, name FROM django_migrations'))
            expected_migrations = set(migration_graph.nodes)
            if expected_migrations - applied or applied - expected_migrations:
                raise CommandError('Набор миграций не совместим с текущей версией приложения.')
            if list(database.execute('PRAGMA foreign_key_check')):
                raise CommandError('SQLite содержит нарушенные внешние связи.')
        finally:
            database.close()
    except CommandError:
        raise
    except (sqlite3.Error, OSError) as error:
        raise CommandError('Файл базы недоступен или не является проверяемой SQLite-базой.') from error


def _snapshot_to_temporary(source_connection, destination):
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix='.travelhub-snapshot-', suffix='.sqlite3', dir=destination.parent,
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        os.chmod(temporary, 0o600)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
        validate_database(temporary)
        return temporary
    except CommandError:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise
    except (sqlite3.Error, OSError) as error:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise CommandError('Не удалось создать или проверить SQLite-снимок.') from error
    except Exception:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise


def create_backup(destination):
    destination = validate_external_path(destination, label='Путь резервной копии')
    if destination.exists():
        raise CommandError('Файл резервной копии уже существует; он не был перезаписан.')
    connection = connections['default']
    if connection.vendor != 'sqlite':
        raise CommandError('Этот локальный backup поддерживает только текущую SQLite-базу.')
    connection.ensure_connection()
    temporary = _snapshot_to_temporary(connection.connection, destination)
    try:
        os.link(temporary, destination)
    except FileExistsError as error:
        raise CommandError('Файл backup появился параллельно; существующий файл сохранён.') from error
    except OSError as error:
        raise CommandError('Не удалось безопасно создать backup без перезаписи существующего файла.') from error
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def restore_test_copy(backup, destination):
    backup = validate_external_path(backup, label='Путь резервной копии')
    if not backup.is_file():
        raise CommandError('Файл backup не найден.')
    validate_database(backup)
    destination = validate_external_path(destination, label='Путь тестовой копии')
    connection = connections['default']
    if connection.vendor != 'sqlite':
        raise CommandError('Восстановление поддерживает только локальную SQLite-базу.')
    current_database = Path(connection.settings_dict['NAME']).expanduser().resolve()
    if destination == current_database or destination == backup:
        raise CommandError('Restore разрешён только в отдельный файл-копию, не в исходную базу или backup.')
    if destination.exists():
        raise CommandError('Целевая копия уже существует; restore никогда не перезаписывает файлы. Укажите новый путь.')

    source_connection = sqlite3.connect(f'file:{quote(backup.as_posix(), safe="/")}?mode=ro', uri=True)
    try:
        temporary = _snapshot_to_temporary(source_connection, destination)
    finally:
        source_connection.close()
    try:
        quarantine_copy(temporary)
        validate_database(temporary)
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise CommandError('Целевая копия появилась во время restore; она не была перезаписана.') from error
        except OSError as error:
            raise CommandError('Не удалось безопасно установить тестовую копию без перезаписи.') from error
    except (sqlite3.Error, ValueError, OSError):
        temporary.unlink(missing_ok=True)
        raise CommandError('Копия не опубликована: не удалось установить блокировку восстановленного доступа.') from None
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink(missing_ok=True)
    return destination
