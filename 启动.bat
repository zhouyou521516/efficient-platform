@echo off
chcp 65001 >nul
title 高效交互平台
cd /d "%~dp0"

rem ---------- 运行时优先级：本目录 python\ → 同盘 xnj\python311\ → 系统 PATH ----------

set "PYW="
if exist "%~dp0python\pythonw.exe" set "PYW=%~dp0python\pythonw.exe"
if not defined PYW for %%I in ("%~dp0..\xnj\python311\pythonw.exe") do if exist "%%~fI" set "PYW=%%~fI"
if not defined PYW for %%P in (pythonw.exe) do if not "%%~$PATH:P"=="" set "PYW=%%~$PATH:P"

if not defined PYW (
    echo 没有找到 pythonw.exe。
    echo 请先安装 Python 3.11+（安装时勾选 Add python.exe to PATH），或把本目录放到 xnj\python311 同一磁盘根目录下。
    pause
    exit /b 1
)

rem -S：跳过 site 初始化，避免第三方 .pth 干扰（本程序只用标准库）
start "" "%PYW%" -S "%~dp0高效交互平台.py"
