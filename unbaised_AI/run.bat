@echo off
setlocal EnableDelayedExpansion
title BiasModel v2.5 - Auto Setup and Launch

echo.
echo ============================================================
echo   BiasModel v2.5 -- Auto Setup and Launch
echo ============================================================
echo.

:: Make sure we are in the project folder (same folder as this bat file)
cd /d "%~dp0"

:: ============================================================
:: STEP 1: Check Python
:: ============================================================
echo [1/6] Checking Python...
python --version >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Python not found.
    echo Please install Python 3.10+ from https://python.org
    echo Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)
python --version
echo.

:: ============================================================
:: STEP 2: Check Node.js
:: ============================================================
echo [2/6] Checking Node.js...
node --version >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Node.js not found.
    echo Please install Node.js from https://nodejs.org
    echo.
    pause
    exit /b 1
)
node --version
echo.

:: ============================================================
:: STEP 3: Create virtual environment
:: ============================================================
echo [3/6] Setting up Python virtual environment...
if not exist "venv\Scripts\activate.bat" (
    echo Creating venv...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo venv created successfully.
) else (
    echo venv already exists, skipping.
)
echo.

:: ============================================================
:: STEP 4: Install Python packages
:: ============================================================
echo [4/6] Installing Python packages...
venv\Scripts\python.exe -c "import fastapi" >nul 2>nul
if %errorlevel% neq 0 (
    echo Installing from requirements.txt...
    venv\Scripts\python.exe -m pip install --upgrade pip -q
    venv\Scripts\python.exe -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo ERROR: pip install failed.
        pause
        exit /b 1
    )
    echo Packages installed successfully.
) else (
    echo Packages already installed, skipping.
)
echo.

:: ============================================================
:: STEP 5: Install npm packages
:: ============================================================
echo [5/6] Setting up frontend...
if not exist "frontend\node_modules" (
    echo Running npm install...
    cd frontend
    call npm install
    if %errorlevel% neq 0 (
        cd ..
        echo ERROR: npm install failed.
        pause
        exit /b 1
    )
    cd ..
    echo npm packages installed.
) else (
    echo node_modules already exists, skipping.
)
echo.

:: ============================================================
:: STEP 6: Setup .env file
:: ============================================================
echo [6/6] Checking environment file...
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
    ) else (
        echo GEMINI_API_KEY=your_gemini_api_key_here > .env
        echo GOOGLE_CSE_ID=your_google_cse_id_here >> .env
    )
    echo.
    echo ============================================================
    echo   ACTION REQUIRED: Add your GEMINI_API_KEY to the .env file
    echo ============================================================
    echo.
    echo Opening .env in Notepad -- save it before continuing.
    echo.
    pause
    notepad .env
    echo.
    echo Press any key once you have saved your API key...
    pause >nul
) else (
    findstr /i "your_gemini_api_key_here" ".env" >nul 2>nul
    if %errorlevel% equ 0 (
        echo WARNING: .env still has placeholder key. Gemini features may fail.
    ) else (
        echo .env is configured.
    )
)
echo.

:: ============================================================
:: LAUNCH SERVERS
:: ============================================================
echo ============================================================
echo   All setup complete! Starting servers...
echo ============================================================
echo.
echo   Backend  --  http://localhost:8000
echo   Frontend --  http://localhost:5173
echo.
echo   NOTE: Make sure Jan AI is running at http://127.0.0.1:1337
echo.

:: Start backend
start "BiasModel Backend (port 8000)" cmd /k "cd /d "%~dp0" && venv\Scripts\python.exe main.py"

:: Wait for backend to start
timeout /t 3 /nobreak >nul

:: Start frontend
start "BiasModel Frontend (port 5173)" cmd /k "cd /d "%~dp0\frontend" && npm run dev"

:: Wait then open browser
timeout /t 5 /nobreak >nul
start "" http://localhost:5173

echo.
echo Both servers are running in their own windows.
echo Close those windows to stop the servers.
echo.
pause
