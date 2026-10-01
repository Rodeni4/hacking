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
