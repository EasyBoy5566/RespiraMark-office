@echo off
chcp 65001 >nul
cd /d %~dp0
echo Starting RespiraMark Office server...
python server.py
pause
