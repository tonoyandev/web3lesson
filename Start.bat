@echo off
rem Double-click to open anti-persona on Windows.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 app.py) else (python app.py)
if errorlevel 1 (
  echo.
  echo Python 3 is needed. Get it from https://www.python.org/downloads/
  pause
)
