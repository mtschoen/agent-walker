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

REM Warn only if INSTALL_DIR isn't on the PERSISTED PATH (User or Machine).
REM Deliberately NOT a grep of this session's %PATH%: after the user adds the
REM dir (via :path_note or the GUI), this cmd session's %PATH% stays stale until
REM a new terminal opens, so a %PATH% check would warn on every re-run even
REM though the dir is permanently installed. The persisted PATH is also exactly
REM what fresh processes -- the recency-nudge hook, the status line -- will see.
powershell -NoProfile -Command "$d=('%INSTALL_DIR%').TrimEnd([char]92); $raw=(@([Environment]::GetEnvironmentVariable('Path','User'),[Environment]::GetEnvironmentVariable('Path','Machine')) -join ';'); if ((($raw -split ';' | ForEach-Object { $_.Trim().TrimEnd([char]92) }) -icontains $d)) { exit 0 } else { exit 1 }"
if errorlevel 1 call :path_note

popd
endlocal
exit /b 0

:path_note
REM Do NOT suggest `setx PATH "%PATH%;..."` here. At a cmd prompt %PATH% is the
REM merged system+user PATH, so setx (no /M) writes the whole thing into the
REM *user* PATH (duplicating every system entry) and silently truncates at 1024
REM chars, dropping the tail. The PowerShell user-scope SetEnvironmentVariable
REM below reads only the user PATH and has no length cap.
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
