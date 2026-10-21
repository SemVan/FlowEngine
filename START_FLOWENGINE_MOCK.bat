@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FlowEngine - MOCK MODE

set "ROOT=%~dp0"
set "FLOWENGINE_CONFIG_DIR=%~dp0personal-data"
if not exist "%FLOWENGINE_CONFIG_DIR%" mkdir "%FLOWENGINE_CONFIG_DIR%"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "PY_KIND="

echo.
echo ========================================
echo   FlowEngine - MOCK MODE
echo ========================================
echo Folder: %ROOT%
echo.

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo Existing .venv is not usable on this computer. Rebuilding it...
        rmdir /s /q "%~dp0.venv"
    )
)

if not exist "%VENV_PY%" (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY_KIND=launcher"
    )

    if not defined PY_KIND (
        where python >nul 2>&1
        if not errorlevel 1 (
            python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
            if not errorlevel 1 set "PY_KIND=python"
        )
    )

    if not defined PY_KIND (
        echo [ERROR] Python 3.11 or newer was not found.
        echo Install Python, then double-click this file again.
        echo Opening the official Python download page...
        start "" "https://www.python.org/downloads/windows/"
        echo.
        pause
        exit /b 1
    )

    echo First launch on this computer: creating .venv...
    if /I "!PY_KIND!"=="launcher" (
        py -3 -m venv "%~dp0.venv"
    ) else (
        python -m venv "%~dp0.venv"
    )
    if errorlevel 1 goto :setup_failed
)

"%VENV_PY%" -c "import fastapi,uvicorn,serial,serial_asyncio,jinja2,yaml,structlog,pydantic,platformdirs" >nul 2>&1
if errorlevel 1 (
    echo Installing FlowEngine dependencies. This is needed only once per computer...
    "%VENV_PY%" -m pip install -e .
    if errorlevel 1 goto :setup_failed
)

echo.
echo Starting FlowEngine in MOCK mode...
echo The browser will open automatically.
echo Keep this window open while using FlowEngine.
echo.

start "" "%VENV_PY%" -c "import time,webbrowser; time.sleep(3); webbrowser.open('http://127.0.0.1:8765/')"
"%VENV_PY%" -m flowengine --mock
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo FlowEngine stopped with error code %EXITCODE%.
) else (
    echo FlowEngine stopped.
)
echo.
pause
exit /b %EXITCODE%

:setup_failed
echo.
echo [ERROR] First-time setup failed.
echo Check the messages above. Internet access may be required on first launch.
echo.
pause
exit /b 1
