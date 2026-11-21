@echo off
rem Double-click to start Jarvis. Needs Python 3.10+ from python.org (tick "Add python.exe to PATH").
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup. & pause & exit /b 1)
echo Installing what Jarvis needs (first time only)...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)
start "" pythonw jarvis_gui.py
