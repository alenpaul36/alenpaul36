@echo off
rem Double-click to start Jarvis. Needs Python 3.10+ from python.org.
title Jarvis
cd /d "%~dp0"
rem Prefer the "py" launcher that python.org installs; "python" may be the Microsoft Store shortcut.
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if not defined PY python --version >nul 2>nul && set PY=python
if not defined PY (
  echo Python is not installed.
  echo Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then double-click this file again.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)
echo Installing what Jarvis needs ^(first time takes a minute^)...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installing failed. Check your internet connection, then try again.
  pause
  exit /b 1
)
echo Starting Jarvis... ^(keep this window open; closing it closes Jarvis^)
%PY% jarvis_gui.py
if errorlevel 1 (
  echo.
  echo Jarvis stopped with an error ^(shown above, also saved in jarvis-error.log^).
  pause
)
