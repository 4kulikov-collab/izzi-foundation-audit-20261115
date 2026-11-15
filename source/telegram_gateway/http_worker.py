"""Bounded Telegram HTTPS operation. Executed only by the explicitly enabled transport.

No proxy environment, arbitrary origins, redirects, debug output or retries.
The supervisor kills this process at its overall deadline, including blocked DNS/TLS.
"""
import http.client
import json
import math
import re
import ssl
import sys
import time

HOST = 'api.telegram.org'
MAX_FILE = 5 * 1024 * 1024
MAX_JSON = 64 * 1024
MAX_CHUNK = 64 * 1024
MAX_INPUT = 24 * 1024
ERROR_CODES = frozenset({'invalid_request', 'timeout', 'http', 'protocol', 'file_too_large', 'unavailable'})


class Failure(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'unavailable'
        super().__init__(self.code)


def integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def duration(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= 30


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Failure('protocol')
        value[key] = item
    return value


def validate(request):
    if not isinstance(request, dict) or request.get('operation') not in ('file', 'message', 'poll', 'poll_status', 'callback'):
        raise Failure('invalid_request')
    token = request.get('token')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9]{1,20}:[A-Za-z0-9_-]{20,200}', token):
        raise Failure('invalid_request')
    if not all(duration(request.get(key)) for key in ('connect_timeout', 'read_timeout', 'timeout_seconds')):
        raise Failure('invalid_request')
    if request['operation'] == 'file':
        if (not isinstance(request.get('file_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', request['file_id'])
                or not integer(request.get('max_bytes'), 1, MAX_FILE)
                or not integer(request.get('chunk_size'), 1, MAX_CHUNK)):
            raise Failure('invalid_request')
    elif request['operation'] == 'poll':
        if not integer(request.get('offset'),0,2**52-1) or not integer(request.get('poll_timeout'),1,10):
            raise Failure('invalid_request')
        if request['timeout_seconds'] < request['poll_timeout']+5 or request['read_timeout'] < request['poll_timeout']+2:
            raise Failure('invalid_request')
    elif request['operation'] == 'callback':
        if not isinstance(request.get('callback_id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',request['callback_id']):raise Failure('invalid_request')
        if not isinstance(request.get('text'),str) or not 1<=len(request['text'])<=200:raise Failure('invalid_request')
    elif request['operation'] == 'message':
        text = request.get('text')
        if not integer(request.get('chat_id'), 1, 2**52-1) or not isinstance(text, str):
            raise Failure('invalid_request')
        try:
            length = len(text.encode('utf-16-le')) // 2
        except UnicodeError:
            raise Failure('invalid_request') from None
        if not 1 <= length <= 4096:
            raise Failure('invalid_request')
        if 'reply_markup' in request:validate_keyboard(request['reply_markup'])


def validate_keyboard(markup):
    if type(markup) is not dict or set(markup)!={'inline_keyboard'}:raise Failure('invalid_request')
    rows=markup['inline_keyboard']
    if type(rows) is not list or not 1<=len(rows)<=10:raise Failure('invalid_request')
    for row in rows:
        if type(row) is not list or not 1<=len(row)<=2:raise Failure('invalid_request')
        for button in row:
            if type(button) is not dict or set(button)!={'text','callback_data'}:raise Failure('invalid_request')
            if not isinstance(button['text'],str) or not 1<=len(button['text'])<=60:raise Failure('invalid_request')
            data=button['callback_data']
            if not isinstance(data,str) or not re.fullmatch(r'iz:[0-9a-f]{32}:(?:v[1-7]|ok|edit|unknown|confirm|back|cancel|hint)',data):raise Failure('invalid_request')


class Deadline:
    def __init__(self, seconds, clock):
        self.clock, self.end = clock, clock() + seconds

    def remaining(self, cap):
        left = self.end - self.clock()
        if left <= 0:
            raise Failure('timeout')
        return min(left, cap)


def read_response(request, method, path, payload, *, limit, json_body, deadline, connection_factory):
    connection = response = None
    try:
        # Origin and TLS context cannot be supplied by an update or configuration.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs()
        connection = connection_factory(HOST, 443, timeout=deadline.remaining(request['connect_timeout']), context=context)
        connection.set_debuglevel(0)
        connection.connect()
        connection.auto_open = 0
        connection_socket = connection.sock
        connection_socket.settimeout(deadline.remaining(request['read_timeout']))
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        headers = {'Accept-Encoding': 'identity', 'Connection': 'close',
                   'Accept': 'application/json' if json_body else 'image/png,image/jpeg,application/octet-stream'}
        if body is not None:
            headers['Content-Type'] = 'application/json; charset=utf-8'
        connection.request(method, path, body=body, headers=headers)
        connection_socket.settimeout(deadline.remaining(request['read_timeout']))
        response = connection.getresponse()
        if response.status != 200:  # Includes all redirects, 429 and server failures. No retries.
            raise Failure('http')
        pairs = response.getheaders()
        if sum(len(k)+len(v) for k, v in pairs) > 16384:
            raise Failure('protocol')
        values = {}
        for key, value in pairs:
            key = key.lower()
            if key in values and key in ('content-length', 'transfer-encoding', 'content-encoding', 'content-type'):
                raise Failure('protocol')
            values[key] = value.strip()
        if values.get('content-encoding', 'identity').lower() != 'identity':
            raise Failure('protocol')
        if values.get('transfer-encoding', '').lower() not in ('', 'chunked'):
            raise Failure('protocol')
        if 'content-length' in values and 'transfer-encoding' in values:
            raise Failure('protocol')
        mime = values.get('content-type', '').split(';', 1)[0].strip().lower()
        if mime not in ({'application/json'} if json_body else {'image/jpeg', 'image/png', 'application/octet-stream'}):
            raise Failure('protocol')
        declared = None
        if 'content-length' in values:
            length = values['content-length']
            if not re.fullmatch(r'[0-9]{1,20}', length):
                raise Failure('protocol')
            declared = int(length)
            if declared > limit:
                raise Failure('protocol' if json_body else 'file_too_large')
        data = bytearray()
        while True:
            # read1 performs at most one underlying read, so slow trickles cannot
            # reset a full per-read budget indefinitely. Parent deadline is also enforced.
            connection_socket.settimeout(deadline.remaining(request['read_timeout']))
            block = response.read1(min(MAX_CHUNK, limit + 1 - len(data)))
            deadline.remaining(request['read_timeout'])
            if not block:
                break
            if type(block) is not bytes:
                raise Failure('protocol')
            data.extend(block)
            if len(data) > limit:
                raise Failure('protocol' if json_body else 'file_too_large')
            if response.isclosed():
                break
        if declared is not None and len(data) != declared:
            raise Failure('protocol')
        return bytes(data)
    finally:
        for resource in (response, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


def api(request, name, payload, deadline, connection_factory, result_type=dict):
    raw = read_response(request, 'POST', '/bot'+request['token']+'/'+name, payload,
                        limit=MAX_JSON, json_body=True, deadline=deadline, connection_factory=connection_factory)
    try:
        parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise Failure('protocol') from None
    if not isinstance(parsed, dict) or parsed.get('ok') is not True or type(parsed.get('result')) is not result_type:
        raise Failure('protocol')  # Never echo Telegram description, response or URL.
    return parsed['result']


def safe_path(value):
    if (not isinstance(value, str) or len(value) > 512 or not value
            or any(part in ('', '.', '..') or not re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in value.split('/'))):
        raise Failure('protocol')
    return value


def execute(request, *, connection_factory=http.client.HTTPSConnection, clock=time.monotonic):
    validate(request)
    deadline = Deadline(request['timeout_seconds'], clock)
    if request['operation'] == 'poll_status':
        bot=api(request,'getMe',{},deadline,connection_factory)
        webhook=api(request,'getWebhookInfo',{},deadline,connection_factory)
        if not integer(bot.get('id'),1,2**52-1) or bot.get('is_bot') is not True or type(webhook.get('url')) is not str:
            raise Failure('protocol')
        return b'P'+json.dumps({'bot_id':bot['id'],'webhook_set':bool(webhook['url'])}).encode()
    if request['operation'] == 'poll':
        updates=api(request,'getUpdates',{'offset':request['offset'],'timeout':request['poll_timeout'],
            'limit':1,'allowed_updates':['message','callback_query']},deadline,connection_factory,result_type=list)
        if len(updates)>1 or any(type(u) is not dict or not integer(u.get('update_id'),0,2**52-2) for u in updates):
            raise Failure('protocol')
        return b'U'+json.dumps(updates,ensure_ascii=False,separators=(',',':')).encode()
    if request['operation'] == 'callback':
        result=api(request,'answerCallbackQuery',{'callback_query_id':request['callback_id'],'text':request['text'],
            'show_alert':False,'cache_time':0},deadline,connection_factory,result_type=bool)
        if result is not True:raise Failure('protocol')
        return b'A'
    if request['operation'] == 'message':
        payload={'chat_id':request['chat_id'],'text':request['text'],'link_preview_options':{'is_disabled':True},'allow_paid_broadcast':False}
        if 'reply_markup' in request:payload['reply_markup']=request['reply_markup']
        result = api(request,'sendMessage',payload,deadline,connection_factory)
        chat = result.get('chat')
        if (not integer(result.get('message_id'), 1, 2**52-1) or not isinstance(chat, dict)
                or type(chat.get('id')) is not int or chat['id'] != request['chat_id']):
            raise Failure('protocol')
        return b'S'
    metadata = api(request, 'getFile', {'file_id': request['file_id']}, deadline, connection_factory)
    if metadata.get('file_id') != request['file_id']:
        raise Failure('protocol')
    path = safe_path(metadata.get('file_path'))
    size = metadata.get('file_size')
    if size is not None:
        if not integer(size, 1, 2**52-1):
            raise Failure('protocol')
        if size > request['max_bytes']:
            raise Failure('file_too_large')
    data = read_response(request, 'GET', '/file/bot'+request['token']+'/'+path, None,
                         limit=request['max_bytes'], json_body=False, deadline=deadline, connection_factory=connection_factory)
    if not data or (size is not None and len(data) != size):
        raise Failure('protocol')
    # Keep binary bytes unchanged; shared worksheet validation handles decoding/pixels.
    return b'F'+data


def main():
    output = b'E:unavailable'
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT+1)
        if len(raw) > MAX_INPUT:
            raise Failure('invalid_request')
        request = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        output = execute(request)
    except Failure as error:
        output = ('E:'+error.code).encode('ascii')
    except TimeoutError:
        output = b'E:timeout'
    except Exception:
        pass  # No exception text, request body, token, file path or HTTP response escapes.
    sys.stdout.buffer.write(output)


if __name__ == '__main__':
    main()
