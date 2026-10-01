"""Model discovery adapters. Generation is intentionally not implemented."""
from .yandex import fetch_models

PROVIDERS = {'yandex': fetch_models}
