"""Persistent button UI over the same revision/form commands. No independent business data."""
import hashlib
import json
from pathlib import Path
import re
import uuid
from django.conf import settings
from django.db import transaction
from travelcards.forms import FIELD_LABELS,STATE_LABELS,normalize_fields
from worksheet_reviews.ocr_services import current_revision,initialize_ocr
from worksheet_reviews.corrections import suggestions
from worksheet_reviews.ui import recognition_label
from .adapter import active_owner,denied,existing,integer,message_base,reply_once,GatewayError
from .chat_review import execute_command,review_token
from .models import ReviewSession,UpdateReceipt

KEYS=list(FIELD_LABELS)
DATA_RE=re.compile(r'iz:([0-9a-f]{32}):(v[1-7]|ok|edit|unknown|confirm|back|cancel|hint)')
NOTICES={'stale_review':'Кнопка или шаг устарели. Проверьте актуальные поля ниже.',
         'invalid_command':'Значение не принято. Выберите поле кнопкой «Исправить»; бюджет указывайте в рублях.',
         'pending_review':'Остались кандидаты OCR. Проверьте их кнопками «Верно», «Исправить» или «Неизвестно».',
         'cancelled':'Изменение отменено. Поля не изменены.',
         'missing_review':'Сначала пришлите обезличенный PNG/JPEG листа.'}


def access(binding,lock=False):
    if (Path(settings.DATA_DIR)/'STOP').exists():raise GatewayError(503,'disabled','Песочница остановлена.')
    actor=active_owner(binding,lock=lock)
    if actor.is_staff or actor.is_superuser:raise denied()
    ctx=context(binding)
    previous=ReviewSession.objects.filter(gateway=binding.gateway,owner=actor).first()
    if previous and previous.context_hash!=ctx:raise denied()
    return actor


def context(binding):
    from inquiries.models import OrganizationIdentity
    org=OrganizationIdentity.objects.filter(slot=1).values_list('organization_id',flat=True).first()
    if org is None:raise GatewayError(503,'disabled','Компания песочницы не настроена.')
    return hashlib.sha256(json.dumps([str(org),binding.gateway,binding.owner_id,binding.telegram_user_id,binding.chat_id,binding.bot_id]).encode()).hexdigest()


def session_for(binding,actor):
    recent=UpdateReceipt.objects.filter(gateway=binding.gateway,owner=actor,review__isnull=False,review__owner=actor).order_by('-created_at','-pk').first()
    if recent is None:return None,None,False
    work=recent.review;rev=current_revision(work) or initialize_ocr(work,actor);ctx=context(binding)
    session=ReviewSession.objects.select_for_update().filter(gateway=binding.gateway,owner=actor).first()
    if session and session.context_hash!=ctx:raise denied()
    changed=bool(session and (session.review_id!=work.pk or session.revision_hash!=rev.content_hash))
    if session is None:
        session=ReviewSession(gateway=binding.gateway,owner=actor,context_hash=ctx,nonce=uuid.uuid4())
    if session.pk is None or changed:
        session.review=work;session.revision_hash=rev.content_hash;session.panel='summary';session.field_key='';session.nonce=uuid.uuid4();session.save()
    return session,rev,changed


def actions(session,rev,actor):
    if session.panel=='edit':return [('Отмена','cancel')]
    if session.panel=='field':
        field=normalize_fields(rev.fields)[session.field_key]
        if len(field_text(session,rev,actor).encode('utf-16-le'))//2>3000:return [('Назад','back')]
        buttons=[]
        if field['state']=='ocr_candidate' and field['value']:buttons.append(('Верно','ok'))
        buttons += [('Исправить','edit'),('Неизвестно','unknown'),('Назад','back')]
        if session.field_key in suggestions(session.review,actor):buttons.append(('Применить подсказку','hint'))
        return buttons
    return [(f'{i}. {FIELD_LABELS[key]}',f'v{i}') for i,key in enumerate(KEYS,1)]+[('Подтвердить','confirm')]


def field_text(session,rev,actor):
    value=normalize_fields(rev.fields)[session.field_key]
    text=f'{FIELD_LABELS[session.field_key]}: {value["value"] or "—"}\n[{STATE_LABELS[value["state"]]}]'
    hint=suggestions(session.review,actor).get(session.field_key)
    if hint:text+='\nПодсказка из подтверждённых исправлений: '+hint+'\nНе применена. Сверьте с фото; принятие — отдельной кнопкой.'
    return text


