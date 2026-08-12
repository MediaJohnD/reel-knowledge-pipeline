@echo off
REM Reel Knowledge Pipeline - nightly vault organize
REM Normalizes note filenames and files them into the right vault subfolder.

setlocal enabledelayedexpansion
cd /d "C:\Users\media\Reel Knowledge Pipeline"

if not exist "data\logs" mkdir "data\logs"

for /f "tokens=2-4 delims=/ " %%a in ('date /t') do (set mydate=%%c-%%a-%%b)
for /f "tokens=1-2 delims=/:" %%a in ('time /t') do (set mytime=%%a-%%b)
set logfile=data\logs\organize-vault-%mydate%_%mytime%.log

uv run python -m reel_pipeline.cli organize-vault >> !logfile! 2>&1

if %ERRORLEVEL% NEQ 0 (
  echo Vault organize failed with exit code %ERRORLEVEL% >> !logfile!
  exit /b %ERRORLEVEL%
)
