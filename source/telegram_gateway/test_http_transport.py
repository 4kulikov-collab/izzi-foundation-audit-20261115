"""Real HTTP parser over a fake socket; zero external connections or real credentials."""
import contextlib
from dataclasses import replace
import http.client
import io
import json
import logging
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
import traceback
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .adapter import Binding
from .http_transport import FileRejected, TelegramHTTPTransport, TelegramTransportError, run_isolated
from .http_worker import Failure, MAX_FILE, MAX_JSON, execute
from .models import UpdateReceipt
from worksheet_reviews.models import WorksheetReview

TOKEN = '123456789:SYNTHETIC_ONLY_NOT_A_REAL_BOT_TOKEN'


def raw_response(body, *, status=200, mime='application/json', headers=(), length=True):
    head = f'HTTP/1.1 {status} Synthetic\r\nContent-Type: {mime}\r\nConnection: close\r\n'
    if length:
        head += f'Content-Length: {len(body)}\r\n'
    head += ''.join(f'{key}: {value}\r\n' for key, value in headers)
    return (head+'\r\n').encode('ascii')+body


def api_response(result=None, **kwargs):
    return raw_response(json.dumps({'ok': True, 'result': result or {}}, ensure_ascii=False).encode(), **kwargs)


class Clock:
    now = 0.0
    def __call__(self): return self.now


class FakeSocket:
    def __init__(self, wire, fixture):
        self.stream = io.BytesIO(wire)
        self.fixture, self.closed, self.sent, self.timeouts = fixture, False, bytearray(), []

    def settimeout(self, value):
        if self.closed and self.stream.closed:
            raise OSError('descriptor closed')
        self.timeouts.append(value)

    def sendall(self, value):
        self.sent.extend(value)
        if self.fixture.send_failure and b'/sendMessage HTTP/' in value:
            raise self.fixture.send_failure
    def makefile(self, *args): return self.stream
    def close(self): self.closed = True


class HTTPFixture:
    """HTTPSConnection replacement retaining stdlib HTTP request/response parsing."""
    def __init__(self, wires):
        self.wires, self.connections, self.sockets, self.clock = list(wires), [], [], Clock()
        self.connect_failure = None
        self.read_failure = None
        self.send_failure = None
        self.advance_per_read = 0

    def factory(self, host, port, *, timeout, context):
        assert host == 'api.telegram.org' and port == 443
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        fixture = self
        class Connection(http.client.HTTPConnection):
            default_port = 443  # Match HTTPSConnection while replacing the socket only.
            def connect(self):
                if fixture.connect_failure:
                    raise fixture.connect_failure
                if not fixture.wires:
                    raise AssertionError('Unexpected retry/request')
                self.sock = FakeSocket(fixture.wires.pop(0), fixture)
                fixture.sockets.append(self.sock)
                original = self.sock.stream.read1
                def read1(size):
                    fixture.clock.now += fixture.advance_per_read
                    if fixture.read_failure:
                        raise fixture.read_failure
                    return original(min(size, 3) if fixture.advance_per_read else size)
                self.sock.stream.read1 = read1
        connection = Connection(host, port, timeout=timeout)
        self.connections.append(connection)
        return connection

    def run(self, request, timeout):
        try:
            return execute(request, connection_factory=self.factory, clock=self.clock)
        except Failure as error:
            return ('E:'+error.code).encode()
        except TimeoutError:
            return b'E:timeout'
        except Exception:
            return b'E:unavailable'

    def requests(self):
        return [bytes(sock.sent) for sock in self.sockets]


