@echo off
rem HIMA launcher for Windows: double-click, or run "launcher\start.bat --fly" etc. (options: hima_start.py --help)
chcp 65001 >nul
cd /d "%~dp0.."
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0hima_start.py" %*
    goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0hima_start.py" %*
    goto done
)
echo Python 3.10 or newer is not installed.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo or run:  winget install -e --id Python.Python.3.12
:done
echo.
pause
