@echo off
setlocal EnableExtensions EnableDelayedExpansion
set ROOT=%~dp0
set PYTHONPATH=%ROOT%backend;%ROOT%
set FETCH_REAL_ROOT=%ROOT%..\fetch_real

REM Load poly-monitor\.env into this process (uvicorn --reload children inherit it)
if exist "%ROOT%.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%.env") do (
    if not "%%A"=="" if not "%%B"=="" (
      set "%%A=%%B"
    )
  )
)

cd /d "%ROOT%backend"
"%ROOT%..\venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --reload-dir "%ROOT%backend"
endlocal
