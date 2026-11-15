"""Interactive future provisioning -> immutable memory; no env/argv/file/network secret loader."""
from dataclasses import dataclass, field
import getpass
import re
import sys
import warnings
from .configuration import PilotError


@dataclass(frozen=True, repr=False)
class MemoryToken:
    _value: str = field(repr=False)

    def __post_init__(self):
        if type(self._value) is not str or not re.fullmatch(r'[0-9]{1,20}:[A-Za-z0-9_-]{20,200}', self._value):
            raise PilotError('Некорректный bot token; значение скрыто.')

    def __call__(self):
        return self._value

    def __repr__(self):
        return '<MemoryToken hidden>'


@dataclass(frozen=True, repr=False)
class SecretBundle:
    token: MemoryToken = field(repr=False)
    webhook: str = field(repr=False)

    def __post_init__(self):
        if type(self.token) is not MemoryToken or type(self.webhook) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', self.webhook):
            raise PilotError('Нужен проверенный набор секретов только в памяти.')

    @classmethod
    def from_values(cls, token, webhook):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9]{1,20}:[A-Za-z0-9_-]{20,200}', token):
            raise PilotError('Некорректный bot token; значение скрыто.')
        if not isinstance(webhook, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', webhook):
            raise PilotError('Некорректный отдельный webhook-secret; значение скрыто.')
        return cls(MemoryToken(token), webhook)

    def __repr__(self):
        return '<SecretBundle hidden>'


def read_interactive():
    if not sys.stdin.isatty():
        raise PilotError('Токены вводятся только в согласованном интерактивном терминале со скрытым вводом.')
    values = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            values = (getpass.getpass('Bot token (скрыто): '), getpass.getpass('Отдельный webhook-secret (скрыто): '))
    except (getpass.GetPassWarning, OSError, EOFError):
        pass
    if values is None:
        raise PilotError('Безопасный ввод не состоялся; секреты не установлены.')
    return SecretBundle.from_values(*values)
