@echo off
REM Reel Knowledge Pipeline - nightly vault organize
REM Normalizes note filenames and files them into the right vault subfolder,
REM then snapshots the vault to git.

setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "data\logs" mkdir "data\logs"

for /f "tokens=2-4 delims=/ " %%a in ('date /t') do (set mydate=%%c-%%a-%%b)
for /f "tokens=1-2 delims=/:" %%a in ('time /t') do (set mytime=%%a-%%b)
set logfile=data\logs\organize-vault-%mydate%_%mytime%.log

uv run python -m reel_pipeline.cli organize-vault >> !logfile! 2>&1
set organize_rc=!ERRORLEVEL!

if !organize_rc! NEQ 0 (
  echo Vault organize failed with exit code !organize_rc! >> !logfile!
)

REM Snapshot the vault to git whether or not the organize pass succeeded: the
REM day's notes are written by the worker, not by organize, so a failed organize
REM is the case where having a commit matters most. The task's exit code still
REM reports organize's result, not the snapshot's.
REM
REM The vault's location is NOT hardcoded here - this file is tracked in a public
REM repo. It comes from the pipeline's own config (paths.vault_dir, overridden by
REM REEL_VAULT_DIR in .env), and the repo root is whatever git says contains it,
REM since vault_dir points at a subfolder of the vault rather than its root.
set vaultlog=%~dp0!logfile!
set notesdir=
for /f "delims=" %%v in ('uv run python -c "from reel_pipeline.config import load_settings; print(load_settings().vault_dir)"') do set notesdir=%%v

if not defined notesdir (
  echo Could not resolve vault_dir from config - skipping vault snapshot >> "!vaultlog!"
  exit /b !organize_rc!
)

set vaultroot=
for /f "delims=" %%r in ('git -C "!notesdir!" rev-parse --show-toplevel 2^>nul') do set vaultroot=%%r

if not defined vaultroot (
  echo Vault is not a git repo - skipping vault snapshot >> "!vaultlog!"
  exit /b !organize_rc!
)

REM git reports a forward-slash path; cd wants backslashes.
set vaultroot=!vaultroot:/=\!

REM GIT_TERMINAL_PROMPT=0 makes a missing credential fail fast instead of hanging
REM this task overnight on a prompt nobody is awake to answer.
set GIT_TERMINAL_PROMPT=0
cd /d "!vaultroot!"
git add -A >> "!vaultlog!" 2>&1
git diff --cached --quiet
if errorlevel 1 (
  git commit -q -m "Vault snapshot !mydate!" >> "!vaultlog!" 2>&1
  git push -q >> "!vaultlog!" 2>&1
  if errorlevel 1 echo Vault push failed - commit is local only >> "!vaultlog!"
) else (
  echo No vault changes to commit >> "!vaultlog!"
)

exit /b !organize_rc!
