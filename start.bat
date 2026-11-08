@echo off
.\.venv\Scripts\python.exe scripts\identity_preflight.py
if errorlevel 1 (
  echo [Startup] Identity verification was not completed. Nothing else was loaded.
  exit /b 1
)
echo Booting VANTA Core...
.\.venv\Scripts\python.exe scripts\start_neo4j.py
if errorlevel 1 (
  echo [Startup] Neo4j failed. VANTA was not started to avoid split-brain memory.
  del /q "%LOCALAPPDATA%\VANTA\identity\startup_ticket.json" >nul 2>&1
  pause
  exit /b 1
)
.\.venv\Scripts\python.exe agent.py
del /q "%LOCALAPPDATA%\VANTA\identity\startup_ticket.json" >nul 2>&1
pause
