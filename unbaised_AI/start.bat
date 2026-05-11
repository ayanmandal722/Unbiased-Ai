@echo off
title BiasModel v2.5 Launcher
echo ====================================================
echo   BIASMODEL V2.5 - FULL STACK STARTER
echo ====================================================
echo.

:: 1. Start Backend in a new window
echo [1/2] Launching Backend (FastAPI) on port 8000...
start "BiasModel Backend" cmd /k ".\venv\Scripts\python.exe main.py"

:: Give the backend a moment to start
timeout /t 3 /nobreak > nul

:: 2. Start Frontend in a new window
echo [2/2] Launching Frontend (Vite) on port 5173...
start "BiasModel Frontend" cmd /k "cd frontend && npm run dev"

echo.
echo ----------------------------------------------------
echo APPLICATIONS STARTED
echo ----------------------------------------------------
echo Backend: http://localhost:8000
echo Frontend: http://localhost:5173
echo.
echo NOTE: Ensure Jan AI is running at 127.0.0.1:1337
echo.
echo Close the newly opened terminal windows to stop the servers.
pause
