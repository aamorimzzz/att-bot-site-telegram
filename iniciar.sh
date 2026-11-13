#!/usr/bin/env bash
# AttSite Bot — Linux / macOS
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
    echo "[1/3] Criando ambiente virtual..."
    "$PY" -m venv .venv
fi

echo "[2/3] Instalando/atualizando dependências..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo "  Arquivo .env criado. Edite-o (ex: nano .env), coloque o token do"
    echo "  @BotFather em TELEGRAM_BOT_TOKEN e rode ./iniciar.sh de novo."
    echo
    exit 0
fi

echo "[3/3] Iniciando o bot (Ctrl+C para parar)..."
exec .venv/bin/python bot.py "$@"
