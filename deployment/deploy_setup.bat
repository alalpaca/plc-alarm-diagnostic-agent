@echo off
REM ============================================================
REM PLC Agent - Windows Server Deployment Script
REM 
REM Usage: Copy this script to the server, run it once to set up,
REM        then use start_server.bat to start the service.
REM ============================================================

echo ============================================
echo   PLC Agent - Server Setup
echo ============================================
echo.

REM Step 1: Clone or copy project
if not exist "PLC" (
    echo [1/5] Cloning project...
    echo NOTE: If you don't have a git repo, manually copy the PLC folder here.
    echo       Then re-run this script.
    pause
    exit /b 1
) else (
    echo [1/5] Project folder found.
)

cd PLC

REM Step 2: Create virtual environment
if not exist "venv" (
    echo [2/5] Creating virtual environment...
    python -m venv venv
) else (
    echo [2/5] Virtual environment already exists.
)

REM Step 3: Install dependencies
echo [3/5] Installing dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt
pip install langgraph-checkpoint-sqlite

REM Step 4: Check .env
if not exist ".env" (
    echo [4/5] Creating .env from template...
    copy .env.example .env
    echo.
    echo *** IMPORTANT: Edit .env and fill in your OPENAI_API_KEY ***
    echo     Open: %CD%\.env
    echo.
    pause
) else (
    echo [4/5] .env already configured.
)

REM Step 5: Test
echo [5/5] Running system test...
call venv\Scripts\python.exe run.py test

echo.
echo ============================================
echo   Setup Complete!
echo   
echo   To start the server, run:
echo     start_server.bat
echo ============================================
pause
