@echo off
REM Build the C++ walker and install as claude-walker.exe at %USERPROFILE%\.local\bin.
setlocal enabledelayedexpansion
pushd "%~dp0"

cmake -S cpp -B cpp\build -DCMAKE_BUILD_TYPE=Release || goto :error
cmake --build cpp\build --config Release -j || goto :error

set "WALKER_BIN="
for %%f in (cpp\build\Release\walker.exe cpp\build\walker.exe) do (
    if exist "%%f" (
        set "WALKER_BIN=%%f"
        goto :found
    )
)
echo install.bat: built walker.exe not found under cpp\build\
goto :error

:found
set "INSTALL_DIR=%USERPROFILE%\.local\bin"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
copy /Y "%WALKER_BIN%" "%INSTALL_DIR%\claude-walker.exe" >nul || goto :error
echo installed %WALKER_BIN% -^> %INSTALL_DIR%\claude-walker.exe

REM Smoke test: bare-flag invocation routes to cost mode.
"%INSTALL_DIR%\claude-walker.exe" --period 86400 --win-start 0 >nul || goto :smoke_failed
echo smoke test ok

REM Warn if install dir isn't on PATH. NOTE: do NOT suggest
REM `setx PATH "%PATH%;..."` here. At a cmd prompt %PATH% expands to the merged
REM system+user PATH; setx (no /M) writes the whole thing into the *user* PATH,
REM duplicating every system entry, and setx silently truncates at 1024 chars,
REM dropping the tail. Use the PowerShell user-scope SetEnvironmentVariable
REM method below instead -- it reads only the user PATH and has no length cap.
echo %PATH% | findstr /I /C:"%INSTALL_DIR%" >nul
if errorlevel 1 call :path_note

popd
endlocal
exit /b 0

:path_note
echo.
echo Note: %INSTALL_DIR% is not on PATH. Add it before the recency-nudge
echo hook or status line can find claude-walker by name. To add it to your
echo User PATH safely (no setx 1024-char truncation, no system/user merge):
echo   powershell -NoProfile -Command "$p=[Environment]::GetEnvironmentVariable('Path','User'); if(-not $p){$p=''}; if(($p -split ';') -notcontains '%INSTALL_DIR%'){[Environment]::SetEnvironmentVariable('Path',($p.TrimEnd(';')+';%INSTALL_DIR%').TrimStart(';'),'User')}"
echo Or edit it via the GUI: rundll32 sysdm.cpl,EditEnvironmentVariables
echo Then open a new terminal for the change to take effect.
goto :eof

:smoke_failed
echo smoke test FAILED
popd
endlocal
exit /b 1

:error
popd
endlocal
exit /b 1
