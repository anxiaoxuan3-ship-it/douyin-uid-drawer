@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem   重新打包安装包（需要 Inno Setup 的 ISCC.exe）
rem
rem   用法：先把新的 UID看号器.exe 放进本目录，再双击本文件。
rem   产物：安装包\UID看号器-安装版-x.x.x.exe
rem ============================================================

if not exist "UID看号器.exe" (
  echo [错误] 本目录里没有 UID看号器.exe，先打包主程序再来。
  pause
  exit /b 1
)

set "ISCC="
if exist "D:\DHC\.inn\Inno\ISCC.exe" set "ISCC=D:\DHC\.inn\Inno\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not defined ISCC (
  echo [错误] 没找到 Inno Setup 的 ISCC.exe
  echo        装一个 Inno Setup 6 即可：https://jrsoftware.org/isdl.php
  pause
  exit /b 1
)

echo 正在编译安装包...
"%ISCC%" "安装包\安装包.iss"
if errorlevel 1 (
  echo.
  echo [错误] 编译失败，看上面的报错。
  pause
  exit /b 1
)

echo.
echo 完成！产物在 安装包\ 目录里。
explorer "安装包"
pause