from functools import lru_cache

from openai import AsyncOpenAI, OpenAI

from ..config import get_settings


def _headers() -> dict:
    return {"HTTP-Referer": get_settings().app_url, "X-Title": "Manualagerie"}


@lru_cache
def get_client() -> OpenAI:
    s = get_settings()
    return OpenAI(api_key=s.openrouter_api_key, base_url=s.openrouter_base_url,
                  default_headers=_headers(), timeout=120, max_retries=2)


@lru_cache
def get_async_client() -> AsyncOpenAI:
    s = get_settings()
    return AsyncOpenAI(api_key=s.openrouter_api_key, base_url=s.openrouter_base_url,
                       default_headers=_headers(), timeout=120, max_retries=2)
