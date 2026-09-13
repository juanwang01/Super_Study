@echo off
chcp 65001 >nul
REM ============================================
REM Probe-Plan-Teach 超级学习系统 启动脚本
REM 使用前：把下面三个配置改成你自己的
REM ============================================
set LEARN_LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
set LEARN_LLM_API_KEY=粘贴你的APIKey到这里
set LEARN_LLM_MODEL=doubao-seed-1-6-250615
set LEARN_HOST=0.0.0.0
set LEARN_PORT=8080

cd /d "%~dp0backend"
echo 正在启动超级学习系统，请稍候...
echo 浏览器打开: http://localhost:%LEARN_PORT%
python main.py
pause
