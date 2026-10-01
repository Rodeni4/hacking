"""Fake HTTP transport: tests can never contact the real provider."""
import io
import json
from urllib.error import HTTPError, URLError

from app.providers.yandex import fetch_models


class FakeOpener:
    def __init__(self, value):
        self.value = value
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        if isinstance(self.value, BaseException):
            raise self.value
        return io.BytesIO(self.value)


def fixture_provider(path, key, folder):
    value = json.loads(path.read_text(encoding='utf-8'))
    if value == 'timeout':
        reply = TimeoutError()
    elif value == 'network':
        reply = URLError('offline')
    elif isinstance(value, int):
        reply = HTTPError('https://example.invalid', value, 'fake error', {}, io.BytesIO(b'sensitive upstream body'))
    elif value == 'broken':
        reply = b'not json'
    else:
        reply = json.dumps(value).encode()
    return fetch_models(key, folder, opener=FakeOpener(reply))


async def fixture_chat_provider(directory, key, folder, model, messages, tools=None):
    import asyncio
    import httpx
    from app.providers.yandex_chat import complete

    async def handle(request):
        settings = json.loads((directory / 'chat-response.json').read_text(encoding='utf-8'))
        (directory / 'chat-request.json').write_text(request.content.decode(), encoding='utf-8')
        await asyncio.sleep(settings.get('delay', 0))
        if settings.get('timeout'):
            raise httpx.ReadTimeout('dummy secret must not leak')
        default = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Ответ с инструментами'}]}]} if tools else {
            'choices': [{'message': {'role': 'assistant', 'content': 'Тестовый ответ <script>не выполнять</script>'}, 'finish_reason': 'stop'}]
        }
        return httpx.Response(settings.get('status', 200), json=settings.get('body', default))
    return await complete(key, folder, model, messages, transport=httpx.MockTransport(handle), tools=tools)
