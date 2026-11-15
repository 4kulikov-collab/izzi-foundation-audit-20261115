"""Explicit future local outbound pilot. No server, webhook registration or token file/env loader."""
import argparse
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import warnings
from uuid import UUID

CHECKS=('owner_approval','isolated_device','bot_and_chat_verified','no_webhook_confirmed','anonymized_only')

@dataclass(frozen=True,repr=False)
class Config:
    owner_id:int
    telegram_user_id:int
    bot_id:int
    organization_id:str
    gateway:str
    enabled:bool


def load(path):
    p=Path(path)
    if any(x.is_symlink() for x in (p,*p.parents)) or not p.is_file() or p.stat().st_size>16384:raise ValueError()
    from pilot_runtime.configuration import unique_object
    data=json.loads(p.read_bytes(),object_pairs_hook=unique_object)
    if type(data) is not dict or set(data)!={'schema','purpose','owner_id','telegram_user_id','chat_id','bot_id','organization_id','gateway','enabled','approval_reference','confirmations'}:raise ValueError()
    if type(data['schema']) is not int or data['schema']!=1 or data['purpose']!='synthetic-polling-pilot' or type(data['enabled']) is not bool:raise ValueError()
    for key in ('owner_id','telegram_user_id','chat_id','bot_id'):
        if type(data[key]) is not int or not 1<=data[key]<=2**52-1:raise ValueError()
    if data['chat_id']!=data['telegram_user_id']:raise ValueError()
    if str(UUID(data['organization_id']))!=data['organization_id'] or not re.fullmatch('[A-Za-z0-9_-]{1,64}',data['gateway']):raise ValueError()
    checks=data['confirmations']
    if type(checks) is not dict or set(checks)!=set(CHECKS) or any(type(v) is not bool for v in checks.values()):raise ValueError()
    ref=data['approval_reference']
    if ref is not None and (not isinstance(ref,str) or not re.fullmatch('[A-Za-z0-9_-]{3,100}',ref)):raise ValueError()
    if data['enabled'] and (not all(checks.values()) or not ref):raise ValueError()
    return Config(data['owner_id'],data['telegram_user_id'],data['bot_id'],data['organization_id'],data['gateway'],data['enabled'])


def configure(directory,cfg):
    from django.conf import settings
    if settings.configured:raise ValueError()
    p=Path(directory).absolute()
    if not p.is_dir() or any(x.is_symlink() for x in (p,*p.parents)):raise ValueError()
    for name in ('IZZI_SYNTHETIC_UI.json','prototype.sqlite3','django-secret-key','STOP'):
        if (p/name).is_symlink():raise ValueError()
    if (p/'STOP').exists() or not (p/'prototype.sqlite3').is_file():raise ValueError()
    if (p/'IZZI_SYNTHETIC_UI.json').stat().st_size>4096:raise ValueError()
    marker=json.loads((p/'IZZI_SYNTHETIC_UI.json').read_bytes())
    if marker.get('schema')!='izzi-synthetic-ui-v1' or marker.get('synthetic_only') is not True or marker.get('organization_id')!=cfg.organization_id:raise ValueError()
    os.environ.update(DJANGO_SETTINGS_MODULE='config.settings',TRAVELHUB_DATA_DIR=str(p),XDG_CACHE_HOME=str(p/'cache'))
    import django
    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from inquiries.models import OrganizationIdentity
    from django.contrib.auth import get_user_model
    if MigrationExecutor(connection).migration_plan(MigrationExecutor(connection).loader.graph.leaf_nodes()):raise ValueError()
    if not OrganizationIdentity.objects.filter(slot=1,organization_id=cfg.organization_id).exists():raise ValueError()
    if not get_user_model().objects.filter(pk=cfg.owner_id,username=marker['manager'],is_active=True,is_staff=False,is_superuser=False).exists():raise ValueError()
    settings.TELEGRAM_GATEWAY=None  # This profile never exposes a webhook binding.
    settings.VOICE_INQUIRY_BOUNDARY_ENABLED=False
    settings.SANDBOX_DIAGNOSTICS_ENABLED=True
    settings.SANDBOX_DIAGNOSTICS_SCOPE='live-unverified'
    return p


def read_token():
    from pilot_runtime.secrets_boundary import MemoryToken
    if not sys.stdin.isatty():raise ValueError()
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        return MemoryToken(getpass.getpass('Токен отдельного тестового бота (скрыто): '))


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('check','run','stop'))
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--confirm-telegram',action='store_true')
    parser.add_argument('--cycles',type=int,default=100)
    args=parser.parse_args(argv)
    try:
        root=Path(__file__).resolve().parents[1]
        if args.action=='stop':
            p=args.directory.absolute()
            if not p.is_dir() or any(x.is_symlink() for x in (p,*p.parents,p/'STOP',p/'IZZI_SYNTHETIC_UI.json')):raise ValueError()
            marker=json.loads((p/'IZZI_SYNTHETIC_UI.json').read_bytes())
            if marker.get('schema')!='izzi-synthetic-ui-v1' or marker.get('synthetic_only') is not True:raise ValueError()
            (p/'STOP').touch();print('STOP установлен. Начатая отправка могла завершиться.');return 0
        from stage_one_demo.package import verify
        verify(root)
        if not args.config or not 1<=args.cycles<=10000:raise ValueError()
        cfg=load(args.config)
        if args.action=='run' and (not cfg.enabled or not args.confirm_telegram):raise ValueError()
        from pilot_runtime.preflight import local_ocr_ready
        if not local_ocr_ready():raise ValueError()
        state=configure(args.directory,cfg)
        if args.action=='check':
            print(json.dumps({'local_database_and_ocr_checked':True,'live_telegram_verified':False,'network_used':False,'server_started':False}));return 0
        from .http_transport import TelegramPollingHTTPTransport
        from .adapter import Binding
        from .polling import run,PollingError
        # No secret read until local schema/organization/owner/OCR checks pass.
        transport=TelegramPollingHTTPTransport(read_token(),enabled=True)
        binding=Binding(cfg.gateway,cfg.owner_id,cfg.telegram_user_id,cfg.telegram_user_id,None,transport,in_chat_review=True,mode='polling',in_chat_buttons=True,bot_id=cfg.bot_id)
        def terminate(*unused):raise KeyboardInterrupt()
        previous=signal.signal(signal.SIGTERM,terminate)
        try:result=run(binding,transport,state,bot_id=cfg.bot_id,organization_id=cfg.organization_id,enabled=True,max_cycles=args.cycles,idle_wait=lambda:time.sleep(1))
        except PollingError as error:
            print('Polling остановлен: '+str(error),file=sys.stderr);return 2
        finally:signal.signal(signal.SIGTERM,previous)
        print(json.dumps(result));return 0
    except KeyboardInterrupt:
        print('Получение остановлено. Начатая отправка могла завершиться.');return 2
    except Exception:
        print('Polling не запущен или остановлен из-за ошибки. Секреты и содержимое скрыты; автоматического повтора нет.',file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
