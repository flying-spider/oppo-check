@echo off
cd /d "%~dp0"

set http_proxy=
set https_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set all_proxy=
set ALL_PROXY=

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found. Install Python 3.9+ and add to PATH.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    call .venv\Scripts\activate.bat
    python -c "" >nul 2>nul
    if %errorlevel% equ 0 goto activated
    echo Old .venv is broken, recreating...
    rmdir /s /q .venv
)

echo Creating virtual environment...
python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo Failed to create .venv. Try running as Administrator.
    pause
    exit /b 1
)

:activated
call .venv\Scripts\activate.bat

if exist ".venv\install_done.flag" goto start
echo Installing dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)
echo Installing Playwright Chromium...
playwright install chrome
echo. > .venv\install_done.flag

:start
echo.
echo Starting OPPO Query Tool...
echo.
set "CHROME_PATH="
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if defined CHROME_PATH (
    start "" "%CHROME_PATH%" --no-proxy-server "http://127.0.0.1:5000"
) else (
    start "" "http://127.0.0.1:5000"
)
python oppo_web.py
pause