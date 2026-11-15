"""Explicit future operator commands. Preparation/serving are never automatic."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from .configuration import PilotError, load_config, state_directory, stop, default_config_path, write_template
from .package import verify, dependencies
from .runtime import activate, configure, readiness, serve_options
from .secrets_boundary import read_interactive


def main(root=None, argv=None):
    root = Path(root or Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description='Izzi: закрытый синтетический пилот; подключение только после отдельного разрешения.')
    parser.add_argument('action', choices=['verify','template','preflight','check','prepare','serve','stop'])
    parser.add_argument('--config', type=Path, default=default_config_path(root))
    parser.add_argument('--confirm-local-start', action='store_true')
    parser.add_argument('--confirm-telegram', action='store_true')
    args = parser.parse_args(argv)
    # Stop must keep working even when code/config verification has become a blocker.
    if args.action == 'stop':
        stop(root)
        print('Стоп-флаг установлен. Новые операции блокируются; уже начатый внешний запрос мог быть доставлен. Не удаляйте БД/журнал.')
        return 0
    verify(root)
    if args.action == 'verify':
        print('Целостность пакета подтверждена. Сеть и секреты не использовались.')
        return 0
    if args.action == 'template':
        write_template(root,args.config)
        print('Выключенный шаблон записан вне исходников. Заполните согласованные домен/ID; секреты сюда не добавлять.')
        return 0
    if args.action == 'preflight':
        from .preflight import assess
        report=assess(root,args.config)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if report['local_checks_passed'] else 2
    config = load_config(args.config)
    checks = dependencies(root)
    from .preflight import local_ocr_ready
    ocr_ready = local_ocr_ready()
    if args.action == 'check':
        print(json.dumps({'config_valid':True, 'telegram_requested':config.telegram_enabled, 'ocr_rus_available':ocr_ready, **checks},ensure_ascii=False,indent=2))
        return 0 if checks['python_3_12'] and checks['virtual_environment'] and not checks['missing_or_different_packages'] and ocr_ready else 2
    if checks['missing_or_different_packages'] or not checks['python_3_12'] or not checks['virtual_environment']:
        raise PilotError('Зависимости не готовы; выполните check.')
    if args.action == 'serve' and not ocr_ready:
        raise PilotError('Русская локальная OCR-модель недоступна; секреты не читались, сервер не запущен.')
    if not args.confirm_local_start:
        raise PilotError('Нужен явный --confirm-local-start после отдельного разрешения на целевую среду.')
    if args.action == 'prepare':
        if config.telegram_enabled:
            raise PilotError('Подготовка выполняется только при telegram_enabled=false.')
        configure(config, root, initialize=True)
        from django.core.management import call_command
        from django.contrib.auth import get_user_model
        call_command('migrate', interactive=False, verbosity=0)
        owner = get_user_model().objects.filter(pk=config.owner_id).first()
        if owner is None:
            call_command('create_private_owner')
            owner = get_user_model().objects.filter(pk=config.owner_id).first()
        if not owner or not owner.is_active or owner.is_staff or owner.is_superuser:
            raise PilotError('Проверенный обычный владелец не найден; права не менялись.')
        from inquiries.models import OrganizationIdentity
        OrganizationIdentity.objects.get_or_create(slot=1)
        call_command('seed_manager_demo', username=owner.username)
        print('Синтетическая база подготовлена. Telegram выключен. Сеть не запускалась.')
        return 0
    state = state_directory(root)
    if (state/'STOP').exists():
        raise PilotError('Пилот остановлен. Снятие стоп-флага возможно только после отдельной сверки журнала и разрешения.')
    if config.telegram_enabled and not args.confirm_telegram:
        raise PilotError('Нет отдельного --confirm-telegram; секреты не читались.')
    configure(replace(config, telegram_enabled=False), root)
    if readiness()['status'] != 'ready':
        raise PilotError('База, миграции или обычный владелец не готовы; секреты не читались и сервер не запущен.')
    if config.telegram_enabled:
        activate(config, read_interactive())
    from django.core.wsgi import get_wsgi_application
    from waitress import serve
    print('Запуск только на 127.0.0.1:8765. Для внешнего HTTPS нужен отдельно согласованный proxy. Остановка входа: pilot.py stop.', flush=True)
    serve(get_wsgi_application(), **serve_options())
    return 0


def entry():
    code = 2
    try:
        code = main()
    except PilotError as error:
        print(str(error), file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print('Операция остановлена.', file=sys.stderr)
    except Exception:
        print('Операция не завершена; детали и секреты не выводятся.', file=sys.stderr)
    raise SystemExit(code)
