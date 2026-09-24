@echo off
REM Reel Review Runner
REM Reviews new Reel notes once (research + verdict), logs to a timestamped file.
REM Never aborts the schedule: always exits 0.

setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "data\logs" mkdir "data\logs"

for /f "tokens=2-4 delims=/ " %%a in ('date /t') do (set mydate=%%c-%%a-%%b)
for /f "tokens=1-2 delims=/:" %%a in ('time /t') do (set mytime=%%a-%%b)
set logfile=data\logs\review-%mydate%_%mytime%.log

uv run python scripts\review_new_reels.py --apply --limit 10 >> "!logfile!" 2>&1

if %ERRORLEVEL% NEQ 0 (
  echo Review failed with exit code %ERRORLEVEL% >> "!logfile!"
)
exit /b 0
