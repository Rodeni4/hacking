import json
import sqlite3
import time
import unittest
from contextlib import closing

import httpx

from app.providers.yandex_chat import ChatError, complete, URL, RESPONSES_URL
from app.providers.yandex import ProviderError
from tests import test_connections


class ChatAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_tools_are_rejected_before_network(self):
        def handler(request):
            self.fail('Неподдерживаемый инструмент не должен отправляться провайдеру')
        for name in ['list_files', 'execute_python_code', 'web_search_read', 'web_search_with_grep', 'shell']:
            with self.assertRaises(ProviderError):
                await complete('dummy', 'folder', 'model', [], tools=[{'type': name}], transport=httpx.MockTransport(handler))

    async def test_hosted_tools_use_responses_and_preserve_sources(self):
        for tools in [[{'type': 'web_search'}], [{'type': 'code_interpreter', 'container': {'type': 'auto'}}],
                      [{'type': 'web_search'}, {'type': 'code_interpreter', 'container': {'type': 'auto'}}]]:
            def handler(request):
                self.assertEqual(str(request.url), RESPONSES_URL)
                self.assertEqual(request.headers['OpenAI-Project'], 'folder')
                body = json.loads(request.content)
                self.assertEqual(body['tools'], tools)
                self.assertEqual(body['input'], [{'role': 'system', 'content': 'Инструкция'}, {'role': 'user', 'content': 'Вопрос'}])
                self.assertNotIn('messages', body)
                return httpx.Response(200, json={'status': 'completed', 'output': [
                    {'type': 'web_search_call', 'status': 'completed'},
                    {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Ответ', 'annotations': [
                        {'type': 'url_citation', 'url': 'https://example.org/source'}]}]}]})
            result = await complete('dummy', 'folder', 'model', [{'role': 'system', 'content': 'Инструкция'}, {'role': 'user', 'content': 'Вопрос'}],
                                    tools=tools, transport=httpx.MockTransport(handler))
            self.assertIn('https://example.org/source', result['content'])
            self.assertEqual(result['finish_reason'], 'stop')

    async def test_responses_failure_and_missing_text_are_not_saved_as_answers(self):
        for body in [{'status': 'failed', 'error': {'message': 'dummy-secret'}},
                     {'status': 'completed', 'output': []},
                     {'status': 'completed', 'output': [{'type': 'function_call'}]},
                     {'status': 'completed', 'output': 'broken'}]:
            with self.assertRaises(ProviderError) as raised:
                await complete('dummy-secret', 'folder', 'model', [], tools=[{'type': 'web_search'}],
                               transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
            self.assertNotIn('dummy-secret', str(raised.exception))

    async def test_service_unavailable_is_safe_and_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503, text='dummy-secret upstream details')

        with self.assertRaises(ChatError) as raised:
            await complete('dummy-secret', 'folder', 'model', [], transport=httpx.MockTransport(handler))
        self.assertEqual(len(calls), 1)
        self.assertEqual(raised.exception.diagnostics['code'], 'service_unavailable')
        self.assertEqual(raised.exception.diagnostics['http_status'], 503)
        self.assertIn('VPN', raised.exception.diagnostics['action'])
        self.assertNotIn('dummy-secret', str(raised.exception))
        self.assertNotIn('upstream details', str(raised.exception.diagnostics))

    async def test_request_and_exact_response(self):
        async def handler(request):
            self.assertEqual(str(request.url), URL)
            self.assertEqual(request.headers['Authorization'], 'Api-Key dummy-secret')
            self.assertEqual(request.headers['OpenAI-Project'], 'folder')
            self.assertEqual(json.loads(request.content), {'model': 'gpt://exact/ID@version', 'messages': [{'role': 'user', 'content': 'Текст'}], 'stream': False, 'max_tokens': 4096, 'tool_choice': 'none'})
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Ответ\nбез изменений'}, 'finish_reason': 'length'}]})
        result = await complete('dummy-secret', 'folder', 'gpt://exact/ID@version', [{'role': 'user', 'content': 'Текст'}], transport=httpx.MockTransport(handler))
        self.assertEqual(result, {'content': 'Ответ\nбез изменений', 'finish_reason': 'length'})

    async def test_errors_empty_response_and_tools(self):
        responses = [httpx.Response(code, text='dummy-secret upstream details') for code in [400, 401, 403, 404, 429, 500, 302]]
        responses += [httpx.Response(200, content=b'broken'), httpx.Response(200, json={}),
                      httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': ''}}]}),
                      httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'text', 'tool_calls': [{}]}}]})]
        for response in responses:
            with self.subTest(status=response.status_code):
                with self.assertRaises(ProviderError) as raised:
                    await complete('dummy-secret', 'folder', 'model', [], transport=httpx.MockTransport(lambda request: response))
                self.assertNotIn('dummy-secret', str(raised.exception))
        for exception in [httpx.ConnectError('dummy-secret'), httpx.ReadTimeout('dummy-secret')]:
            def fail(request):
                raise exception
            with self.assertRaises(ProviderError) as raised:
                await complete('dummy-secret', 'folder', 'model', [], transport=httpx.MockTransport(fail))
            self.assertNotIn('dummy-secret', str(raised.exception))


