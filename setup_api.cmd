@echo off
setlocal enabledelayedexpansion
title DeepSeek API Setup
cd /d "%~dp0"

set "HP="

rem --- auto search: common places ---
if exist "harness\home\.credentials.yaml.example" set "HP=harness\home"
if exist "DSHQuant\harness\home\.credentials.yaml.example" set "HP=DSHQuant\harness\home"
if not defined HP if exist "%USERPROFILE%\Downloads\DSHQuant\harness\home\.credentials.yaml.example" set "HP=%USERPROFILE%\Downloads\DSHQuant\harness\home"
if not defined HP if exist "%USERPROFILE%\Desktop\DSHQuant\harness\home\.credentials.yaml.example" set "HP=%USERPROFILE%\Desktop\DSHQuant\harness\home"

rem --- auto search: one level of subfolders under current dir ---
if not defined HP for /d %%D in (*) do if exist "%%D\harness\home\.credentials.yaml.example" set "HP=%%D\harness\home"
if not defined HP for /d %%D in (*) do if exist "%%D\DSHQuant\harness\home\.credentials.yaml.example" set "HP=%%D\DSHQuant\harness\home"

if defined HP goto :found

rem --- ask user for path (accepts DSHQuant / harness / harness\home) ---
echo.
echo [i] HARNESS not found automatically.
echo Enter the path of the folder containing "home\.credentials.yaml.example".
echo You may paste either:
echo   C:\...\DSHQuant          or
echo   C:\...\DSHQuant\harness  or
echo   C:\...\DSHQuant\harness\home
echo.
set /p "UD=Path: "
if exist "!UD!\harness\home\.credentials.yaml.example" set "HP=!UD!\harness\home"
if not defined HP if exist "!UD!\home\.credentials.yaml.example" set "HP=!UD!\home"
if not defined HP if exist "!UD!\.credentials.yaml.example" set "HP=!UD!"

if defined HP goto :found

echo.
echo [ERROR] Still not found. Please check the path and run again.
echo.
pause
exit /b

:found
set "KEY="
set /p "KEY=DeepSeek API Key, starts with sk-: "
if not "!KEY:~0,3!"=="sk-" goto :badkey
> "!HP!\.credentials.yaml" echo DEEPSEEK_API_KEY: !KEY!

rem --- self verify ---
set "CHECK="
for /f "delims=" %%L in ('type "!HP!\.credentials.yaml" 2^>nul') do set "CHECK=%%L"
echo.
if "!CHECK!"=="DEEPSEEK_API_KEY: !KEY!" (
    echo [OK] API key saved and verified.
    echo Config: !HP!\.credentials.yaml
    echo Next: run python launcher.py, then open http://127.0.0.1:8787/control
) else (
    echo [ERROR] Write failed. Please check the path has write permission.
)
echo.
pause
exit /b

:badkey
echo.
echo [ERROR] Key must start with sk-.
echo.
pause
exit /b