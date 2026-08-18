@echo off
REM DSHQuant harness 运行时安装脚本（git clone 用户使用；Release zip 已自带 node_modules）
REM 需要：Node.js 18+（https://nodejs.org）
cd /d "%~dp0"
echo [1/2] 安装 DeepSeek HARNESS 运行时依赖（约 530 包 / 250MB，需几分钟）...
call npm install --no-audit --no-fund
if errorlevel 1 (
  echo 安装失败：请确认 Node.js 已安装且 npm 可用
  pause
  exit /b 1
)
echo [2/2] 完成。启动方式：运行仓库根 launcher.py，或手动：
echo   set DSH_HOME=%~dp0home
echo   node node_modules\@deepseek-ai\dsh\lib\bin.js web
echo.
echo 首次使用请配置 API Key：复制 home\.credentials.yaml.example 为 home\.credentials.yaml 并填入 DeepSeek API Key
pause
