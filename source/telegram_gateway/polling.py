"""One poller per isolated data root. Reuses accept() and durable UpdateReceipt idempotency."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Protocol
from .adapter import Binding,GatewayError,accept,active_owner
from .models import PollingState

class UpdateSource(Protocol):
    def poll_status(self) -> dict: ...
    def get_updates(self, offset: int) -> list: ...

class PollingError(Exception):
    pass

@contextmanager
def exclusive(directory):
    lock=Path(directory)/'telegram-polling.lock'
    try:lock.mkdir()
    except OSError:raise PollingError('poller_locked') from None
    try:yield
    finally:lock.rmdir()


def _run(binding, source: UpdateSource, directory, *, bot_id, organization_id, enabled=False, max_cycles=100, idle_wait=lambda:None):
    if enabled is not True:raise PollingError('disabled')
    if not isinstance(binding,Binding) or not binding.valid() or binding.mode!='polling' or not binding.in_chat_review:
        raise PollingError('configuration')
    if type(bot_id) is not int or not 1<=bot_id<=2**52-1 or type(max_cycles) is not int or not 1<=max_cycles<=10000:raise PollingError('configuration')
    if binding.in_chat_buttons and binding.bot_id!=bot_id:raise PollingError('configuration')
    from inquiries.models import OrganizationIdentity
    if not OrganizationIdentity.objects.filter(organization_id=organization_id).exists():raise PollingError('organization_mismatch')
    directory=Path(directory)
    if not directory.is_dir() or any(p.is_symlink() for p in (directory,*directory.parents)):raise PollingError('unsafe_directory')
    def owner():
        user=active_owner(binding)
        if user.is_staff or user.is_superuser:raise PollingError('access_denied')
    def stopped():return (directory/'STOP').exists()
    def call(fn,*args):
        failed=False
        try:return fn(*args)
        except Exception:failed=True
        if failed:raise PollingError('transport_failure')
    if stopped():return {'status':'stopped','processed':0}
    from dataclasses import replace
    from pilot_runtime.runtime import GuardedTransport
    binding=replace(binding,transport=GuardedTransport(binding.transport,directory))
    with exclusive(directory):
        owner()
        status=call(source.poll_status)
        if (type(status) is not dict or set(status)!={'bot_id','webhook_set'}
                or type(status['bot_id']) is not int or type(status['webhook_set']) is not bool
                or status!={'bot_id':bot_id,'webhook_set':False}):raise PollingError('bot_or_webhook_mismatch')
        identity=hashlib.sha256(json.dumps([str(organization_id),bot_id,binding.owner_id,binding.telegram_user_id,binding.chat_id]).encode()).hexdigest()
        cursor,_=PollingState.objects.get_or_create(gateway=binding.gateway,defaults={'owner_id':binding.owner_id,'binding_hash':identity})
        if cursor.owner_id!=binding.owner_id or cursor.binding_hash!=identity:raise PollingError('binding_changed')
        processed=0
        for cycle in range(max_cycles):
            owner()
            if stopped():return {'status':'stopped','processed':processed}
            updates=call(source.get_updates,cursor.next_offset)
            if stopped():return {'status':'stopped','processed':processed}
            if type(updates) is not list or len(updates)>1:raise PollingError('invalid_batch')
            if not updates:
                idle_wait();continue
            update=updates[0];number=update.get('update_id') if type(update) is dict else None
            if type(number) is not int or not 0<=number<=2**52-2:raise PollingError('invalid_update')
            if number<cursor.next_offset:raise PollingError('offset_regression')
            outcome=None;failure=None
            try:outcome=accept(update,binding)
            except GatewayError as error:
                from config.diagnostics import emit
                emit('telegram',error.code)
                if error.status not in (400,403):failure='processing_failure'
            except Exception:failure='processing_failure'
            if failure:raise PollingError(failure)
            if outcome:
                from config.diagnostics import emit
                emit('telegram',outcome.get('status'));emit('reply',outcome.get('reply_state'))
            # Re-check revocation before advancing; crash before save replays through UpdateReceipt.
            owner()
            if stopped():return {'status':'stopped','processed':processed}
            cursor.next_offset=number+1;cursor.save(update_fields=['next_offset']);processed+=1
            if outcome and (outcome.get('reply_state') in ('unknown','attempted') or outcome.get('callback_ack_state') in ('unknown','attempted')):
                return {'status':'reply_uncertain','processed':processed}
        return {'status':'cycle_limit','processed':processed}


def run(*args,**kwargs):
    code=None
    try:return _run(*args,**kwargs)
    except PollingError as error:code=str(error)
    except Exception:code='local_failure'
    from config.diagnostics import emit
    emit('telegram','temporary_failure')
    raise PollingError(code)
