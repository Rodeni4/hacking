"""Connection management and durable model cache; secrets never enter SQLite."""
import json
import hashlib
import time
from datetime import datetime, timezone
from threading import RLock
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr, field_validator

from .db import connect
from .providers import PROVIDERS
from .providers.yandex import ProviderError


class Credentials(BaseModel):
    provider: Literal['yandex'] = 'yandex'
    folder_id: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(default=SecretStr(''), max_length=4096)

    @field_validator('folder_id')
    @classmethod
    def folder(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError('Некорректный Folder ID')
        return value

    @field_validator('api_key')
    @classmethod
    def key(cls, value):
        key = value.get_secret_value().strip()
        if any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError('Некорректный ключ')
        return SecretStr(key)


class ConnectionInput(Credentials):
    name: str = Field(min_length=1, max_length=120)

    @field_validator('name')
    @classmethod
    def name_not_empty(cls, value):
        if not value.strip():
            raise ValueError('Введите название')
        return value.strip()


def get_connection(con, connection_id):
    row = con.execute('SELECT * FROM connections WHERE id=?', (connection_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Подключение не найдено.')
    return dict(row)


def public_connection(row, secrets):
    return {**{key: row[key] for key in ('id', 'name', 'provider', 'folder_id', 'models_updated_at', 'models_folder_id')},
            'key_saved': secrets.exists(row['secret_ref']), 'models': json.loads(row['models_json'])}


def validate_selection(con, body, previous=None):
    if body.connection_id is None:
        if body.model_id is not None:
            raise HTTPException(422, 'Сначала выберите подключение.')
        return
    row = get_connection(con, body.connection_id)
    if body.model_id is None:
        return
    # An existing selection remains valid even if it disappears from discovery.
    if previous and (previous['connection_id'], previous['model_id']) == (body.connection_id, body.model_id):
        return
    if row['models_folder_id'] != row['folder_id'] or not any(item['id'] == body.model_id for item in json.loads(row['models_json'])):
        raise HTTPException(422, 'Выберите модель из актуального списка этого подключения.')


def agent_view(con, row):
    agent = dict(row)
    agent.update(source=None, connection_name=None, model_missing=False)
    if agent['connection_id'] is not None:
        connection = get_connection(con, agent['connection_id'])
        agent['source'] = 'Яндекс AI Studio'
        agent['connection_name'] = connection['name']
        agent['model_missing'] = bool(agent['model_id']) and (
            connection['models_folder_id'] != connection['folder_id'] or
            not any(item['id'] == agent['model_id'] for item in json.loads(connection['models_json'])))
    return agent


def router(database_path, secrets, providers=None):
    routes = APIRouter(prefix='/api/connections')
    adapters = providers if providers is not None else PROVIDERS
    lock = RLock()
    previews = {}

    def cache_key(key, folder):
        return hashlib.sha256((key + '\0' + folder).encode()).hexdigest()

    def save_preview(con, connection_id, key, folder):
        cached = previews.get(cache_key(key, folder))
        if cached and time.monotonic() - cached[0] < 1800:
            result = cached[1]
            con.execute('UPDATE connections SET models_json=?, models_updated_at=?, models_folder_id=? WHERE id=?',
                        (json.dumps(result['models'], ensure_ascii=False), result['models_updated_at'], folder, connection_id))

    def obtain(body, existing=None):
        key = body.api_key.get_secret_value()
        if not key and existing:
            key = secrets.read(existing['secret_ref'])
        if not key:
            raise HTTPException(422, 'Введите API-ключ.')
        try:
            models = adapters[body.provider](key, body.folder_id)
        except ProviderError as error:
            raise HTTPException(502, str(error)) from None
        result = {'models': models, 'models_updated_at': datetime.now(timezone.utc).isoformat(), 'models_folder_id': body.folder_id}
        if len(previews) >= 100:
            previews.pop(next(iter(previews)))
        previews[cache_key(key, body.folder_id)] = (time.monotonic(), result)
        return result

    @routes.get('')
    def list_connections():
        with connect(database_path) as con:
            return [public_connection(dict(row), secrets) for row in con.execute('SELECT * FROM connections ORDER BY id DESC')]

    @routes.post('/preview')
    def preview(body: Credentials):
        with lock:
            return obtain(body)

    @routes.post('/{connection_id}/preview')
    def preview_saved(connection_id: int, body: Credentials):
        with lock, connect(database_path) as con:
            return obtain(body, get_connection(con, connection_id))

    @routes.post('', status_code=201)
    def create(body: ConnectionInput):
        key = body.api_key.get_secret_value()
        if not key:
            raise HTTPException(422, 'Введите API-ключ.')
        with lock:
            reference = secrets.create(key)
            try:
                with connect(database_path) as con:
                    cursor = con.execute('INSERT INTO connections(name, provider, folder_id, secret_ref) VALUES (?, ?, ?, ?)',
                                         (body.name, body.provider, body.folder_id, reference))
                    save_preview(con, cursor.lastrowid, key, body.folder_id)
                    result = public_connection(get_connection(con, cursor.lastrowid), secrets)
            except Exception:
                secrets.delete(reference)
                raise
            return result

    @routes.put('/{connection_id}')
    def update(connection_id: int, body: ConnectionInput):
        with lock:
            with connect(database_path) as con:
                old = get_connection(con, connection_id)
            key = body.api_key.get_secret_value()
            reference = secrets.create(key) if key else old['secret_ref']
            try:
                with connect(database_path) as con:
                    con.execute('UPDATE connections SET name=?, provider=?, folder_id=?, secret_ref=? WHERE id=?',
                                (body.name, body.provider, body.folder_id, reference, connection_id))
                    save_preview(con, connection_id, key or secrets.read(reference), body.folder_id)
                    result = public_connection(get_connection(con, connection_id), secrets)
            except Exception:
                if key:
                    secrets.delete(reference)
                raise
            if key:
                secrets.delete(old['secret_ref'])
            return result

    @routes.post('/{connection_id}/refresh')
    def refresh(connection_id: int):
        with lock:
            with connect(database_path) as con:
                row = get_connection(con, connection_id)
            result = obtain(Credentials(folder_id=row['folder_id']), row)
            with connect(database_path) as con:
                con.execute('UPDATE connections SET models_json=?, models_updated_at=?, models_folder_id=? WHERE id=?',
                            (json.dumps(result['models'], ensure_ascii=False), result['models_updated_at'], row['folder_id'], connection_id))
                return public_connection(get_connection(con, connection_id), secrets)

    @routes.delete('/{connection_id}', status_code=204)
    def delete(connection_id: int):
        with lock, connect(database_path) as con:
            con.execute('BEGIN IMMEDIATE')
            row = get_connection(con, connection_id)
            agents = list(con.execute('SELECT id, name FROM agents WHERE connection_id=?', (connection_id,)))
            if agents:
                names = ', '.join(f'{agent["name"]} (№{agent["id"]})' for agent in agents)
                raise HTTPException(409, 'Сначала измените подключение у агентов: ' + names)
            secrets.delete(row['secret_ref'])
            con.execute('DELETE FROM connections WHERE id=?', (connection_id,))
        return Response(status_code=204)

    return routes
