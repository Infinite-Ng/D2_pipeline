@echo off
REM 一次性环境准备：在 D:\codingSpace\D2_agent 下双击运行即可
cd /d "%~dp0"

echo === 创建虚拟环境 .venv ===
python -m venv .venv || goto :err

echo === 安装依赖 ===
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :err
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err

echo === 安装 Playwright 浏览器内核 ===
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo [提示] 下载内核失败（常见于公司代理）。脚本会自动改用你本机已装的 Chrome，
  echo        无需处理，直接继续下一步即可。
)

echo.
echo === 完成。下一步运行探测脚本： ===
echo    .venv\Scripts\python.exe probe_d2.py
pause
exit /b 0

:err
echo.
echo [失败] 上一步出错，请把屏幕内容发给我。
pause
exit /b 1
