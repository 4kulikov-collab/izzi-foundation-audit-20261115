"""Opt-in text interface for one manager. Uses existing OCR revisions, never CRM writes."""
import hashlib
import json
import re
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from travelcards.forms import FIELD_LABELS, STATE_LABELS, normalize_fields
from worksheet_reviews.ocr_services import current_revision, initialize_ocr, save_fields
from worksheet_reviews.ui import recognition_label
from .models import UpdateReceipt

ERRORS = {'invalid_command':'Команда не принята. Отправьте ПОЛЯ для актуальных инструкций.',
          'stale_review':'Версия листа изменилась. Отправьте ПОЛЯ; правка не применена.',
          'pending_review':'Есть непроверенные кандидаты OCR. Проверьте каждый: ВЕРНО, ИСПРАВИТЬ или НЕИЗВЕСТНО.',
          'missing_review':'Сначала пришлите обезличенный PNG/JPEG листа.'}
KEYS = list(FIELD_LABELS)


from worksheet_reviews.corrections import suggestions


def review_token(work, revision):
    return hashlib.sha256(f'{work.pk}:{revision.content_hash}'.encode()).hexdigest()[:16]


def describe(work,binding):
    from .adapter import active_owner
    actor=active_owner(binding)
    if work is None or work.owner_id != actor.pk:
        from .adapter import denied
        raise denied()
    revision=current_revision(work) or initialize_ocr(work,actor)
    fields=normalize_fields(revision.fields);token=review_token(work,revision);hints=suggestions(work,actor)
    lines=['Izzi · проверка обезличенного листа',recognition_label(revision.metadata)+'. Рукопись и полевое качество не подтверждены.']
    for index,key in enumerate(KEYS,1):
        f=fields[key];value=f['value'] or '—'
        lines.append(f"{index}. {FIELD_LABELS[key]}: {value}\n[{STATE_LABELS[f['state']]}]")
        if key in hints:
            lines.append(f'Подсказка из подтверждённых исправлений: {hints[key]} (проверьте по фото).')
            lines.append(f'ПРИМЕНИТЬ {token} {index} — принять эту подсказку после проверки.')
    lines += ['Лист сверен.' if revision.confirmed_at else 'Лист ожидает сверки; OCR не подтверждён автоматически.',
        f'Версия: {token}',f'ВЕРНО {token} 1 — проверить поле 1 без изменения.',
        f'ИСПРАВИТЬ {token} 4 250000 — пример команды, не рекомендованная сумма.',
        f'НЕИЗВЕСТНО {token} 4 — очистить неизвестное поле.',
        f'ПОДТВЕРДИТЬ {token} — сохранить проверку. ПОЛЯ — показать заново.',
        'Только обезличенные данные. База отдельная. U-ON: не синхронизировано.']
    text='\n'.join(lines)
    # Never truncate a value into apparent certainty. Long drafts remain available in private UI.
    if len(text.encode('utf-16-le'))//2>3500:
        text='Лист сохранён, но поля слишком длинные для одного сообщения. Ничего не подтверждено автоматически. Откройте приватное рабочее место для сверки. U-ON: не синхронизировано.'
    if binding.mode=='polling' and 'поля слишком длинные' in text:
        text='Лист сохранён. Поля слишком длинные для одного сообщения; ничего не подтверждено автоматически. Нужен отдельно запущенный локальный интерфейс для сверки; polling его не запускает. U-ON: не синхронизировано.'
    if binding.review_origin:text+='\n'+binding.review_origin+reverse('worksheet_reviews:detail',args=[work.pk])
    return text



