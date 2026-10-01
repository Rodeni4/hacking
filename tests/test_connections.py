import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from urllib.error import HTTPError, URLError

from app.db import initialize, connect
from app.providers.yandex import fetch_models, ProviderError, URL
from tests.support import FakeOpener
from tests import test_app

MODELS = [
    {'id': 'gpt://folder/qwen/rc@Exact-Version', 'owned_by': 'Alibaba', 'object': 'model', 'created': 0},
    {'id': 'emb://folder/text-embeddings/latest@123', 'owned_by': 'Yandex', 'object': 'model', 'created': 12},
    {'id': 'vendor/model:version/Keep_CASE', 'owned_by': 'Another vendor', 'object': 'model', 'created': 23},
]


class ProviderTests(unittest.TestCase):
    def test_exact_full_list_and_request(self):
        fake = FakeOpener(json.dumps({'data': MODELS}).encode())
        self.assertEqual(fetch_models('dummy-secret', 'folder', fake), MODELS)
        self.assertEqual(fake.request.full_url, URL)
        self.assertEqual(fake.request.get_method(), 'GET')
        self.assertEqual(fake.request.get_header('Authorization'), 'Api-Key dummy-secret')
        self.assertEqual(fake.request.get_header('X-project'), 'folder')
        self.assertEqual(fake.timeout, 20)

    def test_errors_are_safe(self):
        values = [TimeoutError(), URLError('secret must not leak'), b'broken json', b'{}',
                  b'{"data": [null]}', b'{"data": [{"id":"a"}]}',
                  HTTPError(URL, 401, 'secret', {}, io.BytesIO(b'secret')),
                  HTTPError(URL, 403, 'secret', {}, io.BytesIO(b'secret'))]
        for value in values:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ProviderError) as raised:
                    fetch_models('secret', 'folder', FakeOpener(value))
                self.assertNotIn('secret', str(raised.exception))


