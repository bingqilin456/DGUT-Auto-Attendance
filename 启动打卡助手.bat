@echo off
chcp 65001 >nul
title 莞工自动打卡助手
cd /d "%~dp0"

echo ============================================
echo   莞工自动打卡助手
echo ============================================
echo.

rem ---- 1. 找 Python ----
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 Python。
    echo.
    echo 请先安装 Python 3.10 或更高版本：
    echo     https://www.python.org/downloads/
    echo 安装时务必勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

rem ---- 2. 补齐依赖（tkinter 是标准库，不用装）----
python -c "import requests, Crypto, tkinter" >nul 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装依赖（大约 1-2 分钟）...
    python -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
    echo 依赖安装完成。
    echo.
)

rem ---- 3. 定位不带控制台窗口的 pythonw.exe ----
set "PYW=pythonw"
for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%i"

echo 正在打开窗口...
start "" "%PYW%" "%~dp0gui.py"
exit /b 0
