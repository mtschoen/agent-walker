@echo off
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto use_python
py -3 "%~dp0agent-walker-search.py" %*
exit /b %ERRORLEVEL%

:use_python
python "%~dp0agent-walker-search.py" %*
exit /b %ERRORLEVEL%
