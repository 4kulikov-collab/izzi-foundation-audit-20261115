"""Trusted, versioned local presentation registry. No uploaded HTML/CSS or remote assets."""
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from django.core.exceptions import ValidationError
from django.template.loader import get_template

BODY = 'presentations/themes/body_v1.html'
OFFER = 'presentations/offer_content.html'

@dataclass(frozen=True)
class Theme:
    id: str
    version: int
    label: str
    style: str
    body: str = BODY
    @property
    def key(self):return f'{self.id}:{self.version}'


def registry(definitions):
    values={}
    for item in definitions:
        if (not isinstance(item,Theme) or not re.fullmatch('[a-z][a-z0-9_-]{0,39}',item.id)
            or type(item.version) is not int or not 1<=item.version<=9999
            or not re.fullmatch(r'presentations/themes/[a-z0-9_]+\.html',item.style)
            or not re.fullmatch(r'presentations/themes/[a-z0-9_]+\.html',item.body)
            or item.key in values):raise ValueError('Invalid trusted template registry')
        values[item.key]=item
    return MappingProxyType(values)

REGISTRY=registry((Theme('cards',1,'Карточки · пример 1','presentations/themes/cards_v1.html'),
                   Theme('brief',1,'Краткий обзор · пример 1','presentations/themes/brief_v1.html')))


def resolve(key):
    if type(key) is not str or key not in REGISTRY:
        raise ValidationError('Оформление не найдено. Выберите поддерживаемый шаблон и версию.')
    return REGISTRY[key]


def descriptor(key):
    theme=resolve(key)
    # A source change invalidates outstanding approvals, even if a developer forgot a version bump.
    assets={name:get_template(name).template.source for name in (theme.body,OFFER,theme.style)}
    checksum=hashlib.sha256(json.dumps(assets,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'id':theme.id,'version':theme.version,'assets_sha256':checksum}


def for_snapshot(snapshot):
    spec=snapshot.get('presentation')
    if spec is None:
        # Old stored releases are served from saved bytes, never regenerated here.
        if snapshot.get('template_version') not in (None,'prototype-b-1','prototype-b-2','prototype-b-3','prototype-b-4','prototype-b-5'):
            raise ValidationError('Неизвестная версия старого шаблона.')
        return resolve('cards:1')
    if type(spec) is not dict or set(spec)!={'id','version','assets_sha256'} or type(spec['id']) is not str or type(spec['version']) is not int:
        raise ValidationError('Некорректный контракт оформления.')
    key=f"{spec['id']}:{spec['version']}"
    if spec!=descriptor(key):raise ValidationError('Исходники оформления изменились. Требуется новая проверка.')
    return resolve(key)


def choices():return [(key,t.label) for key,t in REGISTRY.items()]
