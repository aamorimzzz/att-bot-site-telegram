@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title AttSite Bot
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set "PY=python"
where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Criando ambiente virtual...
    %PY% -m venv .venv
    if errorlevel 1 goto :nopython
)

echo [2/3] Instalando/atualizando dependencias...
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :piperro

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo  ==========================================================
    echo   Primeira vez! Vou abrir o arquivo .env no Bloco de Notas.
    echo   Cole o token do @BotFather em TELEGRAM_BOT_TOKEN,
    echo   salve [Ctrl+S] e FECHE o Bloco de Notas para continuar.
    echo  ==========================================================
    echo.
    notepad ".env"
)

echo [3/3] Iniciando o bot... (feche esta janela ou aperte Ctrl+C para parar)
".venv\Scripts\python.exe" bot.py
echo.
pause
exit /b 0

:nopython
echo.
echo  Python nao encontrado.
echo  Instale o Python 3.10+ em https://www.python.org/downloads/
echo  e MARQUE a opcao "Add python.exe to PATH" na instalacao.
echo.
pause
exit /b 1

:piperro
echo.
echo  Falha ao instalar as dependencias. Confira sua internet e tente de novo.
echo.
pause
exit /b 1
