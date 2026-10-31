@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 @ 回复机器人 · 本地控制台 ...
python gui.py
pause
