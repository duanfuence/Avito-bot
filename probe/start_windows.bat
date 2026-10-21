@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Avito Market Scanner - проверка данных

set "PY="
where py >nul 2>nul && py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (python --version >nul 2>nul && set "PY=python")
if not defined PY goto :no_python

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :old_python

if exist ".venv\Scripts\python.exe" goto :install
echo Первый запуск: создаю окружение Python (около минуты)...
%PY% -m venv .venv
if errorlevel 1 goto :venv_fail

:install
echo Проверяю библиотеки...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :pip_fail

echo.
".venv\Scripts\python.exe" avito_probe.py %*
echo.
pause
exit /b

:no_python
echo ОШИБКА: Python не найден.
echo Установите Python с https://www.python.org/downloads/
echo и при установке отметьте галочку "Add python.exe to PATH".
echo После установки закройте это окно и запустите файл снова.
pause
exit /b 1

:old_python
echo ОШИБКА: нужен Python версии 3.10 или новее.
%PY% --version
echo Установите свежую версию с https://www.python.org/downloads/
pause
exit /b 1

:venv_fail
echo ОШИБКА: не удалось создать окружение Python в папке:
echo %CD%
echo Попробуйте переместить папку проекта, например, в C:\avito-bot
pause
exit /b 1

:pip_fail
echo ОШИБКА: не удалось установить библиотеки.
echo Проверьте подключение к интернету. Если работает VPN или антивирус
echo с проверкой HTTPS, попробуйте временно его отключить и запустить снова.
pause
exit /b 1
