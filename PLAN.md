# PLAN — DropZone-Android

Ordered milestones. Each has a gate: pass criterion, evidence source, run command. Record decisions inline.

## M0 — Sideloadable debug APK
Wrap an unedited copy of `DropZone.py` in an Android app: embedded Python (Chaquopy), a foreground service that runs the server, and one screen with Start/Stop, the remote address, and an "Open host page" button that launches the phone's browser. Sign-off: Gabe, 2026-11-11 (answers recorded in `CLAUDE.md` > Settled decisions).
**Gate (build):** `build.bat` exits 0 and produces `DropZone-debug.apk` containing `DropZone.py` — evidence: build output + APK listing — run: `build.bat`.
**Gate (launch):** installed on a device or emulator, Start brings the server up and `GET /` and `GET /remote` return 200 — evidence: `adb` + `curl` through a forwarded port.
**Status:** see `SESSION.md`.

## M1 — Field acceptance on real phones (user-run)
Same acceptance as DropZone's own goal, on the APK instead of Termux.
**Gate:** on each test phone, a multi-GB upload and download are byte-exact, the app does not freeze or get killed with the screen off, and each file downloads once — evidence: Gabe's field run, `DropZone.log` pulled from the app — run: manual steps in `SESSION.md` > Resume.
**Status:** not started (PENDING user).

## Backlog (unordered; Definition of Done each)
Status keys: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` dropped
- [ ] Release signing key (stable across machines) — DoD: APK signed with a project-owned keystore kept outside the toolchain folder; an update installs over the previous build.
- [ ] 32-bit phone support — DoD: only if a test phone refuses the APK as incompatible; confirm Chaquopy offers the ABI for the chosen Python version first.
- [ ] Host page inside the app (WebView) — DoD: file picking and downloads work in-app at multi-GB sizes. Deferred by decision 2026-11-11.
- [ ] Way to read `DropZone.log` from the phone without `adb` — DoD: a button shares or exports the log file.
