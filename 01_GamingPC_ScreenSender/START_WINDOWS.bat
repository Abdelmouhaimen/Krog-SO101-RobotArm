@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run INSTALL_WINDOWS.bat first. & pause & exit /b 1)
".venv\Scripts\python.exe" -m lan_bridge.screen_app
if errorlevel 1 (echo App exited with an error. & pause)
