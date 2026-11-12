"""Bốn provider cùng một interface. Không chứa agent loop."""
import json
import os
import re
import time
import urllib.error
import urllib.request

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'groq': ('GROQ_API_KEY', 'https://api.groq.com/openai/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


MIN_INTERVAL = float(os.getenv('MIN_REQUEST_INTERVAL', '0'))  # giây tối thiểu giữa 2 request
_last_request = [0.0]


def _retry_delay(exc, attempt, body):
    """Ưu tiên Retry-After header hoặc retryDelay của Google; không có thì backoff 10/20/40/60s."""
    header = exc.headers.get('Retry-After') if exc.headers else None
    if header and header.strip().isdigit():
        return min(int(header), 90)
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body)
    if match:
        return min(float(match.group(1)) + 1, 90)
    return min(10 * 2 ** attempt, 60)


def post_json(url, headers, payload, retries=6):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Connection': 'close', **headers})
    for attempt in range(retries):
        wait = MIN_INTERVAL - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < retries - 1:
                try:
                    body = exc.read().decode('utf-8', errors='ignore')
                except Exception:
                    body = ''
                delay = _retry_delay(exc, attempt, body)
                print(f'  [provider] HTTP {exc.code}, chờ {delay:.0f}s rồi thử lại ({attempt + 1}/{retries - 1})', flush=True)
                time.sleep(delay)
                continue
            try:
                detail = exc.read().decode('utf-8', errors='ignore')
            except Exception:
                detail = ''
            raise RuntimeError(f'Provider trả HTTP {exc.code}: {detail[:250]}') from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # TimeoutError ("The read operation timed out") không phải URLError nên phải bắt riêng.
            reason = getattr(exc, 'reason', exc)
            if attempt < retries - 1:
                print(f'  [provider] Lỗi mạng ({reason}), chờ 3s rồi thử lại ({attempt + 1}/{retries - 1})...', flush=True)
                time.sleep(3)
                continue
            raise RuntimeError(f'Không kết nối được provider: {reason}') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, self.url = CONFIG[name]
        # Cho phép đổi server (proxy/reseller), ví dụ ANTHROPIC_BASE_URL=https://api.shopaikey.com
        base = os.getenv(f'{name.upper()}_BASE_URL', '').rstrip('/')
        if base:
            self.url = base + ('/v1/messages' if name == 'anthropic' else '/chat/completions')
        self.key = os.getenv(key_name)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        data = post_json(self.url, {'Authorization': f'Bearer {self.key}'}, payload)
        choice = data['choices'][0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in message.get('tool_calls', [])]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        # 1024 quá thấp cho model có thinking: judge bị cắt (stop_reason=max_tokens) -> unscored.
        max_tokens = int(os.getenv('ANTHROPIC_MAX_TOKENS', '8192'))
        payload = {'model': self.model, 'max_tokens': max_tokens, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
