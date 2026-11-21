@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  挑一个 Python：
rem    1) 系统里装的（兼容性最好，推荐）
rem    2) 随程序分发的便携版 ..\python\python.exe
rem ============================================================
set "PY="
for %%P in (python.exe) do set "PY=%%~$PATH:P"

if not defined PY (
  if exist "%~dp0..\python\python.exe" set "PY=%~dp0..\python\python.exe"
)
if not defined PY goto NOPY

echo ============================================================
echo   抖音弹幕直连桥接
echo ------------------------------------------------------------
echo   用你自己的浏览器去抓抖音弹幕，再转发给 UID 抽号器。
echo   签名和设备风控都由真实浏览器负责，所以最不容易失效。
echo.
echo   直接双击本文件    就随便找个在播房间（没人说话会自动换）
echo   想抓自己的直播间  在抽号器里填好直播间号再点按钮，
echo                     或者在这里执行：本文件.bat 你的直播间号
echo.
echo   推荐做法：填上你自己的直播间号，弹幕最多也最准。
echo ============================================================
echo.
echo 使用的 Python：%PY%
echo.

"%PY%" "%~dp0douyin_bridge.py" %*
echo.
echo 桥接已退出。
pause
exit /b 0

:NOPY
echo.
echo   [错误] 没找到可用的 Python。
echo   正常应该有随包的便携版： %~dp0..\python\python.exe
echo   如果缺了，请重新解压一份完整的程序包；
echo   或者自己装一个： https://www.python.org/downloads/
echo   （安装时勾选 Add python.exe to PATH）
echo.
pause
exit /b 1
