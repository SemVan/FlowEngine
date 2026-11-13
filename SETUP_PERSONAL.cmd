@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -e ".[desktop]"
if errorlevel 1 goto failed
echo Setup complete. Open START_PERSONAL.vbs
pause
exit /b 0
:failed
echo Setup failed. Python 3.11 or newer and internet access are required.
pause
exit /b 1
