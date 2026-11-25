@echo off
rem HIMA ground station for Windows: double-click. Receives the drone's mission packages over the base Wi-Fi,
rem checks them, adds them to the archive and opens the dashboard. Closing this window stops the receiver.
rem Options: hima_start.py --help (e.g. --no-dashboard, --listen 0.0.0.0:8765)
chcp 65001 >nul
cd /d "%~dp0.."
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0hima_start.py" --station %*
    goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0hima_start.py" --station %*
    goto done
)
echo Python 3.10 or newer is not installed.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo or run:  winget install -e --id Python.Python.3.12
:done
echo.
pause
