@echo off
rem ---------------------------------------------------------------------------
rem build.bat - build the sideloadable DropZone APK.   (Windows only)
rem
rem Everything the build needs lives in ONE folder, D:\AndroidToolchain, and is
rem pointed at with environment variables set for this window only (setlocal).
rem Nothing is added to the system PATH or the registry.
rem
rem FILE EFFECTS: writes app\build\ and .gradle\ inside this project, caches
rem downloads in D:\AndroidToolchain\gradle-home, and overwrites
rem DropZone-debug.apk in this folder with the fresh build.
rem
rem Extra arguments are passed to Gradle, e.g.:  build.bat --info
rem ---------------------------------------------------------------------------
setlocal

set "TOOLCHAIN=D:\AndroidToolchain"
set "JAVA_HOME=%TOOLCHAIN%\jdk\jdk-17.0.21+9"
set "ANDROID_HOME=%TOOLCHAIN%\sdk"
rem ANDROID_USER_HOME holds the auto-generated debug signing key.
set "ANDROID_USER_HOME=%TOOLCHAIN%\android-user-home"
rem GRADLE_USER_HOME holds every library Gradle downloads (default would be C:\Users\<you>\.gradle).
set "GRADLE_USER_HOME=%TOOLCHAIN%\gradle-home"

if not exist "%JAVA_HOME%\bin\java.exe" (
    echo ERROR: toolchain not found at "%TOOLCHAIN%". See UNINSTALL.md for what it contains.
    exit /b 1
)

call "%TOOLCHAIN%\gradle\gradle-8.14.3\bin\gradle.bat" -p "%~dp0." assembleDebug %*
if errorlevel 1 (
    echo BUILD FAILED
    exit /b 1
)

copy /Y "%~dp0app\build\outputs\apk\debug\app-debug.apk" "%~dp0DropZone-debug.apk" >nul
echo.
echo Built: "%~dp0DropZone-debug.apk"
endlocal
