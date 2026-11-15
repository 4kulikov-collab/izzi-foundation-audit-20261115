"""Validate the inquiry schema in an already-open read-only SQLite snapshot."""
from django.apps import apps
from django.core.management.base import CommandError
from django.db import connections, models


def _sql(value):
    return ' '.join(value.strip().rstrip(';').split())


def validate_inquiry_schema(database):
    return validate_model_schema(database, 'inquiries')


def validate_model_schema(database, app_label):
    connection = connections['default']
    # Construct only: no context manager, DDL or changes to the active database.
    editor = connection.schema_editor(collect_sql=True)
    for model in apps.get_app_config(app_label).get_models():
        table = model._meta.db_table
        row = database.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", [table]).fetchone()
        if not row:
            raise CommandError(f'Отсутствует таблица локальных обращений: {table}.')
        columns = {c[1]: c for c in database.execute(f'PRAGMA table_info("{table}")')}
        foreign_keys = {(f[3], f[2], f[4]) for f in database.execute(f'PRAGMA foreign_key_list("{table}")')}
        unique_columns = set()
        for index in database.execute(f'PRAGMA index_list("{table}")'):
            # A partial unique index must not satisfy an unconditional constraint.
            if index[2] and not index[4]:
                name = index[1].replace('"', '""')
                unique_columns.add(tuple(c[2] for c in database.execute(f'PRAGMA index_info("{name}")')))
        for field in model._meta.local_fields:
            info = columns.get(field.column)
            if (not info or info[2].lower() != field.db_type(connection).lower()
                    or bool(info[5]) != field.primary_key
                    or (not field.primary_key and bool(info[3]) != (not field.null))):
                raise CommandError(f'Несовместимый столбец {table}.{field.column}.')
            if field.is_relation and (field.column, field.related_model._meta.db_table,
                                      field.target_field.column) not in foreign_keys:
                raise CommandError(f'Отсутствует внешняя связь {table}.{field.column}.')
            if field.unique and not field.primary_key and (field.column,) not in unique_columns:
                raise CommandError(f'Отсутствует уникальность {table}.{field.column}.')
        for constraint in model._meta.constraints:
            if isinstance(constraint, models.UniqueConstraint):
                if constraint.condition is None:
                    expected = tuple(model._meta.get_field(name).column for name in constraint.fields)
                    if expected not in unique_columns:
                        raise CommandError(f'Отсутствует уникальность {constraint.name}.')
                else:
                    actual = database.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                                              [constraint.name]).fetchone()
                    expected_sql = str(constraint.create_sql(model, editor))
                    if not actual or _sql(actual[0]) != _sql(expected_sql):
                        raise CommandError(f'Несовместимый условный индекс {constraint.name}.')
            elif isinstance(constraint, models.CheckConstraint):
                if _sql(constraint.constraint_sql(model, editor)) not in _sql(row[0]):
                    raise CommandError(f'Отсутствует проверка {constraint.name}.')