class HTTPTransportTests(SimpleTestCase):
    def setUp(self):
        self.network = patch('socket.socket.connect', side_effect=AssertionError('External network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.image = b'\x89PNG\r\n\x1a\n\x00\xff\x80\r\nSYNTHETIC_BINARY'

    def transport(self, fixture, **kwargs):
        return TelegramHTTPTransport(lambda: TOKEN, enabled=True, runner=fixture.run, **kwargs)

    def metadata(self, **values):
        return {'file_id': 'synthetic_id', 'file_path': 'photos/file_1.png', 'file_size': len(self.image), **values}

    def read(self, transport, **kwargs):
        return b''.join(transport.iter_file('synthetic_id', max_bytes=MAX_FILE, chunk_size=65536, timeout_seconds=15, **kwargs))

    def test_getfile_then_binary_download_exact_bytes_fixed_origin_tls_and_closed(self):
        fixture = HTTPFixture([api_response(self.metadata()), raw_response(self.image, mime='image/png')])
        self.assertEqual(self.read(self.transport(fixture)), self.image)
        first, second = fixture.requests()
        self.assertTrue(first.startswith(('POST /bot'+TOKEN+'/getFile HTTP/1.1\r\n').encode()))
        self.assertTrue(second.startswith(('GET /file/bot'+TOKEN+'/photos/file_1.png HTTP/1.1\r\n').encode()))
        self.assertIn(b'Host: api.telegram.org\r\n', first)
        self.assertIn(b'Accept-Encoding: identity', second)
        self.assertTrue(all(sock.closed and sock.stream.closed for sock in fixture.sockets))
        self.assertTrue(all(0 < timeout <= 5 for sock in fixture.sockets for timeout in sock.timeouts))

    def test_sendmessage_plain_text_no_preview_no_paid_broadcast_no_retry(self):
        fixture = HTTPFixture([api_response({'message_id': 7, 'chat': {'id': 100001}})])
        self.transport(fixture).send_message(100001, 'Лист ожидает сверки. U-ON: не синхронизировано.', timeout_seconds=10)
        payload = json.loads(fixture.requests()[0].split(b'\r\n\r\n', 1)[1])
        self.assertEqual(payload['link_preview_options'], {'is_disabled': True})
        self.assertIs(payload['allow_paid_broadcast'], False)
        self.assertNotIn('parse_mode', payload)
        self.assertEqual(len(fixture.connections), 1)

    def test_disabled_never_reads_token_or_starts_worker(self):
        with patch('telegram_gateway.http_transport.run_isolated') as runner:
            transport = TelegramHTTPTransport(lambda: self.fail('Token accessed while disabled'), runner=runner)
            with self.assertRaisesRegex(TelegramTransportError, 'disabled'): self.read(transport)
            with self.assertRaisesRegex(TelegramTransportError, 'disabled'): transport.send_message(1, 'Пример', timeout_seconds=10)
            runner.assert_not_called()

    def test_invalid_arguments_never_make_http_requests(self):
        fixture = HTTPFixture([]); transport = self.transport(fixture)
        for options in ({'file_id': 'https://example.invalid/x'}, {'max_bytes': MAX_FILE+1}, {'chunk_size': 0}, {'timeout_seconds': float('inf')}, {'timeout_seconds': -1}):
            args = dict(file_id='synthetic_id', max_bytes=MAX_FILE, chunk_size=1024, timeout_seconds=15); args.update(options)
            with self.subTest(options=list(options)), self.assertRaises(TelegramTransportError): list(transport.iter_file(**args))
        for chat, text in [(True, 'Пример'), (-1, 'Пример'), (1, ''), (1, 'x'*4097), (1, '\ud800')]:
            with self.assertRaises(TelegramTransportError): transport.send_message(chat, text, timeout_seconds=10)
        self.assertEqual(fixture.connections, [])

    def test_safe_path_rejects_other_origins_traversal_encoding_query_and_controls(self):
        for path in ('https://example.invalid/x', '//example.invalid/x', '/photos/x', '../x', 'photos/../x', 'photos/%2e%2e/x', 'photos/x?token=bad', 'photos/x#f', 'photos\\x', 'photos//x', 'photos/x\r\nX: y', 'photos/./x'):
            fixture = HTTPFixture([api_response(self.metadata(file_path=path))])
            with self.subTest(path_kind=path[:8]), self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))
            self.assertEqual(len(fixture.connections), 1)

    def test_all_redirects_http_errors_and_rate_limits_stop_without_retry(self):
        for status in (301, 302, 303, 307, 308, 401, 429, 500):
            fixture = HTTPFixture([raw_response(b'', status=status, headers=[('Location', 'https://example.invalid/never'), ('Retry-After', '1')])])
            with self.subTest(status=status), self.assertRaisesRegex(TelegramTransportError, 'http'): self.read(self.transport(fixture))
            self.assertEqual(len(fixture.connections), 1)
            self.assertTrue(fixture.sockets[0].stream.closed)

    def test_binary_redirect_does_not_follow_location(self):
        fixture = HTTPFixture([api_response(self.metadata()), raw_response(b'', status=302, headers=[('Location','https://example.invalid/x')])])
        with self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))
        self.assertEqual(len(fixture.connections), 2)

    def test_api_ok_type_result_and_json_are_strict(self):
        for body in (b'{"ok":false,"description":"SYNTHETIC_SECRET"}', b'{"ok":1,"result":{}}', b'{"ok":true,"result":[]}', b'{"ok":true,"ok":false}', b'{', b'\xff'):
            fixture = HTTPFixture([raw_response(body)])
            with self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))
            self.assertEqual(len(fixture.connections), 1)

    def test_api_body_limit_declared_and_streamed(self):
        for length in (True, False):
            fixture = HTTPFixture([raw_response(b'x'*(MAX_JSON+1), length=length)])
            with self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))
            self.assertTrue(fixture.sockets[0].closed)

    def test_file_size_metadata_and_actual_limit(self):
        fixture = HTTPFixture([api_response(self.metadata(file_size=MAX_FILE+1))])
        with self.assertRaises(FileRejected): self.read(self.transport(fixture))
        self.assertEqual(len(fixture.connections), 1)
        for length in (True, False):
            metadata = self.metadata(); metadata.pop('file_size')
            fixture = HTTPFixture([api_response(metadata), raw_response(b'x'*(MAX_FILE+1), mime='application/octet-stream', length=length)])
            with self.assertRaises(FileRejected): self.read(self.transport(fixture))
            self.assertTrue(all(sock.closed for sock in fixture.sockets))

    def test_mismatched_file_id_or_size_truncated_body_and_empty_body(self):
        fixture = HTTPFixture([api_response(self.metadata(file_id='different'))])
        with self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))
        for body in (b'', b'partial'):
            fixture = HTTPFixture([api_response(self.metadata()), raw_response(body, mime='image/png')])
            with self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))
        fixture = HTTPFixture([api_response(self.metadata()), raw_response(self.image, mime='image/png')[:-4]])
        with self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))

    def test_gzip_ambiguous_headers_and_json_download_rejected(self):
        for headers in ([('Content-Encoding','gzip')], [('Content-Length','1')], [('Transfer-Encoding','chunked')]):
            fixture = HTTPFixture([api_response(self.metadata(), headers=headers)])
            with self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))
        fixture = HTTPFixture([api_response(self.metadata()), raw_response(b'{"description":"SYNTHETIC_SECRET"}')])
        with self.assertRaises(TelegramTransportError): self.read(self.transport(fixture))

    def test_unknown_length_and_chunked_binary_are_bounded_and_preserved(self):
        metadata = self.metadata(); metadata.pop('file_size')
        for chunked in (False, True):
            body = self.image
            headers = []
            if chunked:
                body = f'{len(body):x}\r\n'.encode()+body+b'\r\n0\r\n\r\n'; headers=[('Transfer-Encoding','chunked')]
            fixture = HTTPFixture([api_response(metadata), raw_response(body, mime='application/octet-stream', length=False, headers=headers)])
            self.assertEqual(self.read(self.transport(fixture)), self.image)

    def test_connect_read_and_overall_deadline_timeout(self):
        for phase in ('connect', 'read', 'trickle'):
            fixture = HTTPFixture([api_response(self.metadata())])
            if phase == 'connect': fixture.connect_failure = TimeoutError(TOKEN)
            if phase == 'read': fixture.read_failure = TimeoutError(TOKEN)
            if phase == 'trickle': fixture.advance_per_read = 4
            with self.subTest(phase=phase), self.assertRaisesRegex(TelegramTransportError, 'timeout'): self.read(self.transport(fixture))
            self.assertTrue(all(sock.closed for sock in fixture.sockets))

    def test_wrong_send_ack_is_uncertain_and_not_retried(self):
        for result in ({'message_id': 1, 'chat': {'id': 2}}, {'message_id': True, 'chat': {'id': 100001}}, {}):
            fixture = HTTPFixture([api_response(result)])
            with self.assertRaises(TelegramTransportError): self.transport(fixture).send_message(100001, 'Пример', timeout_seconds=10)
            self.assertEqual(len(fixture.connections), 1)

    def test_token_url_exception_response_and_provider_never_leak_to_error_or_logs(self):
        secret_url = 'https://api.telegram.org/bot'+TOKEN+'/getFile'
        def leaking_runner(*args): raise RuntimeError(secret_url)
        def leaking_provider(): raise RuntimeError(secret_url)
        transports = [TelegramHTTPTransport(lambda: TOKEN, enabled=True, runner=leaking_runner), TelegramHTTPTransport(leaking_provider, enabled=True)]
        stdout, stderr, logs = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs); logging.getLogger().addHandler(handler)
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                for transport in transports:
                    try: self.read(transport)
                    except TelegramTransportError as error:
                        self.assertIsNone(error.__context__)
                        self.assertNotIn(TOKEN, ''.join(traceback.format_exception(error)))
                        self.assertNotIn(secret_url, repr(error))
                    else: self.fail('Expected safe transport failure')
                    self.assertNotIn(TOKEN, repr(transport))
        finally: logging.getLogger().removeHandler(handler)
        self.assertEqual(stdout.getvalue()+stderr.getvalue()+logs.getvalue(), '')

    def test_worker_stdio_never_emits_exception_token_url_or_unbounded_input(self):
        from . import http_worker
        from types import SimpleNamespace
        for raw in (b'{}', b'x'*(24*1024+1)):
            output = io.BytesIO()
            with patch.object(http_worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw))), patch.object(http_worker.sys, 'stdout', SimpleNamespace(buffer=output)):
                http_worker.main()
            self.assertEqual(output.getvalue(), b'E:invalid_request')
        output = io.BytesIO()
        with patch.object(http_worker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{}'))), patch.object(http_worker.sys, 'stdout', SimpleNamespace(buffer=output)), patch.object(http_worker, 'execute', side_effect=RuntimeError('https://api.telegram.org/bot'+TOKEN+'/getFile')):
            http_worker.main()
        self.assertEqual(output.getvalue(), b'E:unavailable')

    def test_real_subprocess_rejects_invalid_request_without_network(self):
        self.assertEqual(run_isolated({}, 3), b'E:invalid_request')

    def test_supervisor_kills_blocked_synthetic_worker_and_excludes_secret_from_argv_env(self):
        with tempfile.TemporaryDirectory(prefix='izzi-http-deadline-') as directory:
            worker = Path(directory)/'synthetic_worker.py'
            worker.write_text('import sys,time\nsys.stdin.buffer.read()\ntime.sleep(30)\n')
            original = subprocess.Popen; processes = []
            def capture(*args, **kwargs):
                self.assertNotIn(TOKEN, repr(args))
                self.assertNotIn('SSLKEYLOGFILE', kwargs['env'])
                self.assertNotIn('HTTPS_PROXY', kwargs['env'])
                process = original(*args, **kwargs); processes.append(process); return process
            started = time.monotonic()
            with patch('telegram_gateway.http_transport.WORKER', worker), patch('telegram_gateway.http_transport.subprocess.Popen', capture):
                with self.assertRaisesRegex(TelegramTransportError, 'timeout'): run_isolated({'token': TOKEN}, .2)
            self.assertLess(time.monotonic()-started, 2)
            self.assertIsNotNone(processes[0].poll())
            self.assertTrue(processes[0].stdout.closed and processes[0].stdin.closed)


class HTTPGatewayIntegrationTests(TransactionTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('synthetic-http-transport-owner')
        self.png = (Path(settings.BASE_DIR)/'docs/fixtures/synthetic-travel-card.png').read_bytes()
        self.update = {'update_id': 1, 'message': {'message_id': 1, 'from': {'id':100001,'is_bot':False}, 'chat':{'id':100001,'type':'private'},
                        'document':{'file_id':'synthetic_id','file_size':len(self.png),'mime_type':'image/png'}}}
        self.network = patch('socket.socket.connect', side_effect=AssertionError('External network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)

    def post(self, fixture):
        transport = TelegramHTTPTransport(lambda: TOKEN, enabled=True, runner=fixture.run)
        binding = Binding('synthetic-http', self.owner.pk, 100001, 100001, 'SYNTHETIC_WEBHOOK_SECRET_FOR_TESTS_ONLY', transport)
        with override_settings(TELEGRAM_GATEWAY=binding):
            return self.client.post(reverse('telegram-webhook'), json.dumps(self.update), content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=binding.webhook_secret)

    def fixture(self, reply):
        return HTTPFixture([api_response({'file_id':'synthetic_id','file_path':'photos/file.png','file_size':len(self.png)}),
                            raw_response(self.png,mime='image/png'), reply])

    def test_full_http_protocol_bridge_persists_once_and_duplicate_has_zero_http_calls(self):
        fixture = self.fixture(api_response({'message_id':9,'chat':{'id':100001}}))
        self.assertEqual(self.post(fixture).json()['reply_state'], 'sent')
        self.assertEqual(bytes(WorksheetReview.objects.get().worksheet_bytes),self.png)
        second = HTTPFixture([])
        self.assertTrue(self.post(second).json()['repeated'])
        self.assertEqual(len(fixture.connections),3)
        self.assertEqual(second.connections,[])
        self.assertEqual(UpdateReceipt.objects.count(),1)

    def test_failed_send_keeps_work_and_never_resends_or_leaks_api_error(self):
        fixture = self.fixture(raw_response(('SYNTHETIC '+TOKEN).encode(),status=500))
        response=self.post(fixture)
        self.assertEqual(response.json()['reply_state'],'unknown')
        self.assertNotIn(TOKEN.encode(),response.content)
        self.assertEqual(self.post(HTTPFixture([])).json()['reply_state'],'unknown')
        self.assertEqual(WorksheetReview.objects.count(),1)
        self.assertEqual(len(fixture.connections),3)

    def test_send_timeout_is_unknown_and_duplicate_never_sends_again(self):
        fixture = self.fixture(api_response({'message_id':9,'chat':{'id':100001}}))
        fixture.send_failure = TimeoutError('https://api.telegram.org/bot'+TOKEN+'/sendMessage')
        response = self.post(fixture)
        self.assertEqual(response.json()['reply_state'], 'unknown')
        self.assertNotIn(TOKEN.encode(), response.content)
        self.assertEqual(self.post(HTTPFixture([])).json()['reply_state'], 'unknown')
        self.assertEqual(len(fixture.connections), 3)
        self.assertEqual(WorksheetReview.objects.count(), 1)

    def test_getfile_http_failure_is_retryable_without_partial_work(self):
        failing = HTTPFixture([raw_response(b'SYNTHETIC_API_FAILURE', status=500)])
        self.assertEqual(self.post(failing).status_code, 503)
        self.assertFalse(WorksheetReview.objects.exists())
        self.assertFalse(UpdateReceipt.objects.exists())
        working = self.fixture(api_response({'message_id':9,'chat':{'id':100001}}))
        self.assertEqual(self.post(working).json()['reply_state'], 'sent')
        self.assertEqual(WorksheetReview.objects.count(), 1)

    def test_getfile_size_rejection_is_terminal_and_does_not_download_binary(self):
        fixture=HTTPFixture([api_response({'file_id':'synthetic_id','file_path':'photos/file.png','file_size':MAX_FILE+1}),api_response({'message_id':9,'chat':{'id':100001}})])
        self.assertEqual(self.post(fixture).json()['status'],'image_rejected')
        self.assertFalse(WorksheetReview.objects.exists())
        self.assertEqual(self.post(HTTPFixture([])).json()['status'],'image_rejected')
        self.assertEqual(len(fixture.connections),2)
