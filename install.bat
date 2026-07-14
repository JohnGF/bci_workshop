@echo off
echo =================================================
echo 🧠 BCI Workshop: Native Windows Setup
echo =================================================
echo.

:: Check if uv is installed, if not download it
where uv >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [1/3] Installing 'uv' Python package manager...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

    :: Add uv to the current session path so the next command works immediately
    set PATH=%USERPROFILE%\.local\bin;%PATH%
) else (
    echo [1/3] 'uv' is already installed.
)

echo.
echo [2/3] Syncing Python dependencies...
uv sync

echo.
echo =================================================
echo ✅ Setup Complete!
echo =================================================
echo.
echo To launch the BCI Master Dashboard, run:
echo.
echo     uv run main.py
echo.
pause
