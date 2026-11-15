"""Telegram message ingress into the existing manager workflow. Network transport is separately opt-in."""
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Protocol
from urllib.parse import urlsplit
import uuid
import warnings

from PIL import Image

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.urls import reverse

from worksheet_reviews.forms import inspect_worksheet
from worksheet_reviews.services import create_review
from worksheet_reviews.ui import work_state
from .models import UpdateReceipt
from .http_transport import FileRejected

MAX_FILE_BYTES = 5 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


class Transport(Protocol):
    def iter_file(self, file_id, *, max_bytes, chunk_size, timeout_seconds):
        """Yield bounded bytes for Telegram file_id, not a URL/path; close on cancellation."""
        ...

    def send_message(self, chat_id, text, *, timeout_seconds):
        """Return after confirmed success, or raise; never log credentials/body/URLs."""
        ...


@dataclass(frozen=True)
class Binding:
    gateway: str
    owner_id: int
    telegram_user_id: int = field(repr=False)
    chat_id: int = field(repr=False)
    webhook_secret: str | None = field(repr=False)
    transport: Transport = field(repr=False)
    review_origin: str = ''
    in_chat_review: bool = False
    mode: str = 'webhook'
    in_chat_buttons: bool = False
    bot_id: int | None = None

    def valid(self):
        if type(self.in_chat_buttons) is not bool:return False
        if self.in_chat_buttons and (not self.in_chat_review or not integer(self.bot_id,1) or not callable(getattr(self.transport,'send_buttons',None)) or not callable(getattr(self.transport,'answer_callback',None))):return False
        if type(self.in_chat_review) is not bool:
            return False
        if not isinstance(self.gateway, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.gateway):
            return False
        if not all(integer(value, minimum=1) for value in (self.owner_id, self.telegram_user_id, self.chat_id)):
            return False
        if self.telegram_user_id != self.chat_id:
            return False  # This slice accepts one personal manager chat only.
        if self.mode not in ('webhook','polling'):return False
        if self.mode == 'webhook':
            if not isinstance(self.webhook_secret,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',self.webhook_secret):return False
        elif self.webhook_secret is not None or self.review_origin:return False
        if not callable(getattr(self.transport, 'iter_file', None)) or not callable(getattr(self.transport, 'send_message', None)):
            return False
        if not isinstance(self.review_origin, str):
            return False
        if self.review_origin:
            parsed = urlsplit(self.review_origin)
            if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path
                    or not parsed.hostname or (parsed.scheme != 'https' and not (
                        parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')))):
                return False
        return True


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**52 - 1


class GatewayError(Exception):
    def __init__(self, status, code, text):
        self.status, self.code, self.text = status, code, text
        super().__init__(text)


def denied():
    return GatewayError(403, 'access_denied', 'Доступ к этому входу не разрешён.')


def active_owner(binding, *, lock=False):
    query = get_user_model().objects
    if lock:
        query = query.select_for_update()
    owner = query.filter(pk=binding.owner_id, is_active=True).first()
    if owner is None:
        raise denied()
    return owner


def message_base(update, binding):
    if not isinstance(update, dict) or not integer(update.get('update_id')):
        raise GatewayError(400, 'invalid_update', 'Некорректное событие Telegram.')
    message = update.get('message')
    if not isinstance(message, dict) or any(k in update for k in ('edited_message', 'channel_post', 'edited_channel_post', 'callback_query', 'business_message')):
        raise GatewayError(400, 'unsupported_update', 'Поддерживается новое личное сообщение с одним изображением листа.')
    sender, chat = message.get('from'), message.get('chat')
    if (not isinstance(sender, dict) or not isinstance(chat, dict)
            or not integer(sender.get('id'), 1) or sender['id'] != binding.telegram_user_id
            or sender.get('is_bot') is not False or chat.get('type') != 'private'
            or not integer(chat.get('id'), 1) or chat['id'] != binding.chat_id):
        raise denied()
    if not integer(message.get('message_id'), 1):
        raise GatewayError(400, 'invalid_message', 'Некорректный номер сообщения.')
    if any(k in message for k in ('forward_origin', 'forward_from', 'sender_chat', 'via_bot', 'media_group_id')):
        raise GatewayError(400, 'unsupported_message', 'Отправьте один лист напрямую, без пересылки или альбома.')
    return update['update_id'], message


def parse(update, binding):
    update_id, message = message_base(update, binding)
    if bool(message.get('photo')) == bool(message.get('document')):
        raise GatewayError(400, 'image_required', 'Пришлите один PNG/JPEG как файл или фотографию.')
    if message.get('photo'):
        photos = message['photo']
        if not isinstance(photos, list) or not 1 <= len(photos) <= 20 or not all(
                isinstance(p, dict) and integer(p.get('width'), 1) and integer(p.get('height'), 1) for p in photos):
            raise GatewayError(400, 'invalid_photo', 'Некорректные размеры фотографии.')
        attachment = max(photos, key=lambda p: p['width'] * p['height'])
        if attachment['width'] * attachment['height'] > 12_000_000:
            raise GatewayError(400, 'image_too_large', 'Принимаются изображения до 12 мегапикселей.')
        mime = 'image/jpeg'
    else:
        attachment = message['document']
        if not isinstance(attachment, dict) or attachment.get('mime_type') not in ('image/jpeg', 'image/png'):
            raise GatewayError(400, 'image_required', 'Пришлите PNG/JPEG; другие типы здесь не поддерживаются.')
        mime = attachment['mime_type']
    file_id = attachment.get('file_id')
    if not isinstance(file_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', file_id):
        raise GatewayError(400, 'invalid_file', 'Некорректный идентификатор изображения.')
    size = attachment.get('file_size')
    if size is not None and (not integer(size, 1) or size > MAX_FILE_BYTES):
        raise GatewayError(400, 'image_too_large', 'Изображение должно быть непустым и не больше 5 МБ.')
    key = hashlib.sha256(f'{binding.telegram_user_id}:{binding.chat_id}:{message["message_id"]}'.encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([key, file_id, mime, size], separators=(',', ':')).encode()).hexdigest()
    return update_id, key, fingerprint, file_id, mime


def existing(binding, update_id, message_key, fingerprint):
    receipt = UpdateReceipt.objects.select_related('review').filter(gateway=binding.gateway, update_id=update_id).first()
    if receipt:
        if receipt.owner_id != binding.owner_id:
            raise denied()
        if receipt.payload_hash != fingerprint:
            raise GatewayError(409, 'update_conflict', 'Содержимое повторного события отличается; новая работа не создана.')
        return receipt
    if UpdateReceipt.objects.filter(gateway=binding.gateway, message_key=message_key).exists():
        raise GatewayError(409, 'message_conflict', 'Это сообщение уже учтено под другим событием; новая работа не создана.')
    return None


def download(binding, file_id, mime):
    stream = None
    try:
        stream = iter(binding.transport.iter_file(file_id, max_bytes=MAX_FILE_BYTES, chunk_size=CHUNK_BYTES, timeout_seconds=15))
        data = bytearray()
        for index, chunk in enumerate(stream):
            if type(chunk) is not bytes or len(chunk) > CHUNK_BYTES or index > 1024:
                raise ValidationError('Некорректный поток изображения.')
            data.extend(chunk)
            if len(data) > MAX_FILE_BYTES:
                raise ValidationError('Изображение превышает 5 МБ.')
        # Shared byte/type/pixel validation; never trust the filename or MIME alone.
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            content, actual_mime = inspect_worksheet(SimpleUploadedFile('telegram-image', bytes(data), content_type=mime))
        if actual_mime not in ('image/jpeg', 'image/png'):
            raise ValidationError('Нужен PNG/JPEG.')
        return content, actual_mime
    except (ValidationError, Image.DecompressionBombError, Image.DecompressionBombWarning, FileRejected):
        return None
    except Exception:
        raise GatewayError(503, 'file_unavailable', 'Не удалось получить изображение. Работа не создана; доставку события можно повторить.') from None
    finally:
        if stream is not None and callable(getattr(stream, 'close', None)):
            try:
                stream.close()
            except Exception:
                pass


def result(receipt, binding):
    if receipt.result == 'rejected':
        return {'status': 'image_rejected', 'text': 'Изображение повреждено, слишком велико или тип не совпадает. Пришлите новый PNG/JPEG до 5 МБ.', 'review_path': None}
    if receipt.review is None:
        return {'status': 'work_deleted', 'text': 'Лист ранее получен, но работа удалена. Повтор события не восстанавливает её.', 'review_path': None}
    if receipt.review.owner_id != binding.owner_id:
        raise denied()
    path = reverse('worksheet_reviews:detail', args=[receipt.review_id])
    if binding.in_chat_review:
        from .chat_review import describe
        return {'status':'worksheet_saved','text':describe(receipt.review,binding),'review_path':path}
    state = work_state(receipt.review)['label']
    text = f'Лист сохранён: {receipt.review.work_code}. {state}. Откройте рабочее место менеджера для сверки и обращения. U-ON: не синхронизировано.'
    if binding.review_origin:
        text += '\n' + binding.review_origin + path
    return {'status': 'worksheet_saved', 'text': text, 'review_path': path}


def reply_once(receipt, binding, text, keyboard=None):
    # Persist the attempt BEFORE side effects. A crash/timeout is not safe to retry.
    with transaction.atomic():
        active_owner(binding, lock=True)
        claimed = UpdateReceipt.objects.filter(pk=receipt.pk, owner_id=binding.owner_id, reply_state='pending').update(reply_state='attempted')
    if claimed:
        try:
            active_owner(binding)
        except GatewayError:
            UpdateReceipt.objects.filter(pk=receipt.pk).update(reply_state='suppressed')
            raise
        try:
            if keyboard is None:binding.transport.send_message(binding.chat_id,text,timeout_seconds=10)
            else:binding.transport.send_buttons(binding.chat_id,text,keyboard,timeout_seconds=10)
        except Exception:
            state = 'unknown'
        else:
            state = 'sent'
        UpdateReceipt.objects.filter(pk=receipt.pk).update(reply_state=state)
    receipt.refresh_from_db(fields=['reply_state'])
    return receipt.reply_state


def accept(update, binding):
    if connection.in_atomic_block:
        raise GatewayError(503, 'transaction_boundary', 'Telegram-вход требует отдельной транзакционной границы.')
    if not isinstance(binding, Binding) or not binding.valid():
        raise GatewayError(503, 'disabled', 'Telegram-вход не настроен.')
    if binding.in_chat_buttons:
        from .review_ui import access
        access(binding)
    if binding.in_chat_buttons and isinstance(update,dict) and 'callback_query' in update:
        from .review_ui import callback_accept
        return callback_accept(update,binding)
    if binding.in_chat_review and isinstance(update,dict) and isinstance(update.get('message'),dict) and 'text' in update['message']:
        from .chat_review import text_accept
        return text_accept(update,binding)
    update_id, key, fingerprint, file_id, mime = parse(update, binding)
    active_owner(binding)
    receipt = existing(binding, update_id, key, fingerprint)
    repeated = receipt is not None
    if receipt is None:
        image = download(binding, file_id, mime)
        try:
            with transaction.atomic():
                owner = active_owner(binding, lock=True)
                receipt = existing(binding, update_id, key, fingerprint)
                repeated = receipt is not None
                if receipt is None:
                    receipt = UpdateReceipt.objects.create(gateway=binding.gateway, update_id=update_id,
                        message_key=key, payload_hash=fingerprint, owner=owner, result='accepted' if image else 'rejected')
                    if image:
                        receipt.review = create_review(owner=owner, work_code='TG-'+uuid.uuid4().hex[:29].upper(),
                            worksheet_bytes=image[0], worksheet_type=image[1], hotel_choices=[])
                        receipt.save(update_fields=['review'])
        except IntegrityError:
            receipt = existing(binding, update_id, key, fingerprint)
            if receipt is None:
                raise GatewayError(503, 'retry', 'Сохранение не завершено; повторите доставку события.') from None
            repeated = True
    active_owner(binding)
    if binding.in_chat_buttons:
        from .review_ui import photo_reply
        return photo_reply(receipt,binding,repeated)
    response = result(receipt, binding)
    response['reply_state'] = reply_once(receipt, binding, response['text'])
    active_owner(binding)
    response['repeated'] = repeated
    response['crm'] = 'not_synchronized'
    return response
