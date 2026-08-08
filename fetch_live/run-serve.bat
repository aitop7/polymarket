@echo off
setlocal EnableExtensions
set ROOT=%~dp0
if exist "%ROOT%.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%.env") do (
    if not "%%A"=="" if not "%%B"=="" set "%%A=%%B"
  )
)
cd /d "%ROOT%"
"%ROOT%..\venv\Scripts\python.exe" serve.py
endlocal
