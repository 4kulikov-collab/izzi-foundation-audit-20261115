"""Extension boundary only. No transport, credentials, retries or CRM writes."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SyncResult:
    status: str
    message: str


class CRMAdapter(Protocol):
    def synchronize(self, *, local_id: str, version_id: str, digest: str,
                    external_id: str, idempotency_key: str) -> SyncResult: ...


class DisabledAdapter:
    def synchronize(self, **kwargs) -> SyncResult:
        return SyncResult('not_synchronized', 'Не синхронизировано: адаптер CRM не подключён.')


ADAPTERS = {'uon': DisabledAdapter(), 'amocrm': DisabledAdapter()}


@dataclass(frozen=True)
class SyncPlan:
    organization_id: str
    local_id: str
    target: str
    version_digest: str
    idempotency_key: str
    status: str


def plan_sync(*, organization_id, local_id, target, version_digest, changed_fields, field_owners=None):
    """Pure future plan, never execution. No configured owner -> conflict, not last-write-wins.
    Independent targets share Izzi UUID/history, not another CRM's external ID.
    """
    import hashlib
    import re
    from uuid import UUID
    organization_id=str(UUID(organization_id));local_id=str(UUID(local_id))
    if target not in ADAPTERS or not re.fullmatch('[0-9a-f]{64}',version_digest):
        raise ValueError('Неподдержанный получатель или версия.')
    allowed={'party','children_count','dates','budget','trip_type','beach','wishes'}
    if type(changed_fields) is not list or not set(changed_fields)<=allowed:
        raise ValueError('Нет согласованного отображения полей CRM.')
    owners=field_owners or {}
    if type(owners) is not dict or not set(owners)<=allowed or any(v not in ('izzi','uon','amocrm') for v in owners.values()):
        raise ValueError('Некорректная политика владения полями.')
    status='conflict' if any(owners.get(key)!='izzi' for key in changed_fields) else 'disabled'
    key=hashlib.sha256(f'{organization_id}:{target}:{local_id}:{version_digest}'.encode()).hexdigest()
    return SyncPlan(organization_id,local_id,target,version_digest,key,status)
