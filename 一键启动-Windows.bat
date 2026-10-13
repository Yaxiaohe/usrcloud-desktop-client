@echo off
setlocal
cd /d "%~dp0"
title USRCloud Patrol Client

rem ============================================================
rem  有人云巡站客户端 (Electron 启动器，无需 Node.js/npm)
rem  This launcher does NOT need Node.js / npm.
rem  It runs the bundled Electron runtime directly.
rem ============================================================

set "EL=%~dp0node_modules\electron\dist\electron.exe"
set "LOGF=%~dp0launch-log.txt"

echo ============================================
echo   有人云巡站客户端
echo ============================================
echo.

rem Some shells set this; it turns electron.exe into plain Node and breaks startup.
set "ELECTRON_RUN_AS_NODE="

if not exist "%~dp0python\python.exe" (
  echo [!] Missing "python\python.exe".
  echo     Copy the "python" folder from your patrol tool directory
  echo     into THIS folder, then run this script again.
  echo.
  pause
  exit /b 1
)

if not exist "%EL%" (
  echo [!] Missing Electron runtime:
  echo     %EL%
  echo     Restore the "node_modules" folder next to this file.
  echo.
  pause
  exit /b 1
)

rem electron package needs path.txt to locate the binary; recreate it if missing.
if not exist "%~dp0node_modules\electron\path.txt" (
  > "%~dp0node_modules\electron\path.txt" echo electron.exe
)

echo [i] Starting the app...
echo     A log is written to:  launch-log.txt
echo     Keep this window open while the app is running.
echo.

"%EL%" "%~dp0." > "%LOGF%" 2>&1
set "RC=%ERRORLEVEL%"

echo.
echo ============================================
echo   The app has exited.  Exit code: %RC%
echo ============================================
echo.
if exist "%LOGF%" (
  echo ----- last lines of launch-log.txt -----
  powershell -NoProfile -Command "Get-Content -LiteralPath '%LOGF%' -Tail 25" 2>nul
  echo ----------------------------------------
)
echo.
echo If the window closed immediately, please send
echo launch-log.txt and main-log.txt for diagnosis.
echo.
pause
