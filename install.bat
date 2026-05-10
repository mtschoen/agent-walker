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

REM Warn if install dir isn't on PATH.
echo %PATH% | findstr /I /C:"%INSTALL_DIR%" >nul
if errorlevel 1 (
    echo.
    echo Note: %INSTALL_DIR% is not on PATH. Add it before the recency-nudge
    echo hook or status line can find claude-walker by name. To add permanently:
    echo   setx PATH "%%PATH%%;%INSTALL_DIR%"
)

popd
endlocal
exit /b 0

:smoke_failed
echo smoke test FAILED
popd
endlocal
exit /b 1

:error
popd
endlocal
exit /b 1
