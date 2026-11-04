@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ---- Bypass the Windows system SOCKS proxy. ----
REM The LLM APIs (Zhipu / Volcano Ark) are domestic endpoints, direct connect works.
REM httpx-based SDKs crash on a system SOCKS proxy, so clear proxy vars here.
set NO_PROXY=*
set no_proxy=*

REM ---- Guard: refuse to start if another copy already listens on port 5000 ----
netstat -ano | find ":5000" | find /i "LISTENING" >nul
if not errorlevel 1 (
  echo ============================================
  echo   ERROR: port 5000 is already in use.
  echo   An OLD server window is still running.
  echo   Close ALL black terminal windows first,
  echo   then double-click this file again.
  echo ============================================
  pause
  exit /b 1
)

echo ============================================
echo   Research Wiki (YanBao Baike) starting...
echo.
echo   Open in browser:  http://127.0.0.1:5000
echo   To stop: close this window, or press Ctrl+C
echo ============================================
echo.

".venv\Scripts\python.exe" manage.py serve

echo.
echo Service stopped.
pause
