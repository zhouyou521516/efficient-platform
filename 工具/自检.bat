@echo off
chcp 65001 >nul
title 高效交互平台 · 自检
cd /d "%~dp0.."

set "PY="
if exist "%~dp0..\python\python.exe" set "PY=%~dp0..\python\python.exe"
if not defined PY for %%I in ("%~dp0..\..\xnj\python311\python.exe") do if exist "%%~fI" set "PY=%%~fI"
if not defined PY for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PY=%%~$PATH:P"

if not defined PY (
    echo 没有找到 python.exe（应位于同盘 xnj\python311\）
    pause
    exit /b 1
)

"%PY%" -S "%~dp0自检.py"
echo.
pause
