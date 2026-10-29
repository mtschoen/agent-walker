@echo off
REM Install the native walker, its claude-walker.exe alias, and the stdlib-only
REM session-search CLI at %USERPROFILE%\.local\bin.
REM
REM Usage: install.bat
setlocal

set "SCRIPT_DIR=%~dp0"
if not "%~1"=="" (
    echo install.bat: unknown argument: %~1
    echo Usage: install.bat
    endlocal
    exit /b 2
)

pushd "%~dp0"

REM Build whichever impl this host has a toolchain for. C++ is the production
REM binary and is tried first; Go and Rust are conformance-equal fallbacks for
REM hosts without a C++ toolchain. This is the Windows counterpart of
REM install.sh's build_cpp/build_go/build_rust chain. Each :build_* routine sets
REM WALKER_BIN on success and leaves it empty when its toolchain is absent or
REM its build fails, so the caller falls through to the next impl.
set "WALKER_BIN="
set "BUILT_VIA="
call :build_cpp
if not defined WALKER_BIN call :build_go
if not defined WALKER_BIN call :build_rust
if not defined WALKER_BIN (
    echo install.bat: no usable toolchain to build the walker.
    echo              install one of: cmake + a C++ compiler, or go, or cargo + a linker.
    goto :error
)
echo built walker via %BUILT_VIA%: %WALKER_BIN%
set "INSTALL_DIR=%USERPROFILE%\.local\bin"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
copy /Y "%WALKER_BIN%" "%INSTALL_DIR%\agent-walker.exe" >nul || goto :error
REM claude-walker.exe is a deprecated back-compat alias for existing callers
REM (progress-beacon hooks, statusline lookups). Remove once every consumer
REM has migrated to the agent-walker name.
copy /Y "%WALKER_BIN%" "%INSTALL_DIR%\claude-walker.exe" >nul || goto :error
echo installed %WALKER_BIN% -^> %INSTALL_DIR%\agent-walker.exe ^(alias: claude-walker.exe^)

REM Smoke test: bare-flag invocation routes to cost mode.
"%INSTALL_DIR%\agent-walker.exe" --period 86400 --win-start 0 >nul || goto :smoke_failed
echo smoke test ok

copy /Y "%SCRIPT_DIR%search\session_search.py" "%INSTALL_DIR%\agent-walker-search.py" >nul || goto :error
copy /Y "%SCRIPT_DIR%search\opencode_search.py" "%INSTALL_DIR%\opencode_search.py" >nul || goto :error
copy /Y "%SCRIPT_DIR%shared\process_safe.py" "%INSTALL_DIR%\process_safe.py" >nul || goto :error
copy /Y "%SCRIPT_DIR%search\agent-walker-search.bat" "%INSTALL_DIR%\agent-walker-search.bat" >nul || goto :error
REM Git Bash does not run a .bat by its extensionless name, so also install a POSIX
REM shim next to the .bat. The source is LF-only (see .gitattributes).
copy /Y "%SCRIPT_DIR%search\agent-walker-search.sh" "%INSTALL_DIR%\agent-walker-search" >nul || goto :error
echo installed session-search CLI -^> %INSTALL_DIR%\agent-walker-search.bat ^(Git Bash shim: agent-walker-search^)
call "%INSTALL_DIR%\agent-walker-search.bat" --help >nul || goto :smoke_failed
echo session-search smoke test ok
REM Smoke the Git Bash shim when Git for Windows is present. bash is located
REM beside git (not by a bare `where bash`, which can resolve to the WSL launcher);
REM without Git for Windows this step is skipped silently.
call :smoke_gitbash_shim || goto :smoke_failed


REM Warn only if INSTALL_DIR isn't on the PERSISTED PATH (User or Machine).
REM Deliberately NOT a grep of this session's %PATH%: after the user adds the
REM dir (via :path_note or the GUI), this cmd session's %PATH% stays stale until
REM a new terminal opens, so a %PATH% check would warn on every re-run even
REM though the dir is permanently installed. The persisted PATH is also exactly
REM what fresh processes, including the session-search skill, will see.
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
echo hook, status line, or session-search skill can find their commands by name.
echo To add it to your
echo User PATH safely (no setx 1024-char truncation, no system/user merge):
echo   powershell -NoProfile -Command "$p=[Environment]::GetEnvironmentVariable('Path','User'); if(-not $p){$p=''}; if(($p -split ';') -notcontains '%INSTALL_DIR%'){[Environment]::SetEnvironmentVariable('Path',($p.TrimEnd(';')+';%INSTALL_DIR%').TrimStart(';'),'User')}"
echo Or edit it via the GUI: rundll32 sysdm.cpl,EditEnvironmentVariables
echo Then open a new terminal for the change to take effect.
goto :eof

:smoke_gitbash_shim
set "GIT_BASH="
for /f "delims=" %%g in ('where git 2^>nul') do if not defined GIT_BASH if exist "%%~dpg..\bin\bash.exe" set "GIT_BASH=%%~dpg..\bin\bash.exe"
if not defined GIT_BASH exit /b 0
"%GIT_BASH%" "%INSTALL_DIR%\agent-walker-search" --help >nul || exit /b 1
echo git bash shim smoke test ok
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

REM ---------------------------------------------------------------------------
REM Builders. Each is called with no arguments, probes for its toolchain, and on
REM success sets WALKER_BIN (path to the built exe) and BUILT_VIA (impl name).
REM A missing toolchain or a failed build is deliberately NOT fatal here: the
REM routine returns with WALKER_BIN unset so the caller can try the next impl.
REM Only the caller, once every impl has been tried, treats that as an error.

:build_cpp
where cmake >nul 2>&1 || goto :eof
cmake -S cpp -B cpp\build -DCMAKE_BUILD_TYPE=Release || goto :eof
cmake --build cpp\build --config Release -j || goto :eof
for %%f in (cpp\build\Release\walker.exe cpp\build\walker.exe) do (
    if exist "%%f" (
        set "WALKER_BIN=%%f"
        set "BUILT_VIA=cpp"
        goto :eof
    )
)
echo install.bat: cmake reported success but no walker.exe exists under cpp\build\
goto :eof

:build_go
where go >nul 2>&1 || goto :eof
REM CGO off so the pure-Go linker is used -- no system C compiler required.
pushd go
set "CGO_ENABLED=0"
go build -o walker.exe .
set "GO_RC=%ERRORLEVEL%"
popd
if not "%GO_RC%"=="0" goto :eof
if exist "go\walker.exe" (
    set "WALKER_BIN=go\walker.exe"
    set "BUILT_VIA=go"
)
goto :eof

:build_rust
where cargo >nul 2>&1 || goto :eof
REM rustc shells out to a system linker (MSVC link.exe, or gcc under MinGW). If
REM none is installed cargo fails here and we fall through with WALKER_BIN unset.
pushd rust
cargo build --release
set "RUST_RC=%ERRORLEVEL%"
popd
if not "%RUST_RC%"=="0" goto :eof
if exist "rust\target\release\walker.exe" (
    set "WALKER_BIN=rust\target\release\walker.exe"
    set "BUILT_VIA=rust"
)
goto :eof
