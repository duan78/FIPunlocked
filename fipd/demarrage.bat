@echo off
rem demarrage.bat — FIPd v3 (pile officielle : service DirectOutputService requis)
cd /d %~dp0
where python >nul 2>nul || (echo Python introuvable dans le PATH & pause & exit /b 1)
python fipd3.py
pause
