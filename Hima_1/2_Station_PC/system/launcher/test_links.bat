@echo off
rem HIMA link tests for Windows: double-click, or run "launcher\test_links.bat --no-hands" etc.
rem Every test of the links to the Pixhawk (USB, TELEM2 adapter, telemetry radio); report in runs\link_tests\
rem Options: launcher\test_links.bat --help
chcp 65001 >nul
cd /d "%~dp0.."
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0hima_start.py" --links %*
    goto done
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0hima_start.py" --links %*
    goto done
)
echo Python 3.10 or newer is not installed.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo or run:  winget install -e --id Python.Python.3.12
:done
echo.
pause
