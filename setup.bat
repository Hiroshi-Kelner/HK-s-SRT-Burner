@echo off
setlocal

:: Request administrator privileges.
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

echo Installing FFmpeg...
winget install --id Gyan.FFmpeg -e --source winget --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo FFmpeg installation failed.
    pause
    exit /b 1



echo Setup complete. Just know that you will need Python in order to actually run the app.
echo So, if Python's not installed, you can look up how to install Python on the web.
pause
endlocal
