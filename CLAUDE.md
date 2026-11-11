# Project: DropZone-Android — sideloadable Android app that runs DropZone without Termux

Doc tier: 2

## Goal
An APK that a host installs by sideloading; it runs the unedited DropZone server inside the app so guests on the same WiFi use it from a browser exactly as with the Termux host. A correct result: on Gabe's test phones, multi-GB uploads and downloads are byte-exact, the server survives the screen turning off, and each file downloads once.

## Environment
- Feeds from the sibling project `PROJECTS\DropZone`: `app\build.gradle` copies `..\DropZone\DropZone.py` into `app\src\main\python\` on every build when it differs. The DropZone folder is read-only to this project.
- `dz_host.py` adapts DropZone to Android by replacing three module variables after import (`LOG_FILE`, `UPLOAD_DIR`, `LOCAL_IP`); `DropZone.py` itself is unedited.
- Build: `build.bat` (Windows). Toolchain is one portable folder, `D:\AndroidToolchain` (JDK 17, Gradle 8.14.3, Android SDK 35, emulator); no system PATH or registry changes. Removal procedure: `UNINSTALL.md`.
- Versions: Android Gradle plugin 8.13.2, Chaquopy 17.2.0 (embeds Python 3.13; the PC's own Python 3.13 is needed at build time), minSdk 26 (Android 8), targetSdk 35, ABIs arm64-v8a + x86_64.
- App id `com.visualcurrent.dropzone`. On the phone: files in the app's private `files\uploads`, log at `files\DropZone.log` (read with `adb shell run-as com.visualcurrent.dropzone cat files/DropZone.log`).
- Use `C:\adb\adb.exe` for device work. Mixing it with the toolchain's own `adb.exe` restarts the adb server and drops connections.

## Settled decisions (dated, by Gabe)
- 2026-11-11 — Separate project from DropZone with its own rules; nothing in the DropZone directory is touched. Copies may be taken and edited here.
- 2026-11-11 — The host page opens in the phone's browser, not inside the app.
- 2026-11-11 — Sideload (debug-signed) APK is the target for now.
- 2026-11-11 — The agent deletes nothing on the PC it did not create; removal candidates are handed over as a list (`UNINSTALL.md`).

## Do-not-re-derive (verified findings)
- The newest Android command-line tools (`commandlinetools-win-16111833`) only bootstrap a separate "Android CLI" download, which failed here with a socket-permission error; release `13114758` contains the classic `sdkmanager` and works. [verified: both tried 2026-11-11]
- DropZone reads `LOG_FILE`, `UPLOAD_DIR` and `LOCAL_IP` at call time, so overriding them after `import DropZone` is enough; no core edit is needed. [verified: emulator + phone run 2026-11-11, log and uploads land in the app's `files` folder]
- Foreground-service type `connectedDevice` with `CHANGE_WIFI_STATE` starts cleanly on Android 15 (emulator) and Android 16 (phone). [verified: `dumpsys activity services` shows `isForeground=true types=0x10` on both]

## Read order
Root CLAUDE.md (auto) -> this file -> SESSION.md -> PLAN.md. Others only when pointed to.
