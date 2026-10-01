"""Local HTTP API and static interface; no model or tool execution."""
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .db import DEFAULT_PATH, connect, initialize


class AgentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=500)
    instruction: str = Field(default='', max_length=50000)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError('Введите имя агента')
        return value.strip()


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=50000)

    @field_validator('content')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('Введите сообщение')
        return value


def require(connection, table, item_id):
    # Table names are internal constants, never request parameters.
    row = connection.execute(f'SELECT * FROM {table} WHERE id = ?', (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Запись не найдена. Возможно, она уже удалена.')
    return dict(row)


def create_app(database_path=DEFAULT_PATH):
    @asynccontextmanager
    async def lifespan(app):
        initialize(database_path)
        yield

    app = FastAPI(title='Локальные агенты', lifespan=lifespan)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        logging.exception('SQLite operation failed', exc_info=exc)
        return JSONResponse(status_code=500, content={'detail': 'Не удалось выполнить операцию с базой данных. Повторите попытку.'})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={'detail': 'Проверьте обязательные поля и допустимую длину текста.'})

    @app.get('/api/agents')
    def list_agents():
        with connect(database_path) as con:
            return [dict(row) for row in con.execute('SELECT * FROM agents ORDER BY id DESC')]

    @app.post('/api/agents', status_code=201)
    def add_agent(body: AgentInput):
        with connect(database_path) as con:
            cursor = con.execute('INSERT INTO agents(name, description, instruction) VALUES (?, ?, ?)',
                                 (body.name, body.description, body.instruction))
            return require(con, 'agents', cursor.lastrowid)

    @app.get('/api/agents/{agent_id}')
    def get_agent(agent_id: int):
        with connect(database_path) as con:
            return require(con, 'agents', agent_id)

    @app.put('/api/agents/{agent_id}')
    def edit_agent(agent_id: int, body: AgentInput):
        with connect(database_path) as con:
            require(con, 'agents', agent_id)
            con.execute('UPDATE agents SET name=?, description=?, instruction=? WHERE id=?',
                        (body.name, body.description, body.instruction, agent_id))
            return require(con, 'agents', agent_id)

    @app.delete('/api/agents/{agent_id}', status_code=204)
    def delete_agent(agent_id: int):
        with connect(database_path) as con:
            require(con, 'agents', agent_id)
            con.execute('DELETE FROM agents WHERE id=?', (agent_id,))
        return Response(status_code=204)

    @app.get('/api/agents/{agent_id}/conversations')
    def list_conversations(agent_id: int):
        with connect(database_path) as con:
            require(con, 'agents', agent_id)
            return [dict(row) for row in con.execute('SELECT * FROM conversations WHERE agent_id=? ORDER BY id DESC', (agent_id,))]

    @app.post('/api/agents/{agent_id}/conversations', status_code=201)
    def add_conversation(agent_id: int):
        with connect(database_path) as con:
            require(con, 'agents', agent_id)
            cursor = con.execute("INSERT INTO conversations(agent_id, title) VALUES (?, 'Новый разговор')", (agent_id,))
            return require(con, 'conversations', cursor.lastrowid)

    @app.get('/api/conversations/{conversation_id}/messages')
    def list_messages(conversation_id: int):
        with connect(database_path) as con:
            require(con, 'conversations', conversation_id)
            return [dict(row) for row in con.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY id', (conversation_id,))]

    @app.post('/api/conversations/{conversation_id}/messages', status_code=201)
    def add_message(conversation_id: int, body: MessageInput):
        with connect(database_path) as con:
            require(con, 'conversations', conversation_id)
            first = con.execute('SELECT 1 FROM messages WHERE conversation_id=? LIMIT 1', (conversation_id,)).fetchone() is None
            cursor = con.execute('INSERT INTO messages(conversation_id, content) VALUES (?, ?)', (conversation_id, body.content))
            if first:
                title = ' '.join(body.content.split())[:60]
                con.execute('UPDATE conversations SET title=? WHERE id=?', (title, conversation_id))
            return require(con, 'messages', cursor.lastrowid)

    static = Path(__file__).resolve().parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/', include_in_schema=False)
    def index():
        return FileResponse(static / 'index.html')

    return app


app = create_app()