def render(session,actor):
    if session is None or session.review is None:return NOTICES['missing_review'],None
    rev=current_revision(session.review)
    if rev is None:return NOTICES['missing_review'],None
    fields=normalize_fields(rev.fields)
    status='Лист сверен.' if rev.confirmed_at else 'Лист ожидает сверки. OCR не подтверждён автоматически.'
    if session.panel=='edit':
        text=f'Введите новое значение поля «{FIELD_LABELS[session.field_key]}» одним сообщением, до 300 символов.'
        if session.field_key=='budget':text+=' Бюджет — только в рублях.'
        text+='\nДля отмены нажмите «Отмена». Пока поля не изменены.'
    elif session.panel=='field':
        text=field_text(session,rev,actor)
        if len(text.encode('utf-16-le'))//2>3000:text='Значение слишком длинное для сообщения. Нужен отдельно запущенный локальный интерфейс; скрытое значение кнопкой подтвердить нельзя.'
        text+='\n'+status
    else:
        text='Izzi · сверка обезличенного листа\n'+recognition_label(rev.metadata)+'.\n'+status+'\n'
        text+='\n'.join(f'{i}. {FIELD_LABELS[k]}: {fields[k]["value"] or "—"}\n[{STATE_LABELS[fields[k]["state"]]}]' for i,k in enumerate(KEYS,1))
        if len(text.encode('utf-16-le'))//2>3000:
            text='Izzi · '+status+'\nПолные значения открываются кнопками полей. Ничего не сокращено до подтверждённого значения.'
        text+='\nВыберите поле для проверки. «Подтвердить» доступно после ручной сверки кандидатов.'
    text+='\nU-ON: не синхронизировано. Рукопись и полевое качество не подтверждены.'
    buttons=actions(session,rev,actor)
    keyboard={'inline_keyboard':[[{'text':label,'callback_data':f'iz:{session.nonce.hex}:{action}'}] for label,action in buttons]}
    return text,keyboard


def acknowledge(receipt,binding,callback_id):
    with transaction.atomic():
        access(binding,lock=True)
        claimed=UpdateReceipt.objects.filter(pk=receipt.pk,callback_ack_state='pending').update(callback_ack_state='attempted')
    if claimed:
        access(binding)
        try:binding.transport.answer_callback(callback_id,'Действие обработано. Проверьте сообщение Izzi.',timeout_seconds=10)
        except Exception:state='unknown'
        else:state='sent'
        UpdateReceipt.objects.filter(pk=receipt.pk).update(callback_ack_state=state)
    receipt.refresh_from_db(fields=['callback_ack_state'])


def deliver(receipt,binding,repeated,callback_id=None):
    actor=access(binding)
    session=ReviewSession.objects.filter(gateway=binding.gateway,owner=actor).select_related('review').first()
    if session and session.context_hash!=context(binding):raise denied()
    if session and session.review and session.review.owner_id!=actor.pk:raise denied()
    text,keyboard=render(session,actor)
    if receipt.result in NOTICES:text=NOTICES[receipt.result]+'\n\n'+text
    if callback_id:acknowledge(receipt,binding,callback_id)
    access(binding)
    state=reply_once(receipt,binding,text,keyboard=keyboard);access(binding)
    return {'status':'review_command','text':text,'reply_state':state,'callback_ack_state':receipt.callback_ack_state,
        'repeated':repeated,'crm':'not_synchronized','review_path':None}


def photo_reply(receipt,binding,repeated):
    # Rejecting a new image never reopens or silently confirms an older work.
    if receipt.result=='rejected' or receipt.review is None:
        with transaction.atomic():
            actor=access(binding,lock=True)
            if not repeated:
                ReviewSession.objects.filter(gateway=binding.gateway,owner=actor).update(panel='summary',field_key='',nonce=uuid.uuid4())
        from .adapter import result
        response=result(receipt,binding);response['reply_state']=reply_once(receipt,binding,response['text']);response['repeated']=repeated
        return response
    with transaction.atomic():
        actor=access(binding,lock=True)
        session,rev,_=session_for(binding,actor)
        if not repeated and session:
            session.panel='summary';session.field_key='';session.nonce=uuid.uuid4();session.save()
    return deliver(receipt,binding,repeated)


