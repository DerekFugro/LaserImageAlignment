@echo off
rem LaserImageAlignment launcher.
rem
rem Two ways to build the environment, tried in this order:
rem
rem   uv   - if it is on PATH. uv fetches its OWN Python 3.12, so the machine
rem          does not need Python installed at all. This removes the single
rem          biggest setup hurdle: the bundled wheels are cp312, so on a box
rem          with 3.11 or 3.13 the offline install fails with an error that
rem          looks nothing like "wrong Python".
rem   venv - the original path: py -3.12, a venv, and pip. Used whenever uv is
rem          absent, so nothing that worked before stops working and uv is
rem          never a hard requirement.
rem
rem Either way, dependencies come from the bundled wheels\ folder when it is
rem present (no internet needed) and from PyPI when it is not.
setlocal
cd /d "%~dp0"

rem lia.ini holds THIS machine's paths and is NOT in git, so a fresh clone has
rem only the template. Seed it once, then never touch it again - it is the
rem user's file from here on, and git will not fight them over it.
if not exist lia.ini (
    if exist lia.ini.example (
        echo First run: creating lia.ini from lia.ini.example.
        echo EDIT lia.ini and set 'calibrations' to your AllCalibrations folder,
        echo or every run will be refused with "missing lever_arms".
        copy /y lia.ini.example lia.ini >nul
    )
)

where uv >nul 2>&1 && goto :uv

rem ---------------------------------------------------------------- venv path
rem A .venv is built for ONE machine: pyvenv.cfg and the activate scripts carry
rem absolute paths written when it was created. Copy the app folder to another
rem machine (or zip it up and send it) and that venv is dead - but it still
rem EXISTS, so the "if not exist .venv" below would skip creating a good one
rem and go straight to activating a broken one. The error that produces names a
rem drive letter that means nothing on this machine, which reads like a bug in
rem the app rather than what it is.
rem
rem So: ask the venv's own Python to start. If it cannot, the venv is not usable
rem here - throw it away and let the normal first-run path rebuild it. This also
rem covers a half-finished install and one left behind by a Python upgrade.
if exist .venv (
    .venv\Scripts\python.exe -c "" >nul 2>&1
    if errorlevel 1 (
        echo Existing .venv does not work on this machine - rebuilding it...
        rmdir /s /q .venv
    )
)

if not exist .venv (
    echo First run: creating virtual environment...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create a Python virtual environment.
        echo Install Python 3.12 from python.org, or install uv - see README.
        pause
        exit /b 1
    )
    call .venv\Scripts\activate.bat
    if exist wheels\pyside6-6.11.2-cp310-abi3-win_amd64.whl (
        echo Installing dependencies OFFLINE from bundled wheels...
        pip install --no-index --find-links wheels -r requirements.txt
    ) else (
        echo Installing dependencies from PyPI...
        python -m pip install --upgrade pip
        pip install -r requirements.txt
    )
    if errorlevel 1 (
        echo ERROR: dependency install failed. See messages above.
        echo If the offline install failed, check that wheels\ holds every .whl
        echo and that this is Python 3.12 - the wheels are built for 3.12 only.
        pause
        exit /b 1
    )
) else (
    call .venv\Scripts\activate.bat
)
python app.py
if errorlevel 1 pause
goto :eof

rem ------------------------------------------------------------------ uv path
:uv
echo Using uv.
rem Try the bundled wheels first so a machine with no internet still works.
rem This can fail for a reason that is NOT missing wheels - uv also has to have
rem a Python 3.12 already on hand, and fetching one needs the network - so the
rem failure is silenced and we simply fall through to the online sync.
if exist wheels\pyside6-6.11.2-cp310-abi3-win_amd64.whl (
    uv sync --offline --no-index --find-links wheels 2>nul || uv sync
) else (
    uv sync
)
if errorlevel 1 (
    echo ERROR: uv could not build the environment. See messages above.
    pause
    exit /b 1
)
uv run app.py
if errorlevel 1 pause
goto :eof
