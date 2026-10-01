"""Official Yandex AI Studio Models API, without SDK or generation calls."""
import json
import socket
from http.client import HTTPException as HTTPTransportError
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

URL = 'https://ai.api.cloud.yandex.net/v1/models'
ACCESS_HINT = ('Проверьте Folder ID, роль сервисного аккаунта ai.languageModels.user '
               'и область действия API-ключа yc.ai.models.viewer.')


class ProviderError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirected endpoint.


def fetch_models(api_key, folder_id, opener=None):
    request = Request(URL, headers={'Authorization': 'Api-Key ' + api_key, 'x-project': folder_id}, method='GET')
    try:
        client = opener or build_opener(NoRedirect())
        with client.open(request, timeout=20) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 401:
            raise ProviderError('API-ключ неверен или отозван. ' + ACCESS_HINT) from None
        if status == 403:
            raise ProviderError('Недостаточно прав доступа. ' + ACCESS_HINT) from None
        raise ProviderError(f'Сервис моделей вернул HTTP {status}. Повторите попытку позже.') from None
    except (TimeoutError, socket.timeout):
        raise ProviderError('Время ожидания сервиса моделей истекло (20 секунд). Повторите попытку.') from None
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ProviderError('Время ожидания сервиса моделей истекло (20 секунд). Повторите попытку.') from None
        raise ProviderError('Не удалось связаться с сервисом моделей. Проверьте сеть и повторите попытку.') from None
    except (OSError, ValueError, HTTPTransportError):
        raise ProviderError('Сервис моделей недоступен или вернул некорректный ответ.') from None
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise ProviderError('Некорректный ответ сервиса: ожидается список data[].')
    models = []
    for item in payload['data']:
        if (not isinstance(item, dict) or
                any(not isinstance(item.get(key), str) or not item[key] for key in ('id', 'owned_by', 'object')) or
                type(item.get('created')) is not int):
            raise ProviderError('Некорректный ответ сервиса: повреждённая запись модели.')
        models.append({key: item[key] for key in ('id', 'owned_by', 'object', 'created')})
    return models
