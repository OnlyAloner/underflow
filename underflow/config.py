"""Все настройки из окружения. Одно место, чтобы не искать по коду."""

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


@dataclass
class Config:
    # где лежит база
    db_path: str = field(default_factory=lambda: _env("UNDERFLOW_DB", "./data/underflow.db"))
    # кто думает: api (ключ Anthropic), cli (claude -p по подписке), off (только полнотекст)
    librarian: str = field(default_factory=lambda: _env("UNDERFLOW_LIBRARIAN", "auto"))
    model: str = field(default_factory=lambda: _env("UNDERFLOW_MODEL", "claude-opus-5-5"))
    # усилие библиотекаря на поиске и на приёме ответа: поиск должен быть быстрым
    search_effort: str = field(default_factory=lambda: _env("UNDERFLOW_SEARCH_EFFORT", "low"))
    curate_effort: str = field(default_factory=lambda: _env("UNDERFLOW_CURATE_EFFORT", "medium"))
    # серверный фолбэк Anthropic на отказ модели; на прокси и чужих облаках выключать
    fallbacks: bool = field(default_factory=lambda: _env("UNDERFLOW_FALLBACKS", "1") == "1")
    # пусто = писать может кто угодно; иначе нужен Bearer из списка
    write_tokens: list[str] = field(default_factory=lambda: _list("UNDERFLOW_WRITE_TOKENS"))
    # запросов в минуту с одного адреса (0 = без лимита)
    rate_per_min: int = field(default_factory=lambda: int(_env("UNDERFLOW_RATE", "60") or 0))
    # садовник: раз во сколько секунд перебирать промахи поиска (0 = только руками)
    garden_every: int = field(default_factory=lambda: int(_env("UNDERFLOW_GARDEN_EVERY", "3600") or 0))
    # доверять X-Forwarded-For (только если сервер за своим прокси)
    trust_proxy: bool = field(default_factory=lambda: _env("UNDERFLOW_TRUST_PROXY", "0") == "1")

    def resolved_librarian(self) -> str:
        if self.librarian != "auto":
            return self.librarian
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return "api"
        return "off"
