@echo off
setlocal
set "root=%~dp0.."
if exist "%root%\dist\opencode-anywhere.exe" (
    start "" "%root%\dist\opencode-anywhere.exe"
    exit /b
)
start "" pythonw "%~dp0ocp-gui.py"
