@echo off
REM VANTA without a console. Called by start_quiet.vbs, which runs this
REM hidden -- so nothing here may wait for input: a `pause` in a hidden window
REM blocks forever with nothing on screen to explain why.
REM
REM Everything the console would have shown goes to the log instead, because
REM the boot output is the only record of what VANTA did and losing it makes
REM every later question unanswerable.

cd /d "%~dp0"
if not exist "vault\logs" mkdir "vault\logs"

REM The log carries model output, song titles and filenames; cp1252 would
REM raise UnicodeEncodeError partway through and take the process with it.
set PYTHONIOENCODING=utf-8

echo ---------------------------------------------------------- >> "vault\logs\winter.log"
echo [%DATE% %TIME%] Waiting for identity verification... >> "vault\logs\winter.log"

REM Authentication is the lifecycle boundary.  Do not start Neo4j, import the
REM agent, load speech/vision models, or launch workers until the small local
REM face preflight has issued a one-use startup ticket.
.\.venv\Scripts\python.exe scripts\identity_preflight.py >> "vault\logs\winter.log" 2>&1
if errorlevel 1 (
  echo [%DATE% %TIME%] [Startup] Identity verification was not completed. Nothing else was loaded. >> "vault\logs\winter.log"
  exit /b 2
)

echo [%DATE% %TIME%] Identity verified. Booting VANTA Core... >> "vault\logs\winter.log"

.\.venv\Scripts\python.exe scripts\start_neo4j.py >> "vault\logs\winter.log" 2>&1
if errorlevel 1 (
  echo [%DATE% %TIME%] [Startup] Neo4j failed. VANTA was not started to avoid split-brain memory. >> "vault\logs\winter.log"
  del /q "%LOCALAPPDATA%\VANTA\identity\startup_ticket.json" >nul 2>&1
  exit /b 1
)

.\.venv\Scripts\pythonw.exe agent.py >> "vault\logs\winter.log" 2>&1
set "VANTA_EXIT_CODE=%ERRORLEVEL%"
del /q "%LOCALAPPDATA%\VANTA\identity\startup_ticket.json" >nul 2>&1
exit /b %VANTA_EXIT_CODE%
