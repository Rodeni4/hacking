"""Real HTTP integration checks, using only stdlib and production dependencies."""
import json
from contextlib import closing
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent.parent


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name) / 'test.sqlite3')
        self.process = None
        self.addCleanup(self.stop)
        self.client = build_opener(ProxyHandler({}))
        self.start()

    def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        script = self.server_code(port)
        self.process = subprocess.Popen([sys.executable, '-c', script], cwd=ROOT,
                                        env={**os.environ, 'TEST_DB': self.database},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            if self.process.poll() is not None:
                self.fail(self.process.stderr.read().decode(errors='replace'))
            try:
                self.request('/api/agents')
                return
            except OSError:
                time.sleep(.1)
        self.fail('HTTP server did not start')

    def server_code(self, port):
        return ('import os, uvicorn; from app.main import create_app; '
                f'uvicorn.run(create_app(os.environ["TEST_DB"], secrets_path=os.environ["TEST_DB"] + ".secrets"), host="127.0.0.1", port={port}, log_level="error")')

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            self.process.communicate(timeout=10)
            self.process = None

    def request(self, path, method='GET', body=None, status=200):
        payload = None if body is None else json.dumps(body).encode()
        request = Request(self.url + path, data=payload, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            response = self.client.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            self.assertEqual(response.status, status)
            data = response.read()
            if 'application/json' in response.headers.get('Content-Type', ''):
                return json.loads(data)
            return data.decode()

    def test_full_lifecycle_and_process_restart(self):
        self.assertEqual(self.request('/api/agents'), [])
        agent = self.request('/api/agents', 'POST', {'name': ' Первый ', 'description': 'Описание', 'instruction': 'Инструкция'}, 201)
        aid = agent['id']
        self.assertEqual(agent['name'], 'Первый')
        edited = {'name': 'Редактор', 'description': '<script>alert(1)</script>', 'instruction': 'Сохрани\nпереносы'}
        self.request(f'/api/agents/{aid}', 'PUT', edited)
        other = self.request('/api/agents', 'POST', {'name': 'Другой'}, 201)
        c1 = self.request(f'/api/agents/{aid}/conversations', 'POST', status=201)['id']
        c2 = self.request(f'/api/agents/{aid}/conversations', 'POST', status=201)['id']
        text = '<img src=x onerror=alert(1)>\nПривет, мир!'
        message = self.request(f'/api/conversations/{c1}/messages', 'POST', {'content': text}, 201)
        self.assertEqual(message['role'], 'user')
        self.assertEqual(message['content'], text)
        self.request(f'/api/conversations/{c2}/messages', 'POST', {'content': 'Отдельный разговор'}, 201)
        self.assertEqual(self.request(f'/api/agents/{other["id"]}/conversations'), [])
        self.assertEqual(len(self.request(f'/api/conversations/{c1}/messages')), 1)
        self.stop()
        self.start()
        saved = self.request(f'/api/agents/{aid}')
        for key, value in edited.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(len(self.request(f'/api/agents/{aid}/conversations')), 2)
        self.assertEqual(self.request(f'/api/conversations/{c1}/messages'), [message])
        self.assertEqual(self.request(f'/api/conversations/{c2}/messages')[0]['content'], 'Отдельный разговор')
        self.request(f'/api/agents/{aid}', 'DELETE', status=204)
        self.request(f'/api/agents/{aid}', status=404)
        self.request(f'/api/conversations/{c1}/messages', status=404)
        with closing(sqlite3.connect(self.database)) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM messages').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM conversations').fetchone()[0], 0)
        self.assertEqual(len(self.request('/api/agents')), 1)

    def test_validation_and_static_interface(self):
        for name in ['', '   ', 'x' * 121]:
            self.request('/api/agents', 'POST', {'name': name}, 422)
        self.request('/api/agents/999', 'PUT', {'name': 'Нет'}, 404)
        self.request('/api/agents/999', 'DELETE', status=404)
        self.request('/api/agents/999/conversations', 'POST', status=404)
        self.request('/api/conversations/999/messages', 'POST', {'content': 'Нет'}, 404)
        agent = self.request('/api/agents', 'POST', {'name': 'Тест'}, 201)
        cid = self.request(f'/api/agents/{agent["id"]}/conversations', 'POST', status=201)['id']
        for text in ['', '  ', 'x' * 50001]:
            self.request(f'/api/conversations/{cid}/messages', 'POST', {'content': text}, 422)
        self.assertEqual(self.request(f'/api/conversations/{cid}/messages'), [])
        self.assertIn('Модель не подключена', self.request('/'))
        self.assertIn('textContent', self.request('/static/app.js'))
        self.assertIn(':root', self.request('/static/style.css'))


if __name__ == '__main__':
    unittest.main()
