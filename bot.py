"""AttSite Bot — ponto de entrada.

    python bot.py                         inicia o bot do Telegram
    python bot.py --teste URL [--seletor CSS] [--modo texto|html|json]
                                          testa a extração de uma página (sem Telegram)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(level: str, log_file) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%d/%m %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        root.warning("Sem arquivo de log (%s)", exc)
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def test_url(url: str, selector: str | None, mode: str) -> int:
    from attsite.config import load_config
    from attsite.monitor import ExtractError, FetchError, Monitor
    from attsite.storage import Storage

    cfg = load_config(require_token=False)
    mon = Monitor(Storage(":memory:"), cfg)
    try:
        res, lines = await mon.fetch_lines(url, mode, selector, mon.user_agent_for({"url": url, "user_agent": None}))
    except (FetchError, ExtractError) as exc:
        print(f"ERRO: {exc}")
        return 1
    finally:
        await mon.close()
    print(f"HTTP {res.status} · {len(res.content) // 1024} KB · {res.elapsed:.1f}s · {len(lines)} linhas\n")
    for i, line in enumerate(lines[:60], 1):
        print(f"{i:>4}  {line}")
    if len(lines) > 60:
        print(f"   … +{len(lines) - 60} linhas")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AttSite Bot — monitor de mudanças em sites")
    parser.add_argument("--teste", "--test", metavar="URL", help="testa a extração de uma URL e sai")
    parser.add_argument("--seletor", "--selector", metavar="CSS", help="seletor CSS para o --teste")
    parser.add_argument("--modo", "--mode", default="texto", choices=["texto", "html", "json"])
    args = parser.parse_args()

    from attsite.config import ConfigError, load_config

    if args.teste:
        setup_logging("WARNING", __import__("pathlib").Path("data/attsite.log"))
        return asyncio.run(test_url(args.teste, args.seletor, args.modo))

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"\n  ❌ {exc}\n")
        return 2
    setup_logging(cfg.log_level, cfg.log_file)

    from attsite.telegram_bot import run
    from telegram.error import InvalidToken, NetworkError

    try:
        run(cfg)
    except InvalidToken:
        print("\n  ❌ O Telegram recusou o token. Confira TELEGRAM_BOT_TOKEN no .env (copie de novo do @BotFather).\n")
        return 2
    except NetworkError as exc:
        print(f"\n  ❌ Não consegui falar com o Telegram ({exc}).\n"
              "     Confira sua internet / firewall / VPN e tente de novo.\n")
        return 3
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