def execute_command(work,actor,revision,text):
    """One mutation path for text and buttons; same form/version/confirmation checks."""
    outcome='accepted'
    parts=text.strip().split(maxsplit=3)
    if not parts:return 'invalid_command'
    command=parts[0].upper()
    if command in ('ПОЛЯ','/START','/HELP') and len(parts)==1:pass
    elif not revision:outcome='missing_review'
    elif len(parts)<2 or parts[1]!=review_token(work,revision):outcome='stale_review'
    else:
        normalized=normalize_fields(revision.fields)
        fields={k:normalized[k] for k in KEYS}
        try:
            if command=='ПОДТВЕРДИТЬ' and len(parts)==2:
                if any(f['state']=='ocr_candidate' for f in fields.values()):outcome='pending_review'
                else:save_fields(work,actor,revision.number,revision.content_hash,fields,confirm=True)
            elif command in ('ВЕРНО','ИСПРАВИТЬ','НЕИЗВЕСТНО','ПРИМЕНИТЬ') and len(parts)>=3 and re.fullmatch('[1-7]',parts[2]):
                keyfield=KEYS[int(parts[2])-1];field=fields[keyfield]
                value=field['value'];state=field['state']
                if command=='ИСПРАВИТЬ':
                    if len(parts)!=4 or not parts[3].strip() or len(parts[3])>300:raise ValueError()
                    value=parts[3].strip();state='known'
                else:
                    if len(parts)!=3:raise ValueError()
                    if command=='НЕИЗВЕСТНО':value='';state='unknown'
                    elif command=='ВЕРНО':
                        if state!='ocr_candidate' or not value:raise ValueError()
                        state='known'
                    else:
                        value=suggestions(work,actor).get(keyfield)
                        if not value:raise ValueError()
                        state='known'
                field.update(value=value,state=state,source={'kind':'manual_review','page':1,'description':'Проверено менеджером в Telegram-песочнице.'})
                # Apply the existing form's budget/currency/type validation as well as revision checks.
                from travelcards.forms import CardFieldsForm
                formdata={f'{k}_{part}':v[part] for k,v in fields.items() for part in ('value','state')}
                formdata.update({f'{k}_source':v['source']['description'] for k,v in fields.items()})
                formdata.update(expected_revision=revision.number,expected_hash=revision.content_hash,budget_currency=fields['budget'].get('currency','RUB'))
                form=CardFieldsForm(formdata,revision=revision)
                if not form.is_valid():raise ValueError()
                save_fields(work,actor,revision.number,revision.content_hash,form.field_snapshot())
            else:raise ValueError()
        except (ValueError,ValidationError):outcome='invalid_command'
    return outcome

def text_accept(update,binding):
    if binding.in_chat_buttons:
        from .review_ui import message_accept
        return message_accept(update,binding)
    from .adapter import message_base,active_owner,existing,reply_once,GatewayError
    update_id,message=message_base(update,binding);actor=active_owner(binding)
    text=message.get('text')
    if not isinstance(text,str) or not 1<=len(text)<=1000:
        raise GatewayError(400,'invalid_message','Текст команды должен быть от 1 до 1000 символов.')
    if message.get('photo') or message.get('document'):raise GatewayError(400,'unsupported_message','Отправьте изображение или одну команду отдельно.')
    key=hashlib.sha256(f'{binding.telegram_user_id}:{binding.chat_id}:{message["message_id"]}'.encode()).hexdigest()
    fingerprint=hashlib.sha256(json.dumps([key,'text',text],ensure_ascii=True).encode()).hexdigest()
    receipt=existing(binding,update_id,key,fingerprint);repeated=receipt is not None
    if receipt is None:
        with transaction.atomic():
            actor=active_owner(binding,lock=True)
            receipt=existing(binding,update_id,key,fingerprint)
            if receipt is not None:repeated=True
            else:
                recent=UpdateReceipt.objects.filter(gateway=binding.gateway,owner=actor,review__isnull=False,review__owner=actor).order_by('-created_at','-pk').first()
                work=recent.review if recent else None
                outcome='accepted' if work else 'missing_review'
                if work:
                    revision=current_revision(work)
                    outcome=execute_command(work,actor,revision,text)
                receipt=UpdateReceipt.objects.create(gateway=binding.gateway,update_id=update_id,message_key=key,payload_hash=fingerprint,
                    owner=actor,review=work,result=outcome)
    active_owner(binding)
    from config.diagnostics import emit
    if receipt.result in ERRORS:emit('review',receipt.result)
    output=ERRORS.get(receipt.result) or (describe(receipt.review,binding) if receipt.review else 'Лист удалён. Пришлите новый обезличенный PNG/JPEG.')
    reply=reply_once(receipt,binding,output);active_owner(binding)
    return {'status':'review_command','text':output,'reply_state':reply,'repeated':repeated,'crm':'not_synchronized',
            'review_path':reverse('worksheet_reviews:detail',args=[receipt.review_id]) if receipt.review_id else None}
