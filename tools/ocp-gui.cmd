@echo off
setlocal
rem A GUI-launched opencode inherits PyInstaller's _PYI_*/_MEIPASS2 env vars.
rem Reusing that stale _MEI dir makes the exe fail with
rem "Failed to load Python DLL ...\python312.dll" (or a missing init.tcl).
rem Clear them so the GUI always opens from any parent process.
rem NOTE: keep this file ASCII-only; cmd.exe reads it as CP936 and
rem UTF-8 Chinese comments can swallow line breaks.
set "_MEIPASS2="
set "TCL_LIBRARY="
set "TK_LIBRARY="
for /f "tokens=1 delims==" %%v in ('set _PYI_ 2^>nul') do set "%%v="
set "root=%~dp0.."
if exist "%root%\dist\opencode-anywhere.exe" (
    start "" "%root%\dist\opencode-anywhere.exe"
    exit /b
)
start "" pythonw "%~dp0ocp-gui.py"
