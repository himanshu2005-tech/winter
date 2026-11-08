@echo off
setlocal
cd /d "%~dp0"
echo Running VANTA critical quality gate...
".venv\Scripts\python.exe" "scripts\quality_gate.py"
set "VANTA_GATE_EXIT=%ERRORLEVEL%"
if not "%VANTA_GATE_EXIT%"=="0" (
  echo.
  echo VANTA quality gate failed. See runtime\quality_gate.json.
) else (
  echo.
  echo VANTA quality gate passed.
)
exit /b %VANTA_GATE_EXIT%
