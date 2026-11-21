@echo off
REM Xenosaga I Extractor — Windows double-click launcher.
REM Starts the local GUI on a free port and opens your browser.
REM Installs the kit's Python packages (requirements.txt: numpy, fonttools,
REM uharfbuzz) for this user the first time they are missing (or into a
REM kit-local .venv when that Python refuses user installs).

setlocal
cd /d "%~dp0"

REM Prefer the Windows Python launcher; fall back to plain `python` on PATH.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3 was not found on your PATH.
    echo Install Python 3.9 or newer from https://www.python.org/downloads/
    echo ^(make sure to tick "Add Python to PATH" during install^),
    echo then double-click this launcher again.
    pause
    exit /b 1
)

REM A kit-local virtual environment made by an earlier run (see below) wins.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import numpy, fontTools, uharfbuzz" >nul 2>nul
    if not errorlevel 1 (
        set "PY=.venv\Scripts\python.exe"
        goto run
    )
)
%PY% -c "import numpy, fontTools, uharfbuzz" >nul 2>nul
if not errorlevel 1 goto run
echo Installing the kit's Python packages ^(numpy, fonttools, uharfbuzz^) for this user...
%PY% -m pip install --user -r requirements.txt
if not errorlevel 1 goto run
REM A Python that refuses user installs ^(PEP 668 "externally-managed-environment",
REM e.g. an MSYS2 Python^) still works with a virtual environment in the kit folder.
echo pip could not install for this user. Installing into a private environment
echo in this folder instead ^(.venv^)...
%PY% -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
set "PY=.venv\Scripts\python.exe"
goto run

:failed
echo Could not install the packages in requirements.txt ^(pip needs internet access^).
echo To install by hand into the kit folder:
echo   %PY% -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
echo then double-click this launcher again.
pause
exit /b 1

:run
%PY% gui.py %*

REM If gui.py errored out, keep the window open so the user can see why.
if errorlevel 1 pause