class ChatTests(test_connections.ConnectionTests):
    def test_tool_settings_persist_and_control_generation(self):
        self.setup_chat()
        self.assertFalse(self.agent['web_search'])
        body = {'name': 'Чат', 'instruction': 'Инструкция', 'connection_id': self.agent['connection_id'],
                'model_id': self.agent['model_id'], 'web_search': True, 'code_interpreter': True}
        self.request(f'/api/agents/{self.agent["id"]}', 'PUT', body)
        self.stop()
        self.start()
        saved = self.request(f'/api/agents/{self.agent["id"]}')
        self.assertIs(saved['web_search'], True)
        self.assertIs(saved['code_interpreter'], True)
        self.assertEqual(self.wait_result(self.generate(self.message()))['status'], 'completed')
        sent = json.loads((self.fixture.parent / 'chat-request.json').read_text(encoding='utf-8'))
        self.assertEqual(sent['tools'], [{'type': 'web_search'}, {'type': 'code_interpreter', 'container': {'type': 'auto'}}])
        body.update(web_search=False, code_interpreter=False)
        self.request(f'/api/agents/{self.agent["id"]}', 'PUT', body)
        self.assertEqual(self.wait_result(self.generate(self.message('Без инструментов')))['status'], 'completed')
        sent = json.loads((self.fixture.parent / 'chat-request.json').read_text(encoding='utf-8'))
        self.assertNotIn('tools', sent)
        self.assertIn('messages', sent)

    def server_code(self, port):
        script = super().server_code(port)
        self.chat_fixture = self.fixture.parent / 'chat-response.json'
        if not self.chat_fixture.exists():
            self.chat_fixture.write_text('{}', encoding='utf-8')
        script = script.replace('from tests.support import fixture_provider;', 'from tests.support import fixture_provider, fixture_chat_provider;')
        script = script.replace('uvicorn.run(create_app(', 'chat=lambda key, folder, model, messages, **options: fixture_chat_provider(Path(db).parent, key, folder, model, messages, **options); uvicorn.run(create_app(')
        return script.replace('{"yandex": provider})', '{"yandex": provider}, {"yandex": chat})')

    def setup_chat(self):
        connection = self.connection()
        self.agent = self.request('/api/agents', 'POST', {'name': 'Чат', 'instruction': 'Точная инструкция\nна русском', 'connection_id': connection['id'], 'model_id': test_connections.MODELS[0]['id']}, 201)
        self.cid = self.request(f'/api/agents/{self.agent["id"]}/conversations', 'POST', status=201)['id']

    def message(self, content='Первый вопрос'):
        return self.request(f'/api/conversations/{self.cid}/messages', 'POST', {'content': content}, 201)

    def generate(self, message):
        return self.request(f'/api/conversations/{self.cid}/generate', 'POST', {'message_id': message['id']})

    def wait_result(self, generation):
        for _ in range(100):
            result = self.request(f'/api/generations/{generation["id"]}')
            if result['status'] != 'running':
                return result
            time.sleep(.05)
        self.fail('Generation did not finish')

    def test_history_reply_idempotence_restart(self):
        self.setup_chat()
        first = self.message()
        generation = self.generate(first)
        answer = self.wait_result(generation)
        self.assertEqual(answer['status'], 'completed')
        self.assertEqual(answer['message']['role'], 'assistant')
        self.assertEqual(self.generate(first)['id'], generation['id'])
        second = self.message('Второй вопрос')
        self.assertEqual(self.wait_result(self.generate(second))['status'], 'completed')
        sent = json.loads((self.fixture.parent / 'chat-request.json').read_text(encoding='utf-8'))
        self.assertEqual(sent['model'], test_connections.MODELS[0]['id'])
        self.assertEqual([item['role'] for item in sent['messages']], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(sent['messages'][0]['content'], 'Точная инструкция\nна русском')
        self.assertEqual(sent['messages'][2]['content'], answer['message']['content'])
        self.stop()
        self.start()
        history = self.request(f'/api/conversations/{self.cid}/messages')
        self.assertEqual(len(history), 4)
        self.assertEqual(history[1], answer['message'])
        self.cid = self.request(f'/api/agents/{self.agent["id"]}/conversations', 'POST', status=201)['id']
        self.wait_result(self.generate(self.message('Новый разговор')))
        sent = json.loads((self.fixture.parent / 'chat-request.json').read_text(encoding='utf-8'))
        self.assertEqual(len(sent['messages']), 2)
        self.request(f'/api/agents/{self.agent["id"]}', 'DELETE', status=204)

    def test_generation_status_requires_existing_conversation(self):
        self.setup_chat()
        self.assertIsNone(self.request(f'/api/conversations/{self.cid}/generation'))
        self.request(f'/api/agents/{self.agent["id"]}', 'DELETE', status=204)
        self.request(f'/api/conversations/{self.cid}/generation', status=404)

    def test_delete_chat_cascades_and_preserves_other_chat(self):
        self.setup_chat()
        other = self.request(f'/api/agents/{self.agent["id"]}/conversations', 'POST', status=201)['id']
        kept = self.request(f'/api/conversations/{other}/messages', 'POST', {'content': 'Оставить'}, 201)
        generation = self.wait_result(self.generate(self.message()))
        self.request(f'/api/conversations/{self.cid}', 'DELETE', status=204)
        self.request(f'/api/conversations/{self.cid}/messages', status=404)
        self.request(f'/api/generations/{generation["id"]}', status=404)
        self.request(f'/api/conversations/{self.cid}', 'DELETE', status=404)
        self.assertEqual(self.request(f'/api/conversations/{other}/messages'), [kept])
        self.assertEqual([chat['id'] for chat in self.request(f'/api/agents/{self.agent["id"]}/conversations')], [other])
        with closing(sqlite3.connect(self.database)) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM messages WHERE conversation_id=?', (self.cid,)).fetchone()[0], 0)

    def test_delete_running_chat_requires_stop(self):
        self.setup_chat()
        self.chat_fixture.write_text('{"delay":10}', encoding='utf-8')
        generation = self.generate(self.message())
        self.request(f'/api/conversations/{self.cid}', 'DELETE', status=409)
        self.assertEqual(len(self.request(f'/api/conversations/{self.cid}/messages')), 1)
        self.request(f'/api/generations/{generation["id"]}/stop', 'POST')
        self.request(f'/api/conversations/{self.cid}', 'DELETE', status=204)

    def test_failure_retry_and_cancel(self):
        self.setup_chat()
        message = self.message()
        self.chat_fixture.write_text('{"status":403}', encoding='utf-8')
        failed = self.wait_result(self.generate(message))
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['diagnostics']['code'], 'permission')
        self.assertEqual(failed['diagnostics']['http_status'], 403)
        self.assertIsNotNone(failed['started_at'])
        self.assertIsNotNone(failed['finished_at'])
        self.assertGreaterEqual(failed['elapsed_seconds'], 0)
        self.assertIn('yc.ai.foundationModels.execute', failed['error'])
        self.assertEqual(len(self.request(f'/api/conversations/{self.cid}/messages')), 1)
        self.chat_fixture.write_text('{"delay":10}', encoding='utf-8')
        running = self.generate(message)
        self.assertEqual(self.generate(message)['id'], running['id'])
        self.request(f'/api/conversations/{self.cid}/messages', 'POST', {'content': 'Дубликат'}, 409)
        cancelled = self.request(f'/api/generations/{running["id"]}/stop', 'POST')
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(len(self.request(f'/api/conversations/{self.cid}/messages')), 1)
        self.chat_fixture.write_text('{}', encoding='utf-8')
        completed = self.wait_result(self.generate(message))
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(len(self.request(f'/api/conversations/{self.cid}/messages')), 2)

    def test_interrupted_generation_recovery_and_no_model(self):
        self.setup_chat()
        self.chat_fixture.write_text('{"delay":10}', encoding='utf-8')
        generation = self.generate(self.message())
        self.stop()
        self.start()
        recovered = self.request(f'/api/generations/{generation["id"]}')
        self.assertEqual(recovered['status'], 'failed')
        self.assertIsNone(recovered['message'])
        self.request(f'/api/agents/{self.agent["id"]}', 'PUT', {'name': 'Без модели'})
        self.request(f'/api/conversations/{self.cid}/generate', 'POST', {'message_id': generation['user_message_id']}, 422)
