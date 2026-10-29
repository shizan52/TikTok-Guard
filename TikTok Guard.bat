@echo off
rem Double-click to open the TikTok Guard desktop app (the same as: python app.py).
rem This small console window stays open while the app runs; closing the app's window stops it.
cd /d "%~dp0"
title TikTok Guard
python app.py %*
if errorlevel 1 (
  echo.
  echo The app stopped with an error. Is Python installed and on the PATH?
  pause
)
