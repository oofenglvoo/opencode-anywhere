@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0oc-sync.ps1" in %*
