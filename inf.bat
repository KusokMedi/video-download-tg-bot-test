@echo off
color 0A
:START
cls
echo =================================================
echo           Running your Python script...            
echo =================================================
echo.
python main.py
echo.
echo Script finished. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto START