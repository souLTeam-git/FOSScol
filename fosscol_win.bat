@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo [1/4] Creating Python virtual environment...
    python -m venv venv
    
    echo [2/4] Installing dependencies from requirements.txt...
    venv\Scripts\python.exe -m pip install --upgrade pip
    venv\Scripts\python.exe -m pip install -r requirements.txt
    
    echo [3/4] Downloading Chromium browser for Playwright...
    venv\Scripts\playwright.exe install chromium

    echo [4/4] Creating FOSScol shortcuts on Desktop and Start Menu...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { $s = $ws.CreateShortcut(\"$dir\FOSScol.lnk\"); $s.TargetPath = '%~f0'; $s.WorkingDirectory = '%~dp0'; $s.IconLocation = '%~dp0assets\logo_fosscol.ico'; $s.Save() }"
)

start "" "venv\Scripts\pythonw.exe" main.py