@echo off
chcp 65001 >nul
title Media Classifier - Local Service
cd /d "%~dp0"

rem Use Python 3 from PATH, or the Windows py launcher.
where python >nul 2>nul
if %errorlevel%==0 goto RUN

where py >nul 2>nul
if %errorlevel%==0 goto RUNPY

echo.
echo [ERROR] Python 3 not found. Please install Python 3.9+ and retry.
echo.
pause
exit /b 1

:RUNPY
py -3 server.py
goto END

:RUN
python server.py

:END
echo.
echo Service stopped. Press any key to close.
pause >nul
