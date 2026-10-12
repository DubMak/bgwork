@echo off
rem Start the bgwork host + viewer. Usage: bgwork.cmd [--app C:\path\app.exe] [-- app args...]
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" -m bgwork host %*
