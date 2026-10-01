@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Не найден Python в .venv. Выполните команды установки из README.md.
  pause
  exit /b 1
)
echo Откройте в браузере http://127.0.0.1:8000
echo Для остановки нажмите Ctrl+C.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 (
  echo Не удалось запустить сервер. Проверьте зависимости и доступность порта 8000.
  pause
)