def callback_envelope(update,binding):
    if type(update) is not dict or set(update)!={'update_id','callback_query'} or not integer(update.get('update_id')):raise GatewayError(400,'invalid_update','Некорректное событие кнопки.')
    q=update['callback_query']
    if type(q) is not dict or 'inline_message_id' in q or not isinstance(q.get('id'),str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}',q['id']):raise GatewayError(400,'invalid_update','Некорректное событие кнопки.')
    message=q.get('message');sender=q.get('from')
    if type(message) is not dict or type(sender) is not dict:raise denied()
    bot=message.get('from');chat=message.get('chat')
    if (sender.get('id')!=binding.telegram_user_id or not integer(sender.get('id'),1) or sender.get('is_bot') is not False
        or type(bot) is not dict or bot.get('id')!=binding.bot_id or not integer(bot.get('id'),1) or bot.get('is_bot') is not True
        or type(chat) is not dict or chat.get('type')!='private' or chat.get('id')!=binding.chat_id or not integer(chat.get('id'),1)
        or not integer(message.get('message_id'),1) or any(k in message for k in ('forward_origin','forward_from','sender_chat'))):raise denied()
    if not isinstance(q.get('data'),str) or not DATA_RE.fullmatch(q['data']):raise GatewayError(400,'invalid_update','Некорректная кнопка.')
    key=hashlib.sha256(('callback:'+q['id']).encode()).hexdigest()
    fingerprint=hashlib.sha256(json.dumps([key,q['data'],message['message_id'],bot['id'],chat['id'],sender['id']]).encode()).hexdigest()
    return update['update_id'],key,fingerprint,q


def process(binding,update_id,key,fingerprint,*,text=None,callback=None):
    actor=access(binding);receipt=existing(binding,update_id,key,fingerprint);repeated=receipt is not None
    if receipt is None:
        with transaction.atomic():
            actor=access(binding,lock=True);receipt=existing(binding,update_id,key,fingerprint)
            if receipt is not None:repeated=True
            else:
                session,rev,changed=session_for(binding,actor)
                outcome='missing_review'
                if session:
                    work=session.review;outcome='accepted';token=review_token(work,rev)
                    if callback:
                        nonce,action=DATA_RE.fullmatch(callback['data']).groups()
                        allowed={a for _,a in actions(session,rev,actor)}
                        if changed or nonce!=session.nonce.hex or action not in allowed:outcome='stale_review'
                        elif action.startswith('v'):
                            session.panel='field';session.field_key=KEYS[int(action[1:])-1]
                        elif action=='edit':session.panel='edit'
                        elif action=='cancel':session.panel='field';outcome='cancelled'
                        elif action=='back':session.panel='summary';session.field_key=''
                        else:
                            command={'ok':'ВЕРНО','unknown':'НЕИЗВЕСТНО','hint':'ПРИМЕНИТЬ','confirm':'ПОДТВЕРДИТЬ'}[action]
                            suffix='' if action=='confirm' else ' '+str(KEYS.index(session.field_key)+1)
                            outcome=execute_command(work,actor,rev,command+' '+token+suffix)
                            if action=='confirm':session.panel='summary';session.field_key=''
                    else:
                        stripped=text.strip();head=stripped.split(maxsplit=1)[0].upper() if stripped else ''
                        if head in ('ОТМЕНА','/CANCEL') and len(stripped.split())==1:
                            session.panel='summary';session.field_key='';outcome='cancelled'
                        elif head in ('ПОЛЯ','/START','/HELP') and len(stripped.split())==1:
                            session.panel='summary';session.field_key=''
                        elif changed:outcome='stale_review'
                        elif session.panel=='edit' and head not in ('ВЕРНО','ИСПРАВИТЬ','НЕИЗВЕСТНО','ПРИМЕНИТЬ','ПОДТВЕРДИТЬ'):
                            outcome=execute_command(work,actor,rev,f'ИСПРАВИТЬ {token} {KEYS.index(session.field_key)+1} {stripped}')
                            if outcome=='accepted':session.panel='field'
                        else:
                            outcome=execute_command(work,actor,rev,stripped);session.panel='summary';session.field_key=''
                    session.revision_hash=current_revision(work).content_hash;session.nonce=uuid.uuid4();session.save()
                receipt=UpdateReceipt.objects.create(gateway=binding.gateway,owner=actor,update_id=update_id,message_key=key,payload_hash=fingerprint,
                    review=session.review if session else None,result=outcome,callback_ack_state='pending' if callback else 'not_required')
    return deliver(receipt,binding,repeated,callback['id'] if callback else None)


def callback_accept(update,binding):
    access(binding)
    update_id,key,fingerprint,callback=callback_envelope(update,binding)
    return process(binding,update_id,key,fingerprint,callback=callback)


def message_accept(update,binding):
    update_id,message=message_base(update,binding);text=message.get('text')
    if not isinstance(text,str) or not 1<=len(text)<=1000 or message.get('photo') or message.get('document'):raise GatewayError(400,'invalid_message','Нужно одно текстовое значение.')
    key=hashlib.sha256(f'{binding.telegram_user_id}:{binding.chat_id}:{message["message_id"]}'.encode()).hexdigest()
    fingerprint=hashlib.sha256(json.dumps([key,'text',text],ensure_ascii=True).encode()).hexdigest()
    return process(binding,update_id,key,fingerprint,text=text)
