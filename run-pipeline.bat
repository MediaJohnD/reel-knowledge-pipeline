@echo off
REM Reel Knowledge Pipeline Runner
REM Runs the pipeline once, logs output to a timestamped file

setlocal enabledelayedexpansion
cd /d "C:\Users\media\Reel Knowledge Pipeline"

REM Create logs directory if it doesn't exist
if not exist "data\logs" mkdir "data\logs"

REM Run pipeline and log output with timestamp
for /f "tokens=2-4 delims=/ " %%a in ('date /t') do (set mydate=%%c-%%a-%%b)
for /f "tokens=1-2 delims=/:" %%a in ('time /t') do (set mytime=%%a-%%b)
set logfile=data\logs\pipeline-%mydate%_%mytime%.log

uv run python -m reel_pipeline.cli run-once >> !logfile! 2>&1

if %ERRORLEVEL% NEQ 0 (
  echo Pipeline failed with exit code %ERRORLEVEL% >> !logfile!
  exit /b %ERRORLEVEL%
)
