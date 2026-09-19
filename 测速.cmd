@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY (
  echo [错误] 没有找到 Python，请先安装 Python 3.8+ 并勾选 "Add python.exe to PATH"
  echo         下载地址: https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% "%~dp0wpyw-speedtest.py" %*
set "RC=%ERRORLEVEL%"
rem 双击运行时（无参数）保留窗口看结果
if "%~1"=="" pause
exit /b %RC%
