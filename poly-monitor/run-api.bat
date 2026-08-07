@echo off
set ROOT=%~dp0
set PYTHONPATH=%ROOT%backend;%ROOT%
set FETCH_REAL_ROOT=%ROOT%..\fetch_real
cd /d %ROOT%backend
"%ROOT%..\venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
