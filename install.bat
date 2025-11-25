@echo off
REM Skripts Python paketes installeshanas un bota starteshana
REM Script for installing Python packages and starting the bot

echo Installing required packages...
pip install -r requirements.txt

echo.
echo All packages installed successfully!
echo.
echo Starting the bot...
python main.py

pause
