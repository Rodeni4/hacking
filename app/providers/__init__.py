"""Model discovery adapters; text generation lives in yandex_chat."""
from .yandex import fetch_models

PROVIDERS = {'yandex': fetch_models}
