@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw line_sticker_dl.py
  exit /b
)
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw line_sticker_dl.py
  exit /b
)
echo Python not found. Install Python 3 first: https://www.python.org/downloads/
pause
