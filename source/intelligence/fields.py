"""Provider-neutral draft boundary. No provider SDK, credential loader, network or memory store."""
from dataclasses import dataclass
from typing import Protocol
from travelcards.forms import FIELD_LABELS

SCHEMA='izzi.fields-draft.v1'
STATES={'ocr_candidate','unknown','unreadable','conflict','not_important','declined','not_applicable'}


class Unavailable(Exception):pass


class Adapter(Protocol):
    capabilities: frozenset[str]
    def extract(self, *, kind: str, content: bytes | str, schema: str) -> dict: ...


@dataclass(frozen=True)
class DisabledAdapter:
    capabilities: frozenset[str] = frozenset()
    def extract(self, **kwargs):raise Unavailable()


def validate(payload):
    if type(payload) is not dict or set(payload)!={'schema','fields'} or payload['schema']!=SCHEMA:
        raise ValueError()
    if type(payload['fields']) is not dict or set(payload['fields'])!=set(FIELD_LABELS):raise ValueError()
    result={}
    for key,field in payload['fields'].items():
        if type(field) is not dict or set(field)!={'value','state'}:raise ValueError()
        value,state=field['value'],field['state']
        if not isinstance(value,str) or len(value)>600 or state not in STATES or any(ord(c)<32 and c not in '\n\t' for c in value):raise ValueError()
        if state in {'unknown','not_important','declined','not_applicable'} and value:raise ValueError()
        result[key]={'value':value,'state':state,'source':{'kind':'model_candidate','page':None,
            'description':'Кандидат сменяемого ИИ-адаптера. Проверить по исходнику; не подтверждено автоматически.'}}
    result['budget']['currency']='RUB'
    # Existing domain rules detect foreign currency; never convert money or confirm provider output.
    from travelcards.forms import normalize_fields
    return normalize_fields(result)


def extract(adapter, *, kind, content):
    failure='unavailable'
    try:
        capabilities=adapter.capabilities
        if type(capabilities) is not frozenset or not capabilities <= {'text','image'}:raise ValueError()
        if kind not in capabilities:raise Unavailable()
        if kind=='image':
            if type(content) is not bytes or not 0<len(content)<=5*1024*1024:raise ValueError()
        elif kind=='text':
            if not isinstance(content,str) or not 0<len(content)<=10000:raise ValueError()
        else:raise ValueError()
        fields=validate(adapter.extract(kind=kind,content=content,schema=SCHEMA))
        return fields,{'status':'recognized','engine':'replaceable_adapter','schema':SCHEMA,
                       'note':'Кандидаты ИИ; ручная сверка обязательна. Вызов не означает подтверждение поставщика.'}
    except (Unavailable,TimeoutError):pass
    except Exception:failure='error'
    note='ИИ-адаптер не подключён, недоступен или вернул неподдержанный результат. Поля не угаданы; требуется ручная сверка.'
    fields={key:{'value':'','state':'unknown','source':{'kind':'manual_fallback','page':None,'description':note}} for key in FIELD_LABELS}
    fields['budget']['currency']='RUB'
    return fields,{'status':failure,'engine':'replaceable_adapter','schema':SCHEMA,'note':note}
