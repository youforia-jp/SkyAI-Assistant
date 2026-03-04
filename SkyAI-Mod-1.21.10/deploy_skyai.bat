@echo off
setlocal
title SkyAI Prism Deployer

:: --- CONFIGURATION ---
:: We use a wildcard (*) here so it doesn't matter if the version is 1.0.0 or 1.1.0
set MOD_WILDCARD=skyai-1.0.0.jar
set INSTANCE_PATH=C:\Users\juanp\AppData\Roaming\PrismLauncher\instances\Debug-SkyAI\minecraft\mods

echo [SkyAI] Starting Build...
echo.

:: Run the Gradle build
call .\gradlew build

:: Check if build was successful
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] BUILD FAILED. Check your Java code for typos.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [SkyAI] Build Successful. 
echo [SkyAI] Deploying to Prism Instance...

:: Create directory if it doesn't exist
if not exist "%INSTANCE_PATH%" mkdir "%INSTANCE_PATH%"

:: Using 'xcopy' or 'copy' with a wildcard to handle the versioning
:: This will copy any jar starting with 'SkyAI-' from the libs folder
copy /y "build\libs\%MOD_WILDCARD%" "%INSTANCE_PATH%\"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo [+] DEPLOY COMPLETE!
    echo [+] Target: %INSTANCE_PATH%
) else (
    echo.
    echo [!] DEPLOY FAILED. 
    echo [!] Check if build\libs is empty or if Minecraft is still open.
)

pause