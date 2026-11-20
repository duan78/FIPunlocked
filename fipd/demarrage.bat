@echo off
rem =====================================================================
rem  demarrage.bat — fipd3 (pile officielle DirectOutputService), en un
rem  double-clic. L'ancien orchestrateur SPAD n'est plus necessaire.
rem  Prerequis : service DirectOutputService (start=auto, deja configure).
rem =====================================================================
cd /d "%~dp0"
python fipd3.py
pause
