"""
dz_host.py - Android-side launcher for an UNEDITED copy of DropZone.py.

Why this file exists
--------------------
DropZone.py was written to be run as a script (python3 DropZone.py). Inside an
APK three of its assumptions do not hold, and all three are plain module-level
variables that DropZone reads at call time, so they can be replaced from here
without editing DropZone.py itself:

  LOG_FILE    - defaults to "next to the script", which is read-only in an APK.
  UPLOAD_DIR  - defaults to a system temp folder; we want the app's own storage.
  LOCAL_IP    - captured once at import; the phone may change WiFi between runs.

The Java side (ServerService) calls start() and stop(). Nothing here runs at
import time.
"""

import shutil
import tempfile
import threading
from pathlib import Path

# Module state. The embedded Python interpreter lives as long as the app
# process, so these survive a Stop followed by a Start.
_dz = None        # the imported DropZone module
_server = None    # the running HTTPServer, or None when stopped
_thread = None    # thread running serve_forever()
_upload_dir = None


def start(files_dir):
    """Start the DropZone server. Returns [remote_url, core_version].

    files_dir: the app's private files folder (Context.getFilesDir()).
    Raises on failure (e.g. port 7070 already in use); Java shows the message.
    """
    global _dz, _server, _thread, _upload_dir

    base = Path(files_dir)

    if _server is None:
        # Temp files (DropZone uses tempfile at import and may use it for zips)
        # go to app-private storage, NOT the Android cache folder: the system
        # is allowed to purge the cache folder when the phone runs low on space.
        tmp_dir = base / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(tmp_dir)

        if _dz is None:
            import DropZone as dz      # import creates one empty temp folder
            _dz = dz
            shutil.rmtree(dz.UPLOAD_DIR, ignore_errors=True)   # the empty one it just made
            _patch_header_encoding(dz)

        # --- the three overrides -------------------------------------------
        _dz.LOG_FILE = base / "DropZone.log"

        # Uploaded files live here while the server runs. Wiped on start (in
        # case the app was killed last time) and on stop, matching DropZone's
        # own "Ctrl+C cleans up temp files" behaviour.
        _upload_dir = base / "uploads"
        shutil.rmtree(_upload_dir, ignore_errors=True)
        _upload_dir.mkdir(parents=True, exist_ok=True)
        _dz.UPLOAD_DIR = _upload_dir

        _dz.LOCAL_IP = _dz.get_local_ip()
        # --------------------------------------------------------------------

        # Same one-file log roll DropZone does in its own __main__ block.
        try:
            if _dz.LOG_FILE.exists() and _dz.LOG_FILE.stat().st_size > 5 * 1024 * 1024:
                _dz.LOG_FILE.replace(_dz.LOG_FILE.with_suffix(".log.1"))
        except OSError:
            pass

        _dz.log(f"=== DropZone v{_dz.__version__} ({_dz.VERSION_STAGE}) starting inside the Android app ===")

        server = _dz.make_server()     # raises OSError if the port is taken
        thread = threading.Thread(target=_serve, args=(server,), name="dropzone-server", daemon=True)
        thread.start()
        _server, _thread = server, thread

    return [f"http://{_dz.LOCAL_IP}:{_dz.PORT}/remote", _dz.__version__]


def _patch_header_encoding(dz):
    """Stop single-file downloads crashing on names like "Daikon (40's).mp3"
    written with a curly apostrophe, or names containing an emoji.

    Cause (DropZone.py v0.2.14, /api/download): the raw filename is placed in
    the Content-Disposition header, and Python's http.server can only send
    header text that fits the latin-1 character set, so any other character
    raises UnicodeEncodeError and the download dies. The zip path is unaffected
    because its header only carries the user name.

    Fix applied here, outside DropZone.py: characters that cannot be sent are
    replaced by "?" in the header. The browser still saves the correct name,
    because DropZone also sends the percent-encoded form (filename*=UTF-8''...)
    which is pure ASCII and takes priority.
    """
    original_send_header = dz.Handler.send_header

    def send_header(self, keyword, value):
        if isinstance(value, str):
            value = value.encode("latin-1", "replace").decode("latin-1")
        return original_send_header(self, keyword, value)

    dz.Handler.send_header = send_header


def _serve(server):
    """Body of the server thread: run until stop() calls server.shutdown()."""
    try:
        server.serve_forever()
    except Exception:
        import traceback
        _dz.log(f"FATAL in serve_forever:\n{traceback.format_exc()}")


def stop():
    """Stop the server, forget the shared-file list, delete the uploads."""
    global _server, _thread

    if _server is None:
        return
    _server.shutdown()        # asks serve_forever() to return (waits for it)
    _server.server_close()    # releases port 7070 so the next start can bind it
    _thread.join(timeout=5)
    _server, _thread = None, None

    # The interpreter outlives this stop, so DropZone's in-memory lists would
    # otherwise still name files that are about to be deleted.
    with _dz.state_lock:
        _dz.shared_files.clear()
        _dz.users.clear()
    shutil.rmtree(_upload_dir, ignore_errors=True)
    _dz.log("=== DropZone stopped (app) ===")
