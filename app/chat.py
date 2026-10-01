"""Durable generation status and cancellable, single-process background requests."""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .connections import get_connection
from .db import connect
from .providers.yandex import ProviderError
from .providers.yandex_chat import complete


class GenerationInput(BaseModel):
    message_id: int = Field(gt=0)


def ensure_idle(con, conversation_id):
    if con.execute("SELECT 1 FROM generations WHERE conversation_id=? AND status='running'", (conversation_id,)).fetchone():
        raise HTTPException(409, 'В этом разговоре уже ожидается ответ. Дождитесь его или нажмите «Остановить».')


class ChatService:
    def __init__(self, database_path, secrets, adapters=None):
        self.path = database_path
        self.secrets = secrets
        self.adapters = adapters if adapters is not None else {'yandex': complete}
        self.tasks = {}
        self.router = APIRouter(prefix='/api')
        self.router.add_api_route('/conversations/{conversation_id}/generate', self.start, methods=['POST'])
        self.router.add_api_route('/conversations/{conversation_id}/generation', self.latest, methods=['GET'])
        self.router.add_api_route('/generations/{generation_id}', self.get, methods=['GET'])
        self.router.add_api_route('/generations/{generation_id}/stop', self.stop, methods=['POST'])

    def recover(self):
        with connect(self.path) as con:
            con.execute("UPDATE generations SET status='failed', error=?,finished_at=?,diagnostics_json=? WHERE status='running'",
                        ('Сервер был перезапущен до завершения ответа. Можно повторить запрос.', datetime.now(timezone.utc).isoformat(),
                         json.dumps({'code': 'server_restarted', 'action': 'Повторите запрос вручную. Автоматическая повторная отправка отключена.'})))

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.recover()

    def get(self, generation_id: int):
        with connect(self.path) as con:
            row = con.execute('SELECT * FROM generations WHERE id=?', (generation_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Запрос не найден.')
            result = dict(row)
            result['diagnostics'] = json.loads(result.pop('diagnostics_json') or '{}')
            result['timeout_seconds'] = 120
            if row['started_at']:
                end = datetime.fromisoformat(row['finished_at']) if row['finished_at'] else datetime.now(timezone.utc)
                result['elapsed_seconds'] = max(0, round((end - datetime.fromisoformat(row['started_at'])).total_seconds(), 1))
            else:
                result['elapsed_seconds'] = None
            message = con.execute('SELECT * FROM messages WHERE id=?', (row['assistant_message_id'],)).fetchone()
            result['message'] = dict(message) if message else None
            return result

    def latest(self, conversation_id: int):
        with connect(self.path) as con:
            if con.execute('SELECT 1 FROM conversations WHERE id=?', (conversation_id,)).fetchone() is None:
                raise HTTPException(404, 'Разговор не найден.')
            row = con.execute('SELECT id FROM generations WHERE conversation_id=? ORDER BY id DESC LIMIT 1', (conversation_id,)).fetchone()
        return self.get(row['id']) if row else None

    async def start(self, conversation_id: int, body: GenerationInput):
        with connect(self.path) as con:
            con.execute('BEGIN IMMEDIATE')
            previous = con.execute('SELECT * FROM generations WHERE user_message_id=? AND conversation_id=?', (body.message_id, conversation_id)).fetchone()
            if previous and previous['status'] in ('running', 'completed'):
                return self.get(previous['id'])
            ensure_idle(con, conversation_id)
            conversation = con.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
            if conversation is None:
                raise HTTPException(404, 'Разговор не найден.')
            last = con.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1', (conversation_id,)).fetchone()
            if not last or last['id'] != body.message_id or last['role'] != 'user':
                raise HTTPException(409, 'Можно получить ответ только на последнее сообщение пользователя. Обновите историю.')
            agent = con.execute('SELECT * FROM agents WHERE id=?', (conversation['agent_id'],)).fetchone()
            if not agent['connection_id'] or not agent['model_id']:
                raise HTTPException(422, 'Выберите подключение и модель в настройках агента.')
            connection = get_connection(con, agent['connection_id'])
            adapter = self.adapters.get(connection['provider'])
            if adapter is None:
                raise HTTPException(422, 'Этот источник пока не поддерживает ответы.')
            key = self.secrets.read(connection['secret_ref'])
            messages = []
            if agent['instruction'].strip():
                messages.append({'role': 'system', 'content': agent['instruction']})
            messages.extend(dict(row) for row in con.execute('SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id', (conversation_id,)))
            if previous:
                generation_id = previous['id']
                con.execute("UPDATE generations SET status='running', error=NULL, finish_reason=NULL, model_id=? WHERE id=?", (agent['model_id'], generation_id))
            else:
                generation_id = con.execute("INSERT INTO generations(conversation_id,user_message_id,model_id,status) VALUES (?,?,?,'running')",
                                            (conversation_id, body.message_id, agent['model_id'])).lastrowid
            con.execute('UPDATE generations SET started_at=?,finished_at=NULL,diagnostics_json=NULL WHERE id=?',
                        (datetime.now(timezone.utc).isoformat(), generation_id))
        tools = []
        if agent['web_search']:
            tools.append({'type': 'web_search'})
        if agent['code_interpreter']:
            tools.append({'type': 'code_interpreter', 'container': {'type': 'auto'}})
        task = asyncio.create_task(self.run(generation_id, conversation_id, adapter, key, connection['folder_id'], agent['model_id'], messages, tools))
        self.tasks[generation_id] = task
        task.add_done_callback(lambda finished: self.tasks.pop(generation_id, None) if self.tasks.get(generation_id) is finished else None)
        return self.get(generation_id)

    def finish_error(self, generation_id, status, error, diagnostics=None):
        with connect(self.path) as con:
            con.execute("UPDATE generations SET status=?,error=?,finished_at=?,diagnostics_json=? WHERE id=? AND status='running'",
                        (status, error, datetime.now(timezone.utc).isoformat(), json.dumps(diagnostics or {}), generation_id))

    async def run(self, generation_id, conversation_id, adapter, key, folder, model, messages, tools):
        try:
            reply = await adapter(key, folder, model, messages, **({'tools': tools} if tools else {}))
            with connect(self.path) as con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute('SELECT status FROM generations WHERE id=?', (generation_id,)).fetchone()
                if not row or row['status'] != 'running':
                    return
                message_id = con.execute("INSERT INTO messages(conversation_id,role,content) VALUES (?,'assistant',?)", (conversation_id, reply['content'])).lastrowid
                con.execute("UPDATE generations SET status='completed',assistant_message_id=?,finish_reason=?,finished_at=? WHERE id=?",
                            (message_id, reply['finish_reason'], datetime.now(timezone.utc).isoformat(), generation_id))
        except asyncio.CancelledError:
            self.finish_error(generation_id, 'cancelled', 'Ожидание ответа остановлено. Сообщение пользователя сохранено.')
        except ProviderError as error:
            self.finish_error(generation_id, 'failed', str(error), getattr(error, 'diagnostics', None))
        except Exception:
            # Never log request objects, credentials, upstream bodies or prompt text.
            self.finish_error(generation_id, 'failed', 'Не удалось получить или сохранить ответ. Повторите запрос.')

    async def stop(self, generation_id: int):
        result = self.get(generation_id)
        if result['status'] == 'running':
            self.finish_error(generation_id, 'cancelled', 'Ожидание ответа остановлено. Сообщение пользователя сохранено.')
            task = self.tasks.get(generation_id)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return self.get(generation_id)
