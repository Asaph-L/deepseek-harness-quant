@echo off
REM DSHQuant 一键启动（Windows）：优先内置便携 Python（发布全环境包），否则系统 Python
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
