@echo off
REM DSHQuant launcher: bundled Python first, else system Python
cd /d "%~dp0"
if exist "runtime\python\python.exe" (
  "runtime\python\python.exe" launcher.py
  goto :end
)
if exist "portable-py\Scripts\python.exe" (
  "portable-py\Scripts\python.exe" launcher.py
  goto :end
)
python launcher.py
:end
pause