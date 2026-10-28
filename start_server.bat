@echo off
chcp 65001 >nul
title Antigravity News Summarizer Hub (Host Server)
cd /d "%~dp0"
echo ===============================================================
echo   กำลังเปิดระบบสรุปเนื้อข่าว Antigravity News Summarizer Hub
echo   หน้าเว็บของคุณ: http://localhost:8080
echo ===============================================================
start "" "http://localhost:8080"
python run_hub.py
pause
