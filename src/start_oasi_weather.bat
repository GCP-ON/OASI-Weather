@echo off
setlocal EnableDelayedExpansion
cd /d C:\OASI-codes\OASI-Weather

set "PYTHON_EXE=C:\Users\Lazzaro\anaconda3\python.exe"
set "LOG_FILE=C:\OASI-codes\OASI-Weather\startup.log"

echo ============================================== >> "%LOG_FILE%"
echo Startup run at %date% %time% >> "%LOG_FILE%"

if not exist "%PYTHON_EXE%" (
	echo ERROR: Anaconda Python not found at %PYTHON_EXE% >> "%LOG_FILE%"
	exit /b 1
)

REM Start app and log output
echo Launching OASI-Weather dashboard... >> "%LOG_FILE%"
"%PYTHON_EXE%" -m src >> "%LOG_FILE%" 2>&1