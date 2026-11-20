@echo off
rem Double-click to start Snipr. Leave this window open while Snipr should be working.
cd /d "%~dp0"
title Snipr
python -m uv run snipr serve --open
echo.
echo Snipr has stopped. Press any key to close this window.
pause >nul
