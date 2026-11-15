"""Non-secret closed-pilot configuration. No Django startup, files changed or network."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

BASE = '58a035af855a089bfffb8a70eebbfe0572372fb6'
CHECKS = ('owner_approval', 'isolated_host', 'https_verified', 'private_ui', 'proxy_headers', 'backup_ready', 'supervisor_verified')
KEYS = {'schema', 'purpose', 'origin', 'owner_id', 'telegram_user_id', 'chat_id', 'gateway', 'telegram_enabled', 'approval_reference', 'confirmations'}


class PilotError(Exception):
    """Codes/messages never contain supplied configuration/secret values."""
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PilotError('Повторяющиеся ключи конфигурации.')
        result[key] = value
    return result


@dataclass(frozen=True, repr=False)
class PilotConfig:
    origin: str
    owner_id: int
    telegram_user_id: int
    chat_id: int
    gateway: str
    telegram_enabled: bool
    approval_reference: str | None
    confirmations: dict = field(repr=False)

    @property
    def host(self):
        return urlsplit(self.origin).hostname

    def __repr__(self):
        return '<PilotConfig values hidden>'


def parse_config(raw):
    try:
        data = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise PilotError('Конфигурация должна быть корректным JSON.') from None
    if not isinstance(data, dict) or set(data) != KEYS:
        raise PilotError('Набор полей конфигурации неверен; секреты здесь недопустимы.')
    if data['schema'] != 1 or type(data['schema']) is not int or data['purpose'] != 'synthetic-closed-pilot':
        raise PilotError('Требуется конфигурация синтетического закрытого пилота schema=1.')
    origin = data['origin']
    if not isinstance(origin, str) or len(origin) > 253:
        raise PilotError('Не задан согласованный HTTPS origin.')
    try:
        url = urlsplit(origin)
        host = url.hostname or ''
        valid = (url.scheme == 'https' and origin == 'https://'+host and not url.username and not url.password
                 and not url.path and not url.query and not url.fragment and len(host.split('.')) >= 2
                 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')))
    except ValueError:
        valid = False
        host = ''
    if (not valid or not re.search(r'[a-z]', host.split('.')[-1])
            or host.endswith(('.invalid', '.test', '.localhost', '.local', '.example'))
            or host in ('example.com', 'example.org', 'example.net') or '.example.' in host):
        raise PilotError('Нужен реальный согласованный HTTPS-домен без пути, порта, IP и заглушек.')
    for key in ('owner_id', 'telegram_user_id', 'chat_id'):
        if type(data[key]) is not int or not 1 <= data[key] <= 2**52-1:
            raise PilotError('Не заданы проверенные ID владельца и личного Telegram-чата.')
    if data['chat_id'] != data['telegram_user_id']:
        raise PilotError('Для этого пилота требуется один личный чат менеджера.')
    if not isinstance(data['gateway'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', data['gateway']):
        raise PilotError('Некорректное стабильное имя gateway.')
    if type(data['telegram_enabled']) is not bool:
        raise PilotError('telegram_enabled должен быть логическим значением.')
    confirmations = data['confirmations']
    if not isinstance(confirmations, dict) or set(confirmations) != set(CHECKS) or any(type(v) is not bool for v in confirmations.values()):
        raise PilotError('Нужен полный список подтверждений готовности; пока не проверено — false.')
    reference = data['approval_reference']
    if reference is not None and (not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,100}', reference)):
        raise PilotError('Некорректный несекретный номер разрешения.')
    if data['telegram_enabled'] and (not reference or not all(confirmations.values())):
        raise PilotError('Включение запрещено: нет полного набора подтверждений и номера разрешения.')
    return PilotConfig(origin, data['owner_id'], data['telegram_user_id'], data['chat_id'], data['gateway'], data['telegram_enabled'], reference, confirmations.copy())


def load_config(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise PilotError('Нужен обычный JSON-файл конфигурации до 16 КиБ, без ссылок.')
    try:
        return parse_config(path.read_bytes())
    except OSError:
        raise PilotError('Не удалось прочитать конфигурацию.') from None


def state_directory(root, *, create=False):
    root = Path(root).resolve()
    # Current full packages keep private state outside the verified source tree.
    state = (root.parent/(root.name+'-pilot-state') if (root/'PACKAGE.json').is_file()
             else root/'.local-state'/'pilot')
    marker = state/'PILOT_ONLY.json'
    for path in (state.parent, state, marker, state/'prototype.sqlite3', state/'django-secret-key', state/'STOP'):
        if path.is_symlink():
            raise PilotError('Внешние ссылки в данных пилота запрещены.')
    if state.exists():
        try:
            owned = marker.is_file() and marker.stat().st_size <= 1024 and json.loads(marker.read_text()) == {'purpose': 'synthetic-closed-pilot', 'base': BASE}
        except (OSError, ValueError):
            owned = False
        if not owned:
            raise PilotError('Папка не принадлежит этому пилоту; существующие данные не изменены.')
    elif create:
        state.mkdir(parents=True)
        marker.write_text(json.dumps({'purpose':'synthetic-closed-pilot', 'base':BASE})+'\n')
    else:
        raise PilotError('Отдельная папка пилота ещё не подготовлена.')
    return state


def stop(root):
    state = state_directory(root)
    with (state/'STOP').open('a'):
        pass


def default_config_path(root):
    root = Path(root).resolve()
    return (root.parent/(root.name+'-pilot-config.json') if (root/'PACKAGE.json').is_file()
            else root/'pilot-config.json')


def write_template(root, destination):
    """Exclusive, non-secret template outside the verified source tree; never an enabled config."""
    root = Path(root).resolve(); destination = Path(destination).absolute()
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise PilotError('Ссылки в пути конфигурации запрещены.')
    if destination.resolve().is_relative_to(root):
        raise PilotError('Конфигурация должна находиться вне исходников пакета.')
    data = {'schema':1,'purpose':'synthetic-closed-pilot','origin':'https://pilot.example.invalid',
            'owner_id':1,'telegram_user_id':None,'chat_id':None,'gateway':'izzi-closed-pilot',
            'telegram_enabled':False,'approval_reference':None,
            'confirmations':{key:False for key in CHECKS}}
    try:
        with destination.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    except OSError:
        raise PilotError('Шаблон не записан: нужен новый файл в существующей отдельной папке.') from None
