"""Configuração lida do arquivo .env (ou variáveis de ambiente)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
TOKEN_PLACEHOLDERS = {"", "cole_o_token_aqui", "seu_token_aqui"}


class ConfigError(Exception):
    """Erro de configuração com mensagem amigável."""


@dataclass(frozen=True)
class Config:
    token: str
    allowed_users: frozenset[int]
    default_interval: int = 60
    min_interval: int = 10
    max_concurrency: int = 5
    request_timeout: float = 30.0
    error_alert_after: int = 3
    max_watches_per_chat: int = 100
    timezone: str = "America/Sao_Paulo"
    user_agent: str = DEFAULT_USER_AGENT
    sec_user_agent: str = ""
    db_path: Path = BASE_DIR / "data" / "attsite.db"
    log_file: Path = BASE_DIR / "data" / "attsite.log"
    log_level: str = "INFO"


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError as exc:
        raise ConfigError(f"{name} precisa ser um número inteiro (valor atual: {raw!r})") from exc


def _ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"ALLOWED_USERS deve ter só IDs numéricos separados por vírgula (achei {part!r})")
        ids.add(int(part))
    return frozenset(ids)


def _path(name: str, default: Path) -> Path:
    raw = _get(name)
    if not raw:
        return default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else BASE_DIR / p


def load_config(env_file: str | os.PathLike | None = None, require_token: bool = True) -> Config:
    load_dotenv(env_file or BASE_DIR / ".env")
    token = _get("TELEGRAM_BOT_TOKEN")
    if require_token and token in TOKEN_PLACEHOLDERS:
        raise ConfigError(
            "Falta o token do bot. Abra o arquivo .env e coloque em TELEGRAM_BOT_TOKEN "
            "o token que o @BotFather te deu (algo como 123456789:AAH...)."
        )
    min_interval = _int("MIN_INTERVAL", 10, minimum=1)
    return Config(
        token=token,
        allowed_users=_ids(_get("ALLOWED_USERS")),
        default_interval=max(min_interval, _int("DEFAULT_INTERVAL", 60, minimum=1)),
        min_interval=min_interval,
        max_concurrency=_int("MAX_CONCURRENCY", 5, minimum=1),
        request_timeout=float(_int("REQUEST_TIMEOUT", 30, minimum=3)),
        error_alert_after=_int("ERROR_ALERT_AFTER", 3, minimum=1),
        max_watches_per_chat=_int("MAX_WATCHES_PER_CHAT", 100, minimum=1),
        timezone=_get("TIMEZONE", _get("TZ", "America/Sao_Paulo")),
        user_agent=_get("USER_AGENT", DEFAULT_USER_AGENT),
        sec_user_agent=_get("SEC_USER_AGENT"),
        db_path=_path("DB_PATH", BASE_DIR / "data" / "attsite.db"),
        log_file=_path("LOG_FILE", BASE_DIR / "data" / "attsite.log"),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
    )
