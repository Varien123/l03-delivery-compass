@echo off
cd /d "%~dp0"
python auto_update.py >> auto_update.log 2>&1
