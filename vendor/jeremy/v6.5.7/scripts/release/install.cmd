@echo off
setlocal
cd /d "%~dp0"
title BlockDAG Pool Stack Installer
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "BDAG_EXIT=%ERRORLEVEL%"
echo.
if "%BDAG_EXIT%"=="0" (
  echo Installer finished successfully.
) else (
  echo Installer failed with exit code %BDAG_EXIT%.
)
echo.
if /i not "%BDAG_NO_PAUSE%"=="1" pause
exit /b %BDAG_EXIT%
