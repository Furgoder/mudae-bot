@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Creating virtual environment...
python -m venv venv
if errorlevel 1 (
    echo [ERROR] Failed to create venv. Is Python installed and in PATH?
    pause
    exit /b 1
)

echo Activating environment and installing dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo --------------------------------------------------
echo Setup complete!
echo Next: copy Vars.example.py to Vars.py and fill in your data.
echo   copy Vars.example.py Vars.py
echo Then run start.bat
echo --------------------------------------------------
pause
