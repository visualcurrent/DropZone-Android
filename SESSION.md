# SESSION — DropZone-Android

> Read first, update last. Current-state block under 15 lines. It is a prompt, not a history.

## Current state
- M0 build gate PASSED: `build.bat` produces `DropZone-debug.apk` (version `0.1.00-dz0.2.14`). [verified: BUILD SUCCESSFUL twice 2026-11-11; APK listing shows `DropZone.pyc` + `dz_host.pyc`]
- M0 launch gate PASSED on the Android 15 emulator and on a real phone (Xiaomi 23116PN5BC, Android 16, arm64): Start brings the server up, `/`, `/remote`, `/api/state` return 200, service is foreground. [verified: adb + curl 2026-11-11]
- Emulator: Stop frees the port and deletes uploads, Start works again; 200 MB round trip byte-exact; memory flat 184 -> 188 MB across a 1 GB upload. [verified: sha256 match, `/proc/<pid>/status` samples]
- Phone over WiFi, screen off: 2.6 GB up (108 s) and down (514 s) byte-exact; app memory flat at 239 -> 247 MB. [verified: sha256 match, `/proc/<pid>/status`]
- Download runs at 3.4-5 MB/s against 24 MB/s for upload, the same with the screen on or off. [verified: 20 s samples] Cause not known. [assumed: WiFi link direction, no Termux comparison on this phone]
- Field report 1: single-file downloads failed for filenames with characters outside latin-1 (curly apostrophe, emoji); fixed in `dz_host.py`, wrapper 0.1.01. [verified: phone log tracebacks; fix verified on the PC with the APK's Python files, not yet on the phone]
- Field report 1: shared files not appearing until the page is reopened — cause unknown, nothing in the server log. [assumed: client-side]
- Wrapper 0.1.01 is installed on the phone `fc0f334f`; Gabe reports it "seems to be okay now". [verified: user report 2026-11-11] No change was made for the list problem, so treat it as not reproduced, not fixed. The emulator is shut down (its test device `dztest` remains in the toolchain folder).

## Open user questions
- None blocking. Wake lock + WiFi lock while the server runs were added without being asked for (purpose: transfers with the screen off); say if they should go. [assumed: wanted]

## Resume
- Next action: confirm the 0.1.01 filename fix on the phone; if the list-not-appearing problem recurs, read Chrome's console over USB while it happens (phone unlocked) before changing code. Then wait for Gabe's M1 field result. If downloads are slow there too, compare against Termux DropZone on the same phone and network before changing any code.
- Read: PLAN.md > M1 — Field acceptance on real phones.
- Pending gate: Gabe's M1 field run on the test phones — multi-GB up and down byte-exact with the screen off, one copy per download.
- Pending user action: `git init` in this folder, add in GitHub Desktop, commit (hand-off text in LOG.md top entry).
