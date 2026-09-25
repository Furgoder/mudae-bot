@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" goto no_venv
if not exist "Vars.py" goto no_vars

echo Starting Mudae bot. Ouroharvest is controlled by the timer in Bot.py.

:restart
call venv\Scripts\activate.bat
python Bot.py

echo Bot stopped. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto restart

:no_venv
echo [ERROR] Virtual environment "venv" not found.
echo Run setup.bat first.
pause
exit /b 1

:no_vars
echo [ERROR] Vars.py not found.
echo Copy Vars.example.py to Vars.py and fill in your settings:
echo   copy Vars.example.py Vars.py
pause
exit /b 1
