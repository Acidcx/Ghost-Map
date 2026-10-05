@echo off
rem Ghost Map launcher (runs from source). Double-click to start the web UI.
rem First run creates a .venv folder and installs dependencies (needs internet once).
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

echo First run: setting up Ghost Map, this takes a minute...
%PY% -m venv .venv || goto fail
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -e . || goto fail

:run
".venv\Scripts\python.exe" -m ghostmap web --open %*
if errorlevel 1 pause
goto :eof

:nopython
echo Python 3.10 or newer is not installed.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo or download GhostMap.exe instead (see README).
pause
exit /b 1

:fail
if exist ".venv" rmdir /s /q ".venv"
echo Setup failed - see the messages above.
pause
exit /b 1
