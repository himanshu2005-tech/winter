@echo off
REM VANTA test suite. No external runner required; unittest ships with Python.
REM Nothing here touches Neo4j, the network, or the real vault, so it is safe
REM to run while VANTA itself is running.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo Running VANTA test suite...
echo.
set "PYTHONPATH=%CD%"
echo [1/2] Compiling project Python files...
"%PY%" -c "from pathlib import Path; import py_compile; skip={'.venv','dist','build','local_qwen_3b','vault'}; files=[p for p in Path('.').rglob('*.py') if not any(x in skip for x in p.parts)]; [py_compile.compile(str(p),doraise=True) for p in files]; print('Compiled',len(files),'files')"
if errorlevel 1 exit /b 1
echo.
echo [2/2] Running deterministic and mocked scenario tests...
"%PY%" -m unittest discover -s tests -v %*
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% EQU 0 (
    echo All tests passed.
) else (
    echo TESTS FAILED. Fix before shipping.
)
exit /b %EXITCODE%
