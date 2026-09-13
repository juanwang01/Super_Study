@echo off
title 超级学习系统
cd /d "%~dp0backend"
netstat -ano | findstr ":8080 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
  echo [超级学习系统] 已在运行，正在打开浏览器...
) else (
  echo [超级学习系统] 正在启动服务，请稍候...
  start "SuperStudy" cmd /c "python main.py"
)
set /a n=0
:wait
netstat -ano | findstr ":8080 " | findstr "LISTENING" >nul
if %errorlevel%==0 goto open
set /a n+=1
if %n% GEQ 40 goto open
timeout /t 1 /nobreak >nul
goto wait
:open
start "" http://127.0.0.1:8080
exit
