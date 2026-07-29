@echo off
REM ============================================================
REM PLC Agent - Start Server
REM 
REM Starts the Gradio UI on port 7860, accessible from network.
REM Press Ctrl+C to stop.
REM ============================================================

echo ============================================
echo   PLC Agent - Starting Server
echo   
echo   UI will be available at:
echo     Local:   http://127.0.0.1:7860
echo     Network: http://YOUR_SERVER_IP:7860
echo   
echo   Press Ctrl+C to stop.
echo ============================================
echo.

call venv\Scripts\activate.bat
python run.py ui
