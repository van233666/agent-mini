@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo    agent-mini 自动化测试
echo ================================
echo.
".venv\Scripts\python.exe" -m pytest -v
echo.
echo ------------------------------------------------
echo  绿色 PASSED = 这条检查通过
echo  红色 FAILED = 这条检查没通过，要修
echo ------------------------------------------------
pause
