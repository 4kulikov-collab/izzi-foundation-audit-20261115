"""Opt-in real HTTP transport, with a killable per-operation subprocess and no retries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .http_worker import ERROR_CODES, MAX_FILE, MAX_INPUT, MAX_JSON, Failure, validate

WORKER = Path(__file__).with_name('http_worker.py')


class TelegramTransportError(Exception):
    def __init__(self, code='unavailable'):
        self.code = code if code in ERROR_CODES | {'disabled', 'configuration', 'worker'} else 'unavailable'
        super().__init__('Telegram transport: '+self.code)


class FileRejected(TelegramTransportError):
    pass


def run_isolated(request, timeout_seconds):
    """Credentials go through stdin, never argv/env/files. No detached request after timeout."""
    process = None
    code, output = None, b''
    started = time.monotonic()
    try:
        body = json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(body) > MAX_INPUT:
            code = 'invalid_request'
        else:
            process = subprocess.Popen([sys.executable, '-I', str(WORKER)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
                env={key: os.environ[key] for key in ('SYSTEMROOT', 'SystemRoot', 'WINDIR', 'PATH', 'TEMP', 'TMP') if key in os.environ})
            remaining = timeout_seconds - (time.monotonic()-started)
            if remaining <= 0:
                code = 'timeout'
            else:
                output, _ = process.communicate(body, timeout=remaining)
                if time.monotonic()-started > timeout_seconds:
                    code = 'timeout'
                elif process.returncode != 0 or len(output) > MAX_FILE+1:
                    code = 'worker'
    except subprocess.TimeoutExpired:
        code = 'timeout'
    except Exception:
        code = 'worker'
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                    # Drain bounded output and join communicate reader threads (Windows).
                    process.communicate(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    code = code or 'worker'
            # Close all pipes; do not retain raw stderr/errors.
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        code = code or 'worker'
    if code:
        raise TelegramTransportError(code)  # Outside except: no secret-bearing context chain.
    return output


class TelegramHTTPTransport:
    """Construction/import cannot connect. Enabling/secret provisioning is a separate action.

    runner is a trusted internal seam for offline HTTP fixtures, never an update field.
    """
    def __init__(self, token_provider, *, enabled=False, connect_timeout=3, read_timeout=5, runner=run_isolated):
        self._token_provider = token_provider
        self._enabled = enabled is True
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._runner = runner

    def __repr__(self):
        return f'<TelegramHTTPTransport enabled={self._enabled}>'

    def _perform(self, operation, timeout_seconds, **fields):
        if not self._enabled:
            raise TelegramTransportError('disabled')
        code, output = None, b''
        try:
            request = {'operation': operation, 'token': self._token_provider(), 'timeout_seconds': timeout_seconds,
                       'connect_timeout': self._connect_timeout, 'read_timeout': self._read_timeout, **fields}
            validate(request)
            output = self._runner(request, timeout_seconds)
            if type(output) is not bytes or len(output) > MAX_FILE+1:
                code = 'worker'
        except Failure as error:
            code = error.code
        except TelegramTransportError as error:
            code = error.code
        except Exception:
            code = 'unavailable'
        if not code and output.startswith(b'E:'):
            candidate = output[2:].decode('ascii', errors='replace')
            code = candidate if candidate in ERROR_CODES else 'worker'
        if code:
            error = FileRejected if code == 'file_too_large' else TelegramTransportError
            raise error(code)
        return output

    def iter_file(self, file_id, *, max_bytes, chunk_size, timeout_seconds):
        output = self._perform('file', timeout_seconds, file_id=file_id, max_bytes=max_bytes, chunk_size=chunk_size)
        if not output.startswith(b'F') or not 1 <= len(output)-1 <= max_bytes:
            raise TelegramTransportError('protocol')
        # HTTP was bounded/read incrementally in the child. IPC transfers at most 5 MiB;
        # the caller receives unchanged binary chunks, without text/base64 conversion.
        for offset in range(1, len(output), chunk_size):
            yield output[offset:offset+chunk_size]

    def send_message(self, chat_id, text, *, timeout_seconds):
        output = self._perform('message', timeout_seconds, chat_id=chat_id, text=text)
        if output != b'S':
            raise TelegramTransportError('protocol')


    def send_buttons(self,chat_id,text,keyboard,*,timeout_seconds):
        output=self._perform('message',timeout_seconds,chat_id=chat_id,text=text,reply_markup=keyboard)
        if output!=b'S':raise TelegramTransportError('protocol')

    def answer_callback(self,callback_id,text,*,timeout_seconds):
        output=self._perform('callback',timeout_seconds,callback_id=callback_id,text=text)
        if output!=b'A':raise TelegramTransportError('protocol')

class TelegramPollingHTTPTransport(TelegramHTTPTransport):
    """Same fixed-origin/killable worker; construction is disabled by default."""
    def __init__(self, token_provider, *, enabled=False, runner=run_isolated):
        super().__init__(token_provider,enabled=enabled,connect_timeout=3,read_timeout=15,runner=runner)

    def poll_status(self):
        raw=self._perform('poll_status',20)
        try:
            if not raw.startswith(b'P') or len(raw)>MAX_JSON+1:raise ValueError()
            result=json.loads(raw[1:])
            if (type(result) is not dict or set(result)!={'bot_id','webhook_set'} or
                type(result['bot_id']) is not int or not 1<=result['bot_id']<=2**52-1 or type(result['webhook_set']) is not bool):raise ValueError()
        except Exception:
            result=None
        if result is None:raise TelegramTransportError('protocol')
        return result

    def get_updates(self, offset):
        raw=self._perform('poll',20,offset=offset,poll_timeout=10)
        try:
            if not raw.startswith(b'U') or len(raw)>MAX_JSON+1:raise ValueError()
            result=json.loads(raw[1:])
            if (type(result) is not list or len(result)>1 or any(type(u) is not dict or type(u.get('update_id')) is not int or not 0<=u['update_id']<=2**52-2 for u in result)):raise ValueError()
        except Exception:
            result=None
        if result is None:raise TelegramTransportError('protocol')
        return result
