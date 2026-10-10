@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   Video Downloader
echo ============================================
echo.

if not exist "%~dp0venv\Scripts\python.exe" (
    echo [ERROR] venv not found: venv\Scripts\python.exe
    echo         Please make sure the virtual environment exists.
    echo.
    pause
    exit /b 1
)

"%~dp0venv\Scripts\python.exe" "%~dp0main.py"

echo.
echo App exited. Logs: %%LOCALAPPDATA%%\VideoDownloader\logs\app.log
pause
