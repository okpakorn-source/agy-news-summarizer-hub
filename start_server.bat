@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title Antigravity News Summarizer Hub (เครื่องแม่รับงานอัตโนมัติ)
cd /d "%~dp0"
echo ====================================================================
echo   กำลังเปิดระบบรับงานข่าว Antigravity News Summarizer Hub บนคอมเครื่องแม่
echo   ลิงก์ส่งให้พนักงาน: https://agy-news-summarizer-hub.vercel.app
echo   หน้าจอควบคุมของคุณ: http://localhost:8080
echo ====================================================================
start "" "http://localhost:8080"
:loop
python run_hub.py
echo.
echo [ระบบหยุดทำงาน กำลังรีสตาร์ทอัตโนมัติใน 5 วินาที... กด Ctrl+C เพื่อปิดถาวร]
timeout /t 5
goto loop
