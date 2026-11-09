@echo off
chcp 936 >nul
title USB 诊断与修复 - 高效交互平台
setlocal
set "HERE=%~dp0"
set "LOG=%HERE%..\usb-diag.log"
set "ADB=%HERE%..\scrcpy\adb.exe"

net session >nul 2>&1
if errorlevel 1 (
    echo 正在请求管理员权限 ...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

rem 停止 adb，避免占用接口
if exist "%ADB%" "%ADB%" kill-server >nul 2>&1

echo.
echo ============================================================
echo   USB 诊断与修复
echo   按 USB 接口协议特征识别 ADB 接口，不限品牌与型号
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%USB诊断修复.ps1" -LogPath "%LOG%"

echo.
echo [等待设备重新枚举]
timeout /t 10 /nobreak >nul

echo.
echo ============================================================
echo   adb devices
echo ============================================================
if exist "%ADB%" "%ADB%" devices -l

echo.
echo 详细日志: %LOG%
echo.
echo 列表为空   - 请拔线重插，再运行本文件一次
echo unauthorized - 请在手机屏幕上点「允许 USB 调试」
echo.
pause