class ConnectionTests(test_app.AppTests):
    def server_code(self, port):
        self.fixture = Path(self.database).parent / 'response.json'
        if not self.fixture.exists():
            self.fixture.write_text(json.dumps({'data': MODELS}), encoding='utf-8')
        return ('import os, uvicorn; from pathlib import Path; from app.main import create_app; '
                'from tests.support import fixture_provider; '
                'db=os.environ["TEST_DB"]; '
                'provider=lambda key, folder: fixture_provider(Path(db).parent / "response.json", key, folder); '
                f'uvicorn.run(create_app(db, db + ".secrets", {{"yandex": provider}}), host="127.0.0.1", port={port}, log_level="error")')

    def connection(self):
        body = {'name': 'Яндекс', 'folder_id': 'folder', 'api_key': 'dummy-PRIVATE-key'}
        preview = self.request('/api/connections/preview', 'POST', body)
        self.assertEqual(preview['models'], MODELS)
        saved = self.request('/api/connections', 'POST', body, 201)
        self.assertEqual(saved['models'], MODELS)
        self.assertNotIn(body['api_key'], json.dumps(saved))
        self.assertNotIn('api_key', saved)
        self.assertTrue(saved['key_saved'])
        return saved

    def test_connections_models_selection_and_restart(self):
        connection = self.connection()
        cid = connection['id']
        body = {'name': 'Выбранный агент', 'connection_id': cid, 'model_id': MODELS[0]['id']}
        agent = self.request('/api/agents', 'POST', body, 201)
        self.assertFalse(agent['model_missing'])
        self.stop()
        self.start()
        self.assertEqual(self.request(f'/api/agents/{agent["id"]}')['model_id'], MODELS[0]['id'])
        self.assertEqual(self.request(f'/api/agents/{agent["id"]}')['connection_id'], cid)
        self.assertEqual(self.request('/api/connections')[0]['models'], MODELS)
        blocked = self.request(f'/api/connections/{cid}', 'DELETE', status=409)
        self.assertIn(body['name'], blocked['detail'])
        updated = self.request(f'/api/connections/{cid}', 'PUT', {'name': 'Новое имя', 'folder_id': 'folder', 'api_key': ''})
        self.assertTrue(updated['key_saved'])
        secret_files = list(Path(self.database + '.secrets').glob('*.key'))
        self.assertEqual(len(secret_files), 1)
        self.assertEqual(secret_files[0].read_text(), 'dummy-PRIVATE-key')
        for failure in [401, 403, 'timeout', 'network', 'broken', {'data': [None]}]:
            self.fixture.write_text(json.dumps(failure), encoding='utf-8')
            error = self.request(f'/api/connections/{cid}/refresh', 'POST', status=502)
            self.assertNotIn('dummy-PRIVATE-key', json.dumps(error))
            cached = self.request('/api/connections')[0]
            self.assertEqual(cached['models'], MODELS)
            self.assertEqual(cached['models_updated_at'], connection['models_updated_at'])
        self.fixture.write_text('{"data": []}', encoding='utf-8')
        self.request(f'/api/connections/{cid}/refresh', 'POST')
        missing = self.request(f'/api/agents/{agent["id"]}')
        self.assertTrue(missing['model_missing'])
        self.assertEqual(missing['model_id'], MODELS[0]['id'])
        self.request(f'/api/agents/{agent["id"]}', 'PUT', body)
        self.request('/api/agents', 'POST', body, 422)
        self.request(f'/api/agents/{agent["id"]}', 'PUT', {'name': 'Без модели'})
        self.request(f'/api/connections/{cid}', 'DELETE', status=204)
        self.assertEqual(list(Path(self.database + '.secrets').glob('*.key')), [])

    def test_secret_validation_and_changed_folder(self):
        self.request('/api/connections', 'POST', {'name': 'Нет ключа', 'folder_id': 'folder'}, 422)
        self.request('/api/connections/preview', 'POST', {'folder_id': 'folder'}, 422)
        connection = self.connection()
        cid = connection['id']
        updated = self.request(f'/api/connections/{cid}', 'PUT', {'name': 'Другой каталог', 'folder_id': 'other', 'api_key': 'replacement-key'})
        self.assertEqual(updated['models'], MODELS)
        self.assertNotEqual(updated['models_folder_id'], updated['folder_id'])
        self.request('/api/agents', 'POST', {'name': 'Неверная модель', 'connection_id': cid, 'model_id': MODELS[0]['id']}, 422)
        self.request('/api/agents', 'POST', {'name': 'Без модели', 'connection_id': cid}, 201)
        self.assertNotIn('replacement-key', json.dumps(self.request('/api/connections')))
        self.assertNotIn('replacement-key', Path(self.database).read_bytes().decode('latin1'))
        self.assertNotIn('replacement-key', self.request('/'))
        self.request('/secrets/' + next(Path(self.database + '.secrets').glob('*.key')).name, status=404)
        self.request('/api/connections/preview', 'POST', {'folder_id': 'folder', 'api_key': 'key\nheader'}, 422)
        self.request('/api/connections/preview', 'POST', {'folder_id': 'folder', 'api_key': 'x', 'provider': 'openai'}, 422)


class MigrationTests(unittest.TestCase):
    def test_legacy_data_survives_idempotent_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.sqlite3'
            with closing(sqlite3.connect(path)) as con:
                con.executescript("""
                    CREATE TABLE agents(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', instruction TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT 'old');
                    CREATE TABLE conversations(id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE, title TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT 'old');
                    CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'user', content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT 'old');
                    INSERT INTO agents VALUES(7, 'Legacy', 'description', 'instruction', 'old');
                    INSERT INTO conversations VALUES(12, 7, 'Conversation', 'old');
                    INSERT INTO messages VALUES(23, 12, 'user', 'Preserve exact message', 'old');
                """)
            initialize(path)
            initialize(path)
            with connect(path) as con:
                agent = dict(con.execute('SELECT * FROM agents').fetchone())
                self.assertEqual(agent, {'id': 7, 'name': 'Legacy', 'description': 'description', 'instruction': 'instruction', 'created_at': 'old', 'connection_id': None, 'model_id': None})
                self.assertEqual(tuple(con.execute('SELECT * FROM conversations').fetchone()), (12, 7, 'Conversation', 'old'))
                self.assertEqual(tuple(con.execute('SELECT * FROM messages').fetchone()), (23, 12, 'user', 'Preserve exact message', 'old'))
                self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
