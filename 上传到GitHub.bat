@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   ============================================
echo    把本地改动传到 GitHub
echo   ============================================
echo.

rem 先找 PATH 里的 git；找不到就用 Git for Windows 的固定路径
set "GIT=git"
where git >nul 2>nul
if errorlevel 1 set "GIT=C:\Program Files\Git\cmd\git.exe"

"%GIT%" --version >nul 2>nul
if errorlevel 1 (
  echo   [错误] 没找到 git，请先安装 Git for Windows:
  echo          https://git-scm.com/download/win
  pause
  exit /b 1
)

"%GIT%" add -A

echo   即将上传的文件：
echo.
"%GIT%" diff --cached --name-only
echo.

rem 安全检查：本地配置和运行数据绝不能上传
"%GIT%" diff --cached --name-only | findstr /I /C:"config.json" | findstr /V /C:"config.example.json" >nul
if not errorlevel 1 (
  echo   [已拦截] 暂存区里有 config.json（含你的 API Key）。
  echo   先执行: git restore --staged config.json
  pause
  exit /b 1
)
"%GIT%" diff --cached --name-only | findstr /I /C:"data/" >nul
if not errorlevel 1 (
  echo   [已拦截] 暂存区里有 data/ 目录（含对话记录）。
  echo   先执行: git restore --staged data
  pause
  exit /b 1
)

"%GIT%" diff --cached --quiet
if errorlevel 1 (
  "%GIT%" commit -m "update"
) else (
  echo   没有新的改动需要提交。
)

echo.
echo   正在推送到 GitHub...
"%GIT%" push
if errorlevel 1 (
  echo.
  echo   [失败] 推送没成功。常见原因: 网络不通，或 GitHub 登录过期。
  echo   稍后重试一次即可，本地提交已经保住了。
  pause
  exit /b 1
)

echo.
echo   完成: https://github.com/van233666/agent-mini
echo.
pause
