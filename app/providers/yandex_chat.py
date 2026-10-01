"""Chat Completions and Responses with hosted tools; no automatic retries."""
import asyncio

import httpx

from .yandex import ProviderError

URL = 'https://ai.api.cloud.yandex.net/v1/chat/completions'
RESPONSES_URL = 'https://ai.api.cloud.yandex.net/v1/responses'
ACCESS_HINT = ('Проверьте роль ai.languageModels.user и область действия API-ключа '
               'yc.ai.foundationModels.execute. Права на список моделей недостаточно.')


class ChatError(ProviderError):
    def __init__(self, message, code, action, http_status=None):
        super().__init__(message)
        self.diagnostics = {'code': code, 'action': action, 'http_status': http_status}


async def complete(api_key, folder_id, model_id, messages, transport=None, tools=None):
    payload = {'model': model_id, 'messages': messages, 'stream': False, 'max_tokens': 4096, 'tool_choice': 'none'}
    if tools:
        if any(tool not in ({'type': 'web_search'}, {'type': 'code_interpreter', 'container': {'type': 'auto'}}) for tool in tools):
            raise ProviderError('Передан неподдерживаемый инструмент. Доступны веб-поиск и интерпретатор Яндекса.')
        payload = {'model': model_id, 'input': messages, 'tools': tools,
                   'tool_choice': 'auto', 'stream': False, 'max_output_tokens': 4096}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False,
                                     transport=transport) as client:
            response = await asyncio.wait_for(client.post(
                RESPONSES_URL if tools else URL, headers={'Authorization': 'Api-Key ' + api_key, 'OpenAI-Project': folder_id},
                json=payload), timeout=120)
    except httpx.ConnectTimeout:
        raise ChatError('Не удалось установить соединение с сервисом за отведённое время.', 'connect_timeout',
                        'Проверьте интернет, VPN и прокси на компьютере с сервером. Затем повторите запрос.') from None
    except httpx.ReadTimeout:
        raise ChatError('Соединение не вернуло ответ вовремя. Причина задержки на стороне сети или сервиса не установлена.', 'read_timeout',
                        'Повторите запрос позже или выберите другую текстовую модель в настройках агента.') from None
    except (httpx.TimeoutException, asyncio.TimeoutError):
        raise ChatError('Истекло время ожидания запроса. Неизвестно, успел ли провайдер обработать сообщение.', 'request_timeout',
                        'Проверьте сеть и прокси сервера. Повторите позже или выберите другую модель. Автоматического повтора нет.') from None
    except httpx.RequestError:
        raise ChatError('Не удалось связаться с Яндекс AI Studio.', 'network_error', 'Проверьте интернет, VPN, прокси и доступ к ai.api.cloud.yandex.net на компьютере с сервером.') from None
    if response.status_code == 401:
        raise ChatError('API-ключ неверен или отозван.', 'authentication', 'Откройте «Подключения» и замените ключ. ' + ACCESS_HINT, 401)
    if response.status_code == 403:
        raise ChatError('Нет прав для генерации ответа. ' + ACCESS_HINT, 'permission', 'Откройте «Подключения» и проверьте ключ и Folder ID. ' + ACCESS_HINT + (' Для инструментов также проверьте роль ai.assistants.editor.' if tools else ''), 403)
    if response.status_code in (400, 404, 422):
        raise ChatError('Сервис отклонил запрос. По одному HTTP-коду нельзя определить точную причину.', 'invalid_request',
                        'Проверьте модель и поддержку чата в настройках агента. Попробуйте новый разговор с коротким сообщением.', response.status_code)
    if response.status_code == 429:
        raise ChatError('Превышен лимит запросов или квота Яндекс AI Studio.', 'rate_limit', 'Проверьте квоты в Яндекс AI Studio и повторите позже.', 429)
    if response.status_code == 503:
        raise ChatError('Сервис генерации временно недоступен (HTTP 503).', 'service_unavailable',
                        'Сообщение сохранено. Повторите запрос позже кнопкой «Получить ответ» или выберите другую текстовую модель. '
                        'Если ошибка повторяется, сравните работу с VPN и без него на компьютере с сервером. '
                        'По HTTP 503 нельзя определить, ответил Яндекс или промежуточный прокси.', 503)
    if response.status_code >= 300:
        raise ChatError(f'Яндекс AI Studio вернул HTTP {response.status_code}.', 'provider_http', 'Повторите позже. При повторении ошибки проверьте доступность сервиса.', response.status_code)
    try:
        if tools:
            return parse_response(response.json())
        choice = response.json()['choices'][0]
        message = choice['message']
        content = message.get('content')
        if message.get('tool_calls') or message.get('function_call'):
            raise ProviderError('Модель запросила инструмент. Выполнение инструментов пока не подключено.')
        if message.get('role') != 'assistant' or not isinstance(content, str) or not content.strip():
            raise ProviderError('Модель не вернула текстовый ответ. Попробуйте другую модель или запрос.')
        return {'content': content, 'finish_reason': 'length' if choice.get('finish_reason') == 'length' else 'stop'}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError('Яндекс AI Studio вернул некорректный ответ. Сообщение сохранено.') from None


def parse_response(payload):
    status = payload.get('status')
    if status not in ('completed', 'incomplete'):
        raise ProviderError('Яндекс не завершил ответ с инструментами. Повторите запрос позже или проверьте поддержку инструментов выбранной моделью.')
    texts, sources = [], []
    for item in payload['output']:
        if item.get('type') in ('function_call', 'mcp_approval_request'):
            raise ProviderError('Модель запросила неподключённый инструмент. Разрешены только веб-поиск и интерпретатор Яндекса.')
        if item.get('type') != 'message' or item.get('role') != 'assistant':
            continue
        for part in item['content']:
            if part.get('type') == 'output_text' and isinstance(part.get('text'), str):
                texts.append(part['text'])
                for annotation in part.get('annotations', []):
                    if annotation.get('type') == 'url_citation':
                        url = annotation.get('url')
                        if isinstance(url, str) and url and url not in sources:
                            sources.append(url)
                    elif annotation.get('type') == 'container_file_citation':
                        texts.append('Создан файл: ' + str(annotation.get('filename', 'файл')) + ' (скачивание файлов пока не подключено).')
    content = '\n'.join(texts)
    if not content.strip():
        raise ProviderError('Модель не вернула текстовый ответ после работы инструментов. Попробуйте другую модель или запрос.')
    if sources:
        content += '\n\nИсточники:\n' + '\n'.join(sources)
    return {'content': content, 'finish_reason': 'length' if status == 'incomplete' and payload.get('incomplete_details', {}).get('reason') == 'max_output_tokens' else 'stop'}
