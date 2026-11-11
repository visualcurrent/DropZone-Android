#!/usr/bin/env python3
"""
DropZone - LAN File Sharing Server
Run: python3 DropZone.py
Then open http://localhost:7070 in your browser
"""

import os, sys, json, uuid, socket, re, shutil, tempfile, mimetypes, io, zipfile, struct, zlib
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.parse
from email.utils import formatdate

# ─── State ────────────────────────────────────────────────────────────────────
users        = {}
shared_files = {}
UPLOAD_DIR   = Path(tempfile.mkdtemp(prefix="dropzone_"))
PORT         = 7070

# ── Version ──────────────────────────────────────────────────────────────────
# Format: release.beta.incremental (see docs/DEVELOPMENT.md).
__version__   = "0.2.14"        # running build
VERSION_STAGE = "nightly"       # release stage shown on the page: nightly | beta | stable

ADJECTIVES = ["Swift","Crimson","Golden","Silver","Azure","Jade","Amber","Coral","Indigo","Teal"]
ANIMALS    = ["Fox","Wolf","Hawk","Bear","Lynx","Deer","Crow","Orca","Ibis","Puma"]
import random
import threading, socketserver

# ── Transfer tuning ──────────────────────────────────────────────────────────
# At/below STREAM_THRESHOLD a download or zip is buffered in RAM (keeps an exact
# Content-Length so browsers show a progress bar). Above it, data is streamed
# from disk so peak memory stays bounded no matter how large the transfer is.
STREAM_THRESHOLD         = 64 * 1024 * 1024    # 64 MB
UPLOAD_CHUNK             = 4 * 1024 * 1024     # read uploads off the socket in 4 MB chunks (caps peak RAM/upload)
WARN_THRESHOLD           = 512 * 1024 * 1024   # 512 MB: confirm (size + ETA) first
MAX_CONCURRENT_TRANSFERS = 4                   # backstop on simultaneous heavy transfers
SPEEDTEST_BYTES          = 3 * 1024 * 1024     # sample the client times to estimate speed
THREADED                 = True                # set False to force single-threaded serving

SPEEDTEST_PAYLOAD = bytes(SPEEDTEST_BYTES)
transfer_sem = threading.BoundedSemaphore(MAX_CONCURRENT_TRANSFERS)
state_lock   = threading.Lock()                # guards users / shared_files mutation

# ── Live transfer registry ───────────────────────────────────────────────────
# Every heavy download/zip registers here so /api/state can report current
# activity to ALL pages (BACKLOG 6). Feedback rides the 1-per-2.5s state poll —
# a separate request — so a device sees progress even while its own download
# request is blocked waiting for a slot. Phases: queued -> building -> sending.
transfers      = {}                            # tid -> transfer dict
transfers_lock = threading.Lock()
_transfer_seq  = 0
def transfer_start(kind, name, size, owner_sid, owner_name):
    global _transfer_seq
    with transfers_lock:
        _transfer_seq += 1
        tid = _transfer_seq
        transfers[tid] = {"tid":tid,"kind":kind,"name":name,"size":size,
                          "phase":"queued","sid":owner_sid,"owner":owner_name}
    return tid
def transfer_set(tid, phase):
    with transfers_lock:
        if tid in transfers: transfers[tid]["phase"] = phase
def transfer_end(tid):
    with transfers_lock:
        transfers.pop(tid, None)
def transfers_public(viewer_sid):
    with transfers_lock:
        items = sorted(transfers.values(), key=lambda t: t["tid"])
    queued = [t for t in items if t["phase"] == "queued"]
    pos = {t["tid"]: i + 1 for i, t in enumerate(queued)}
    return [{"name":t["name"], "size_str":fmt_size(t["size"]), "phase":t["phase"],
             "owner":t["owner"], "mine":bool(viewer_sid) and t["sid"] == viewer_sid,
             "pos":pos.get(t["tid"]), "queued_total":len(queued)} for t in items]

# ── Diagnostics ──────────────────────────────────────────────────────────────
# Instrumentation for the upload-freeze investigation (BACKLOG item 1).
# DEBUG controls console echo. log() ALWAYS appends to a file beside the script
# (flushed per line) so records survive a Termux crash. rss_mb() reports
# resident memory, to see whether the whole upload body is held in RAM.
import time, traceback
DEBUG    = True
LOG_FILE = Path(__file__).resolve().parent / "DropZone.log"
_log_lock = threading.Lock()
def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    if DEBUG:
        print(line, flush=True)
    try:
        with _log_lock, open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
def rss_mb():
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0   # KB -> MB
    except OSError:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return -1.0

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

LOCAL_IP = get_local_ip()

def gen_name(): return random.choice(ADJECTIVES)+random.choice(ANIMALS)+str(random.randint(10,99))

def fmt_size(b):
    for u in ['B','KB','MB','GB']:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def parse_range(header, size):
    """Parse a single HTTP byte-range request for a file of `size` bytes.

    Returns (start, end) inclusive for a satisfiable range, ("unsatisfiable",
    None) when it names only bytes at/after EOF (caller replies 416), or
    (None, None) when there is no range or it must be ignored — absent header,
    malformed, or multi-range — in which case the caller serves a normal 200
    (RFC 7233: an unparsable Range MUST be ignored). This is the fix for iOS
    Safari, which probes a download with a Range request and then re-fetches if
    the server answers a plain 200; supporting 206 makes the probe and the save
    resolve to one download instead of an OS-suffixed duplicate.
    """
    if not header:
        return (None, None)
    header = header.strip()
    if not header.lower().startswith("bytes=") or "," in header:
        return (None, None)                       # not a byte range / multi-range
    spec = header[6:].strip()
    if "-" not in spec:
        return (None, None)
    lo, _, hi = spec.partition("-")
    lo, hi = lo.strip(), hi.strip()
    try:
        if lo == "":                              # suffix range: final N bytes
            n = int(hi)
            if n <= 0:
                return (None, None)
            start, end = max(0, size - n), size - 1
        else:
            start = int(lo)
            if size == 0 or start >= size:
                return ("unsatisfiable", None)     # start at/after EOF -> 416
            end = int(hi) if hi != "" else size - 1
            if end >= size:
                end = size - 1
            if end < start:
                return (None, None)
    except ValueError:
        return (None, None)
    if size == 0 or start >= size:
        return ("unsatisfiable", None)
    return (start, end)

def get_or_create_user(sid):
    with state_lock:
        if sid not in users:
            users[sid] = {"name": gen_name(), "files": []}
        return users[sid]

def user_display(sid, fallback):
    "Display name for a requester (their chosen name, else their IP)."
    with state_lock:
        u = users.get(sid)
    return u["name"] if u else fallback

def dedup_name(seen, name):
    "Return a unique archive name within `seen`, suffixing ' (n)' on clashes."
    if name in seen:
        seen[name] += 1
        stem, dot, ext = name.rpartition(".")
        return f"{stem} ({seen[name]}){dot}{ext}" if dot else f"{name} ({seen[name]})"
    seen[name] = 0
    return name

# ─── Multipart parser ─────────────────────────────────────────────────────────
def parse_multipart(data, boundary):
    if isinstance(boundary, str): boundary = boundary.encode()
    parts = []
    for seg in data.split(b"--" + boundary)[1:]:
        if seg.startswith(b"--"): break
        seg = seg.lstrip(b"\r\n")
        if b"\r\n\r\n" not in seg: continue
        hdr_raw, body = seg.split(b"\r\n\r\n", 1)
        body = body.rstrip(b"\r\n")
        hdrs = {}
        for line in hdr_raw.decode(errors="replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        parts.append((hdrs, body))
    return parts

# ─── Streaming multipart parser (bounded memory) ──────────────────────────────
# Reads a multipart/form-data body straight from the socket in fixed-size chunks
# and writes each file part directly to disk, so peak memory stays flat no matter
# how large the upload is (BACKLOG item 1). Never holds the whole body — or a
# whole part — in RAM. Peak buffer ~= one chunk + the boundary length.
def stream_multipart(rfile, boundary, length, open_dest, chunk=UPLOAD_CHUNK):
    """Parse a multipart body read from rfile.

    boundary  : boundary token (str or bytes) without leading dashes.
    length    : Content-Length — bytes available to read from rfile.
    open_dest : callable(headers) -> a writable binary file for a file part.
    Yields (headers, dest_or_None, nbytes) per part. For file parts dest is the
    object open_dest returned (already written, not yet closed); for non-file
    form fields dest is None and the bytes are discarded (this app uses none).
    """
    if isinstance(boundary, str):
        boundary = boundary.encode()
    delim = b"--" + boundary
    sep   = b"\r\n" + delim          # data is terminated by CRLF + boundary
    buf   = bytearray()
    remaining = [length]

    def fill(minbytes):
        while len(buf) < minbytes and remaining[0] > 0:
            data = rfile.read(min(chunk, remaining[0]))
            if not data:
                remaining[0] = 0
                break
            remaining[0] -= len(data)
            buf.extend(data)
        return len(buf) >= minbytes

    def read_line():
        # One line without its trailing CRLF (bytes), or None at end of stream.
        while b"\r\n" not in buf:
            if not fill(len(buf) + 1):
                if not buf: return None
                line = bytes(buf); buf.clear(); return line
        i = buf.index(b"\r\n")
        line = bytes(buf[:i]); del buf[:i+2]; return line

    def stream_body(dest):
        # Write body bytes to dest until the next `sep`. Return (cont, nbytes)
        # where cont is b"\r\n" (another part follows) or b"--" (end of body).
        n = 0
        while True:
            i = buf.find(sep)
            if i >= 0:
                if dest is not None: dest.write(buf[:i])
                n += i
                del buf[:i+len(sep)]
                while len(buf) < 2:
                    if not fill(2): break
                cont = bytes(buf[:2]); del buf[:2]
                return cont, n
            keep = len(sep) - 1               # a `sep` might straddle the tail
            if len(buf) > keep:
                out = len(buf) - keep
                if dest is not None: dest.write(buf[:out])
                n += out
                del buf[:out]
            if not fill(len(buf) + 1):         # stream ended with no closing sep
                if buf:
                    if dest is not None: dest.write(buf)
                    n += len(buf); buf.clear()
                return b"--", n

    first = read_line()                        # body opens with the bare delim
    if first is None or delim not in first or first.rstrip().endswith(b"--"):
        return
    while True:
        headers = {}
        while True:
            line = read_line()
            if not line: break                 # None or blank line ends headers
            s = line.decode("utf-8", "replace")  # filenames are UTF-8 (RFC 7578)
            if ":" in s:
                k, v = s.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if "filename" in headers.get("content-disposition", ""):
            dest = open_dest(headers)
            cont, n = stream_body(dest)
            yield headers, dest, n
        else:
            cont, n = stream_body(None)        # non-file field: discard
            yield headers, None, n
        if cont != b"\r\n":
            break

def get_filename(cd):
    for pat in [r'filename\*=UTF-8\'\'(.+)', r'filename="([^"]+)"', r'filename=([^\s;]+)']:
        m = re.search(pat, cd)
        if m: return urllib.parse.unquote(m.group(1))
    return "upload"

# ─── Streaming ZIP (STORE, bounded memory, exact size) ────────────────────────
# Writes a STORE (no-compression) zip straight to a write() callback as it reads
# each source file, so the download starts immediately (no spool to a temp file)
# and peak memory stays flat (BACKLOG 13). The layout is *uniformly* zip64, so
# the exact archive size is a simple closed form — we can send a real
# Content-Length and the browser shows true progress. Per file:
#   local(30)+name+localextra(20)+data+datadesc(24)+central(46)+name+centralextra(28)
# End: zip64-EOCD(56)+locator(20)+EOCD(22). Verified byte-exact vs the emitter
# across ascii/space/unicode names and past the 4 GiB boundary; archives validate
# under Python zipfile and Info-ZIP unzip.
def zip_stream_size(entries):
    "Exact byte length of the archive zip_stream() will emit. entries: (arc, path, size)."
    total = 0
    for arc, _p, size in entries:
        n = len(arc.encode("utf-8"))
        total += 30 + n + 20 + size + 24 + 46 + n + 28
    return total + 56 + 20 + 22

def _dos_dt(ts=None):
    t = time.localtime(ts if ts is not None else time.time())
    d  = ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return d, tm

def zip_stream(entries, write, chunk=1024*1024):
    "Emit the archive to write(bytes), reading each file in bounded chunks."
    dt, tm = _dos_dt()
    offset = 0
    central = []
    def w(b):
        nonlocal offset
        write(b); offset += len(b)
    for arc, path, size in entries:
        name  = arc.encode("utf-8")
        flags = 0x0008 | (0x0800 if any(c > 127 for c in name) else 0)  # data-desc + utf8
        hoff  = offset
        w(struct.pack("<IHHHHHIIIHH",
            0x04034b50, 45, flags, 0, tm, dt, 0, 0xFFFFFFFF, 0xFFFFFFFF, len(name), 20))
        w(name)
        w(struct.pack("<HHQQ", 0x0001, 16, size, size))          # local zip64 extra
        crc = 0
        with open(path, "rb") as fh:
            while True:
                b = fh.read(chunk)
                if not b: break
                crc = zlib.crc32(b, crc)
                w(b)
        crc &= 0xFFFFFFFF
        w(struct.pack("<IIQQ", 0x08074b50, crc, size, size))     # zip64 data descriptor
        central.append((name, flags, crc, size, hoff))
    cd_off = offset
    for name, flags, crc, size, hoff in central:
        w(struct.pack("<IHHHHHHIIIHHHHHII",
            0x02014b50, 45, 45, flags, 0, tm, dt, crc,
            0xFFFFFFFF, 0xFFFFFFFF, len(name), 28, 0, 0, 0, 0, 0xFFFFFFFF))
        w(name)
        w(struct.pack("<HHQQQ", 0x0001, 24, size, size, hoff))   # central zip64 extra
    cd_size = offset - cd_off
    n = len(central)
    w(struct.pack("<IQHHIIQQQQ", 0x06064b50, 44, 45, 45, 0, 0, n, n, cd_size, cd_off))
    w(struct.pack("<IIQI", 0x07064b50, 0, cd_off + cd_size, 1))
    w(struct.pack("<IHHHHIIH", 0x06054b50, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF,
                  0xFFFFFFFF, 0xFFFFFFFF, 0))
    return offset

# ─── HTTP Handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        # No caching for the app shell: the HTML embeds all CSS/JS, so a cached
        # page silently keeps running an OLD client across version bumps (that is
        # why the v0.2.11 download-debounce never ran on the phone — it was still
        # executing the pre-0.2.11 cached page). Always serve the current UI.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def get_session(self):
        for part in self.headers.get("Cookie","").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k.strip() == "session": return v.strip()
        return ""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def _zip_headers(self, zipname, length):
        fsafe = urllib.parse.quote(zipname)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{zipname}"; filename*=UTF-8\'\'{fsafe}')
        self.send_header("Content-Length", length)
        self.end_headers()

    def stream_zip(self, entries, zipname):
        # entries: list of (arcname, src_path, size). ZIP_STORED = wrap only, no
        # compression: files are usually already compressed, and storing is far
        # faster on a phone and keeps the archive size predictable.
        # Re-stat each source now, so the advertised Content-Length and the bytes
        # we stream derive from the same current file sizes (and drop any file
        # that vanished since the list was built).
        entries = [(arc, path, os.path.getsize(path))
                   for arc, path, _sz in entries if os.path.exists(path)]
        total  = sum(sz for _, _, sz in entries)
        zsize  = zip_stream_size(entries)
        client = self.client_address[0]
        sid    = self.get_session()
        log(f"zip requested: {zipname} <- {len(entries)} file(s), {fmt_size(total)} (from {client})")
        tid = transfer_start("zip", zipname, total, sid, user_display(sid, client))
        try:
            with transfer_sem:
                # Streamed straight to the socket — no temp file, first byte goes
                # out immediately, peak memory ~ one chunk (BACKLOG 13).
                self._zip_headers(zipname, zsize)
                transfer_set(tid, "sending")
                log(f"zip sending (streamed): {zipname} {fmt_size(zsize)}")
                try:
                    zip_stream(entries, self.wfile.write)
                    log(f"zip done: {zipname}")
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    log(f"zip cancelled by {client}: {zipname}")
                except (FileNotFoundError, OSError) as e:
                    # A source vanished mid-stream: headers are already sent, so
                    # we can only drop the connection. Rare (temp files are stable).
                    log(f"zip aborted ({zipname}): {e!r}")
        finally:
            transfer_end(tid)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/host"):  self.send_html(HOST_PAGE);   return
        if path == "/remote":       self.send_html(REMOTE_PAGE); return

        if path == "/api/state":
            sid  = self.get_session()
            user = get_or_create_user(sid) if sid else {"name":"?","files":[]}
            with state_lock:
                all_users = [
                    {"id": sid2, "name": u["name"], "files": u["files"], "is_me": sid2 == sid}
                    for sid2, u in users.items() if u["files"]
                ]
            self.send_json({"session":sid,"user":user,"all_users":all_users,
                            "transfers":transfers_public(sid),
                            "local_ip":LOCAL_IP,"port":PORT,
                            "warn_bytes":WARN_THRESHOLD})
            return

        if path == "/api/speedtest":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", len(SPEEDTEST_PAYLOAD))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(SPEEDTEST_PAYLOAD)
            return

        if path == "/api/download":
            fid = qs.get("id",[""])[0]
            if fid not in shared_files: self.send_json({"error":"Not found"},404); return
            f  = shared_files[fid]
            fp = Path(f["tmp_path"])
            if not fp.exists(): self.send_json({"error":"File missing"},404); return
            st       = fp.stat()
            size     = st.st_size
            etag     = f'"{int(st.st_mtime):x}-{size:x}"'
            lastmod  = formatdate(st.st_mtime, usegmt=True)
            fsafe    = urllib.parse.quote(f["name"])
            ctype    = mimetypes.guess_type(f["name"])[0] or "application/octet-stream"
            cdisp    = f'attachment; filename="{f["name"]}"; filename*=UTF-8\'\'{fsafe}'

            # Range support (BACKLOG 10). If-Range gates the range: on a validator
            # mismatch — or a malformed/multi-range header — we fall through to a
            # normal 200 so the client always gets a coherent whole file.
            start, end = parse_range(self.headers.get("Range"), size)
            if_range = self.headers.get("If-Range")
            if start is not None and start != "unsatisfiable" and if_range \
               and if_range.strip() not in (etag, lastmod):
                start = end = None                     # validator changed → full body

            if start == "unsatisfiable":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", 0)
                self.end_headers()
                log(f"download 416 (range past EOF) [{fid}] from {self.client_address[0]}")
                return

            is_partial = start is not None
            if is_partial:
                span = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", span)
                label = f"[{fid}] bytes {start}-{end}/{size} (partial)"
            else:
                start, span = 0, size
                self.send_response(200)
                self.send_header("Content-Length", size)
                label = f"[{fid}]"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", cdisp)
            # Deliberately NOT advertising Accept-Ranges: it made Chrome do a
            # start/cancel/re-segment dance that duplicated small files (BACKLOG
            # 10). We still HONOR an explicit Range request with 206 below (Safari,
            # resume); we just don't invite segmentation on the plain 200.
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", lastmod)
            self.end_headers()
            log(f"download: '{f['name']}' {label} {fmt_size(span)} to {self.client_address[0]}")

            # Small spans (incl. a Safari probe): one buffered slice write — fast,
            # exact, and it never consumes a heavy-transfer slot.
            if span <= STREAM_THRESHOLD:
                try:
                    with open(fp, "rb") as fh:
                        if start: fh.seek(start)
                        self.wfile.write(fh.read(span))
                    log(f"download done: '{f['name']}' [{fid}]")
                except (BrokenPipeError, ConnectionResetError):
                    log(f"download cancelled by {self.client_address[0]}: '{f['name']}' [{fid}]")
                return

            # Large span → stream the slice from disk under the transfer registry.
            sid = self.get_session()
            tid = transfer_start("file", f["name"], span, sid,
                                 user_display(sid, self.client_address[0]))
            try:
                with transfer_sem, open(fp, "rb") as fh:
                    transfer_set(tid, "sending")
                    fh.seek(start)
                    remaining = span
                    while remaining > 0:
                        chunk = fh.read(min(256 * 1024, remaining))
                        if not chunk: break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                log(f"download done: '{f['name']}' [{fid}]")
            except (BrokenPipeError, ConnectionResetError):
                log(f"download cancelled by {self.client_address[0]}: '{f['name']}' [{fid}]")
            finally:
                transfer_end(tid)
            return

        if path == "/api/download_all":
            uid = qs.get("user",[""])[0]
            with state_lock:
                if uid not in users or not users[uid]["files"]:
                    self.send_json({"error":"No files"},404); return
                seen, entries = {}, []
                for f in users[uid]["files"]:
                    fp = Path(f["tmp_path"])
                    if not fp.exists(): continue
                    entries.append((dedup_name(seen, f["name"]), str(fp), f.get("size", 0)))
                safe = re.sub(r'[^A-Za-z0-9._-]', '_', users[uid]["name"]) or "files"
            self.stream_zip(entries, f"DropZone-{safe}.zip")
            return

        if path == "/api/download_everything":
            me = self.get_session()
            with state_lock:
                owners = [(uid, u) for uid, u in users.items()
                          if u["files"] and uid != me]
                if not owners:
                    self.send_json({"error":"No files"},404); return
                entries, used_folders = [], {}
                for uid, u in owners:
                    folder = re.sub(r'[^A-Za-z0-9._ -]', '_', u["name"]).strip() or "user"
                    if folder in used_folders:
                        used_folders[folder] += 1
                        folder = f"{folder} ({used_folders[folder]})"
                    else:
                        used_folders[folder] = 0
                    seen = {}
                    for f in u["files"]:
                        fp = Path(f["tmp_path"])
                        if not fp.exists(): continue
                        arc = dedup_name(seen, f["name"])
                        entries.append((f"{folder}/{arc}", str(fp), f.get("size", 0)))
            self.stream_zip(entries, "DropZone-all-files.zip")
            return

    def do_POST(self):
        path   = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        sid    = self.get_session()

        if path == "/api/session":
            if not sid: sid = str(uuid.uuid4())[:8]
            user = get_or_create_user(sid)
            resp = json.dumps({"session":sid,"user":user}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",len(resp))
            self.send_header("Set-Cookie",f"session={sid}; Path=/; Max-Age=86400")
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            self.wfile.write(resp)
            return

        if path == "/api/upload":
            ct = self.headers.get("Content-Type","")
            if "multipart/form-data" not in ct:
                self.send_json({"error":"Expected multipart"},400); return
            m = re.search(r'boundary=(.+)', ct)
            if not m: self.send_json({"error":"No boundary"},400); return
            boundary = m.group(1).strip()
            log(f"upload start: Content-Length={length} ({fmt_size(length)}) "
                f"from {self.client_address[0]}; RSS={rss_mb():.1f} MB")
            if not sid: self.send_json({"error":"No session"},400); return
            user = get_or_create_user(sid)
            uploaded = []
            open_handles = {}   # id(fh) -> (fid, fname, tmp, fh)
            def open_dest(headers):
                fid  = str(uuid.uuid4())[:8]
                tmp  = UPLOAD_DIR / fid
                fh   = open(tmp, "wb")
                open_handles[id(fh)] = (fid, get_filename(headers.get("content-disposition","")), tmp, fh)
                return fh
            try:
                # Body is streamed to disk in chunks; peak RSS stays flat (item 1).
                for headers, dest, n in stream_multipart(self.rfile, boundary, length, open_dest):
                    if dest is None: continue          # non-file form field
                    fid, fname, tmp, fh = open_handles.pop(id(dest))
                    fh.close()
                    entry = {"id":fid,"name":fname,"tmp_path":str(tmp),
                             "size":n,"size_str":fmt_size(n)}
                    with state_lock:
                        user["files"].append(entry)
                        shared_files[fid] = {**entry,"owner_id":sid,"owner_name":user["name"]}
                    uploaded.append({"id":fid,"name":fname,"size_str":fmt_size(n)})
                    log(f"upload saved: '{fname}' [{fid}] {fmt_size(n)}; RSS={rss_mb():.1f} MB")
            except Exception as e:
                for _fid, _fn, _tmp, _fh in open_handles.values():
                    try: _fh.close()
                    except OSError: pass
                log(f"upload FAILED after {len(uploaded)} file(s): {e!r}; RSS={rss_mb():.1f} MB")
                try: self.send_json({"error":"Upload failed on server"},500)
                except OSError: pass
                return
            log(f"upload done: {len(uploaded)} file(s) saved; RSS={rss_mb():.1f} MB")
            self.send_json({"ok":True,"files":uploaded})
            return

        if path == "/api/rename":
            data = json.loads(self.rfile.read(length))
            with state_lock:
                if sid in users:
                    users[sid]["name"] = data.get("name", users[sid]["name"])[:30]
                    for f in shared_files.values():
                        if f["owner_id"] == sid: f["owner_name"] = users[sid]["name"]
                name = users.get(sid,{}).get("name","")
            self.send_json({"ok":True,"name":name})
            return

        if path == "/api/remove_file":
            data = json.loads(self.rfile.read(length))
            fid  = data.get("id","")
            with state_lock:
                if sid in users:
                    users[sid]["files"] = [f for f in users[sid]["files"] if f["id"] != fid]
                if fid in shared_files and shared_files[fid]["owner_id"] == sid:
                    try: Path(shared_files[fid]["tmp_path"]).unlink(missing_ok=True)
                    except OSError: pass
                    del shared_files[fid]
            self.send_json({"ok":True})
            return

        self.send_json({"error":"Not found"},404)


# ══════════════════════════════════════════════════════════════════════════════
#  FRONT-END
# ══════════════════════════════════════════════════════════════════════════════

CSS = """
:root,[data-theme="light"]{
  --bg:#f4f3ef; --surf:#f9f8f5; --surf2:#fdfcfa; --off:#edeae5;
  --border:#d4d1ca; --div:#dcd9d5;
  --txt:#28251d; --muted:#6b6a66; --hint:#7a7975;
  --pri:#01696f; --pri-h:#0c4e54; --pri-hl:rgba(1,105,111,.12);
  --err:#a12c7b; --err-bg:#fce8f4;
  --sh:0 2px 10px rgba(30,28,20,.09);
  --r:.5rem; --rx:.85rem; --rfull:9999px;
}
[data-theme="dark"]{
  --bg:#171614; --surf:#1c1b19; --surf2:#201f1d; --off:#27261f;
  --border:#383632; --div:#262422;
  --txt:#d0cfc9; --muted:#8a8880; --hint:#9e9c96;
  --pri:#4f98a3; --pri-h:#227f8b; --pri-hl:rgba(79,152,163,.14);
  --err:#d163a7; --err-bg:#2d1a28;
  --sh:0 2px 10px rgba(0,0,0,.35);
}
@media(prefers-color-scheme:dark){:root:not([data-theme]){
  --bg:#171614; --surf:#1c1b19; --surf2:#201f1d; --off:#27261f;
  --border:#383632; --div:#262422;
  --txt:#d0cfc9; --muted:#8a8880; --hint:#9e9c96;
  --pri:#4f98a3; --pri-h:#227f8b; --pri-hl:rgba(79,152,163,.14);
  --err:#d163a7; --err-bg:#2d1a28;
  --sh:0 2px 10px rgba(0,0,0,.35);
}}

*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:none;text-size-adjust:none}
body{min-height:100dvh;font-family:'Satoshi','Inter',system-ui,sans-serif;
  font-size:16px;color:var(--txt);background:var(--bg);line-height:1.5}
img,svg{display:block}
button,input,a{font:inherit;color:inherit}
button{cursor:pointer;background:none;border:none}

/* ── Header ── */
.hdr{
  background:var(--surf);border-bottom:1px solid var(--border);
  padding:10px 16px;
  display:flex;flex-direction:column;gap:3px;
  position:sticky;top:0;z-index:100;box-shadow:var(--sh);
}
.hdr-top{display:flex;align-items:center;gap:10px;width:100%}
.logo{display:flex;align-items:center;gap:8px;font-weight:700;
  font-size:1.05rem;color:var(--pri);text-decoration:none;flex-shrink:0}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:10px}

/* Tagline sits on its own line under the logo row */
.hdr-tagline{
  font-size:.75rem;color:var(--hint);line-height:1.3;
  padding-left:2px;
}

.badge{
  font-size:.68rem;padding:2px 8px;border-radius:var(--rfull);
  background:var(--pri-hl);color:var(--pri);font-weight:600;
  letter-spacing:.04em;flex-shrink:0;
}
.tt-btn{
  width:36px;height:36px;display:flex;align-items:center;justify-content:center;
  border-radius:var(--r);color:var(--muted);transition:background .15s,color .15s;
  flex-shrink:0;
}
.tt-btn:hover{background:var(--off);color:var(--txt)}

/* ── Layout ── */
.main{max-width:800px;margin:0 auto;padding:18px 14px;
  display:flex;flex-direction:column;gap:14px}

/* ── Card ── */
.card{background:var(--surf);border:1px solid var(--border);
  border-radius:var(--rx);overflow:hidden;box-shadow:var(--sh)}
.card-hdr{padding:9px 16px;border-bottom:1px solid var(--div);
  display:flex;align-items:center;gap:8px}
.card-title{font-size:.68rem;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.08em}
.card-body{padding:15px 16px}

/* ── Consistent hint/description text ── */
/* Used for ALL small descriptive text under inputs, under QR, etc. */
.hint{
  font-size:.78rem;
  color:var(--hint);   /* --hint is lighter than --muted, same in both themes */
  line-height:1.45;
  margin-top:6px;
}
.hint-top{   /* same style but margin above instead of below */
  font-size:.78rem;color:var(--hint);line-height:1.45;margin-bottom:8px;
}

/* ── Name row ── */
.name-row{display:flex;align-items:stretch;gap:8px;width:100%}
.name-inp{
  flex:1 1 0;min-width:0;
  padding:9px 12px;border:1px solid var(--border);border-radius:var(--r);
  background:var(--surf2);font-size:.9rem;
  transition:border-color .15s,box-shadow .15s;
}
.name-inp:focus{outline:none;border-color:var(--pri);
  box-shadow:0 0 0 3px var(--pri-hl)}
.btn-save{
  flex:0 0 auto;padding:9px 18px;border-radius:var(--r);
  font-size:.9rem;font-weight:600;background:var(--pri);color:#fff;
  transition:background .15s;white-space:nowrap;
}
.btn-save:hover{background:var(--pri-h)}

/* ── Drop zone ── */
.drop-zone{
  border:2px dashed var(--border);border-radius:var(--rx);
  padding:26px 16px;text-align:center;cursor:pointer;position:relative;
  transition:border-color .15s,background .15s;
}
.drop-zone:hover,.drop-zone.drag-over{border-color:var(--pri);background:var(--pri-hl)}
.drop-zone input[type=file]{
  position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.drop-ico{font-size:2rem;margin-bottom:7px}
.drop-lbl{font-size:.9rem;font-weight:600;margin-bottom:3px}
.drop-sub{font-size:.78rem;color:var(--hint)}

/* ── Progress ── */
.prog-wrap{margin-top:11px;display:none;flex-direction:column;gap:5px}
.prog-wrap.on{display:flex}
.prog-track{background:var(--off);border-radius:var(--rfull);height:5px;overflow:hidden}
.prog-bar{height:100%;background:var(--pri);border-radius:var(--rfull);
  width:0%;transition:width .2s ease}
.prog-lbl{font-size:.75rem;color:var(--muted)}

/* ── Error ── */
.err-msg{display:none;font-size:.75rem;color:var(--err);
  background:var(--err-bg);padding:7px 12px;border-radius:var(--r);margin-top:7px}
.err-msg.on{display:block}

/* ── File list ── */
.file-list{display:flex;flex-direction:column;margin-top:12px}
.file-row{
  display:grid;
  grid-template-columns:28px 1fr 32px;
  grid-template-rows:auto auto;
  column-gap:8px;
  padding:8px 6px;
  border-radius:var(--r);
  transition:background .15s;
  align-items:start;
}
.file-row:hover{background:var(--off)}
.file-row+.file-row{border-top:1px solid var(--div)}
.f-ico{grid-column:1;grid-row:1/3;font-size:1.2rem;align-self:center;line-height:1;padding-top:1px}
.f-name{grid-column:2;grid-row:1;font-size:.88rem;font-weight:500;
  overflow-wrap:break-word;word-break:break-word;line-height:1.35}
.f-meta{grid-column:2;grid-row:2;font-size:.72rem;color:var(--hint);
  font-variant-numeric:tabular-nums;margin-top:2px}
.f-action{grid-column:3;grid-row:1/3;align-self:center;
  display:flex;align-items:center;justify-content:center}
.rm-btn{width:28px;height:28px;display:flex;align-items:center;justify-content:center;
  border-radius:var(--r);font-size:.75rem;color:var(--hint);
  transition:background .15s,color .15s}
.rm-btn:hover{background:#fde8e8;color:#c0392b}
[data-theme="dark"] .rm-btn:hover{background:#3d1f1f;color:#e07070}
.dl-btn{width:32px;height:32px;display:flex;align-items:center;justify-content:center;
  border-radius:var(--r);background:var(--pri-hl);color:var(--pri);
  font-size:1rem;font-weight:700;text-decoration:none;transition:background .15s,color .15s}
.dl-btn:hover{background:var(--pri);color:#fff}
.dl-btn svg{width:16px;height:16px}

/* ── User groups ── */
.user-group{border:1px solid var(--pri);border-radius:var(--rx);
  overflow:hidden;margin-bottom:11px}
.user-group:last-child{margin-bottom:0}
.user-group.is-me{border-color:var(--div)}
.u-hdr{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--off)}
.u-av{width:28px;height:28px;border-radius:50%;background:var(--pri);color:#fff;
  display:flex;align-items:center;justify-content:center;
  font-size:.72rem;font-weight:700;flex-shrink:0}
.u-nm{font-size:.88rem;font-weight:600;flex:1;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.u-ct{font-size:.72rem;color:var(--hint);white-space:nowrap}
.dl-all{
  flex-shrink:0;display:flex;align-items:center;gap:5px;
  padding:6px 11px;border-radius:var(--rfull);
  background:var(--pri);color:#fff;font-size:.74rem;font-weight:600;
  text-decoration:none;white-space:nowrap;transition:background .15s;
}
.dl-all:hover{background:var(--pri-h)}
.dl-all svg{width:13px;height:13px}
/* ── Download-everything bar (top of network card) ── */
.dl-every{
  display:flex;align-items:center;justify-content:center;gap:7px;
  width:100%;padding:11px 14px;margin-bottom:12px;
  border-radius:var(--r);background:var(--pri);color:#fff;
  font-size:.86rem;font-weight:600;text-decoration:none;
  transition:background .15s;
}
.dl-every:hover{background:var(--pri-h)}
.dl-every svg{width:16px;height:16px;flex-shrink:0}
.dl-every .sub{font-weight:400;opacity:.85;font-size:.78rem}
.u-files{padding:4px 8px}
.u-files .file-list{margin-top:0}

/* ── QR section ── */
.qr-wrap{display:flex;flex-direction:column;align-items:center;gap:10px}
.qr-box{background:#fff;padding:13px;border-radius:var(--rx);
  border:1px solid var(--border);display:inline-block}
.qr-url{font-size:.8rem;color:var(--pri);font-weight:500;
  text-align:center;word-break:break-all}
/* QR instruction line — bold action first */
.qr-action{font-size:.88rem;font-weight:600;color:var(--txt);text-align:center}
/* QR description line — lighter, consistent hint style */
.qr-desc{font-size:.78rem;color:var(--hint);text-align:center;line-height:1.45;
  max-width:28ch;margin:0 auto}

/* ── Empty ── */
.empty{color:var(--hint);font-size:.85rem;text-align:center;padding:22px 0}

@media(max-width:480px){
  .main{padding:12px 10px;gap:11px}
  .card-body{padding:12px}
}
"""

JS = """
const API = window.location.origin;
let session = '';
let warnBytes = 512*1024*1024;   // refreshed from /api/state
let measuredBps = 0;             // last measured throughput, bytes/sec

async function initSession(){
  const r = await fetch(`${API}/api/session`,{method:'POST',body:'{}'});
  const d = await r.json();
  session = d.session;
  const el = document.getElementById('username');
  if(el) el.value = d.user.name;
  startPolling();
}

async function fetchState(){
  const r = await fetch(`${API}/api/state`);
  return r.json();
}

function startPolling(){
  const tick=async()=>{
    const s = await fetchState();
    if(s.warn_bytes) warnBytes = s.warn_bytes;
    renderMyFiles(s.user?.files||[]);
    renderAllUsers(s.all_users||[]);
    renderActivity(s.transfers||[], s.session);
  };
  tick();                 // paint immediately, then keep polling
  setInterval(tick, 2500);
}

function escHtml(s){
  return String(s).replace(/[&<>"']/g,c=>(
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Ambient "host activity" banner, fed by the /api/state poll. Shows every
// in-flight download/zip and its phase; a queued transfer shows its position.
function renderActivity(transfers, session){
  const el=document.getElementById('activity');
  if(!el) return;
  if(!transfers||!transfers.length){ el.innerHTML=''; return; }
  const rows=transfers.map(t=>{
    const icon = t.phase==='sending'?'⬇' : t.phase==='building'?'⏳' : '⋯';
    let stat;
    if(t.phase==='queued')        stat = t.pos ? `waiting · #${t.pos} of ${t.queued_total}` : 'waiting…';
    else if(t.phase==='building') stat = 'preparing…';
    else                          stat = 'sending…';
    const who = t.mine ? 'you' : (t.owner||'someone');
    return `<div style="display:flex;justify-content:space-between;gap:12px;padding:3px 0;${t.mine?'font-weight:600;':''}">`
      +`<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${icon} ${escHtml(t.name)}</span>`
      +`<span style="color:var(--muted);white-space:nowrap">${escHtml(t.size_str)} · ${stat} · ${escHtml(who)}</span></div>`;
  }).join('');
  const n=transfers.length;
  el.innerHTML=`<div style="pointer-events:auto;background:var(--surf2);border:1px solid var(--off);border-radius:12px;padding:10px 13px;max-height:42vh;overflow-y:auto;box-shadow:0 6px 24px rgba(0,0,0,.20)">`
    +`<div style="font-size:.78rem;font-weight:600;letter-spacing:.02em;color:var(--txt);margin-bottom:5px">`
    +`Host activity — ${n} transfer${n>1?'s':''} in progress</div>`
    +`<div style="font-size:.8rem;color:var(--txt)">${rows}</div></div>`;
}

function fileIcon(name){
  const ext=(name.split('.').pop()||'').toLowerCase();
  const m={
    pdf:'📄',
    jpg:'🖼',jpeg:'🖼',png:'🖼',gif:'🖼',webp:'🖼',svg:'🖼',heic:'🖼',heif:'🖼',raw:'🖼',
    mp4:'🎬',mov:'🎬',avi:'🎬',mkv:'🎬',webm:'🎬',m4v:'🎬',
    mp3:'🎵',flac:'🎵',wav:'🎵',aac:'🎵',m4a:'🎵',ogg:'🎵',opus:'🎵',
    zip:'🗜',rar:'🗜','7z':'🗜',tar:'🗜',gz:'🗜',bz2:'🗜',xz:'🗜',
    doc:'📝',docx:'📝',odt:'📝',rtf:'📝',
    xls:'📊',xlsx:'📊',ods:'📊',csv:'📊',
    ppt:'📋',pptx:'📋',odp:'📋',
    txt:'📃',md:'📃',log:'📃',
    json:'⚙',xml:'⚙',yaml:'⚙',yml:'⚙',toml:'⚙',ini:'⚙',cfg:'⚙',
    js:'💻',ts:'💻',py:'💻',html:'💻',css:'💻',sh:'💻',bat:'💻',
    apk:'📱',ipa:'📱',
    dmg:'💿',iso:'💿',exe:'⚙',msi:'⚙',deb:'⚙',rpm:'⚙',
  };
  return m[ext]||'📁';
}

let _myFilesSig=null;
function renderMyFiles(files){
  const el = document.getElementById('my-files');
  if(!el) return;
  // Skip the DOM rebuild when nothing changed — avoids churning buttons under an
  // in-flight click every poll (contributes to the re-fire).
  const sig=JSON.stringify(files);
  if(sig===_myFilesSig) return;
  _myFilesSig=sig;
  if(!files.length){el.innerHTML='<p class="empty">No files shared yet.</p>';return;}
  el.innerHTML='<div class="file-list">'+files.map(f=>`
    <div class="file-row">
      <span class="f-ico">${fileIcon(f.name)}</span>
      <span class="f-name">${escHtml(f.name)}</span>
      <span class="f-meta">${f.size_str}</span>
      <span class="f-action">
        <button class="rm-btn" onclick="removeFile('${f.id}')" title="Remove">✕</button>
      </span>
    </div>`).join('')+'</div>';
}

let _netSig=null;
function renderAllUsers(all_users){
  const el = document.getElementById('net-files');
  if(!el) return;
  // Skip the rebuild when the network file list is unchanged (see renderMyFiles).
  const sig=JSON.stringify(all_users);
  if(sig===_netSig) return;
  _netSig=sig;
  if(!all_users.length){el.innerHTML='<p class="empty">No files on the network yet.</p>';return;}
  // Alphabetical by name, but keep your own section pinned to the bottom.
  all_users=[...all_users].sort((a,b)=>
    (a.is_me?1:0)-(b.is_me?1:0) ||
    a.name.localeCompare(b.name,undefined,{sensitivity:'base'}));
  // "Download everything" covers everyone else (your own files are up top already).
  const others=all_users.filter(u=>!u.is_me);
  const totalFiles=others.reduce((n,u)=>n+u.files.length,0);
  const totalBytes=others.reduce((n,u)=>n+u.files.reduce((m,f)=>m+(f.size||0),0),0);
  const everyBar=(totalFiles>0)?`
    <a class="dl-every" href="${API}/api/download_everything" download
       onclick="return confirmBulk(event,this.href,${totalBytes})"
       title="Download every file from everyone as one ZIP, organized into a subfolder per user">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 10l5 5 5-5M4 21h16"/></svg>
      Download everything (.zip)
      <span class="sub">· ${totalFiles} files · ${fmtBytes(totalBytes)}, foldered by user</span>
    </a>`:'';
  const groupHtml=u=>{
    const ub=u.files.reduce((m,f)=>m+(f.size||0),0);
    return `
    <div class="user-group${u.is_me?' is-me':''}">
      <div class="u-hdr">
        <div class="u-av">${escHtml(u.name[0].toUpperCase())}</div>
        <span class="u-nm">${escHtml(u.name)}${u.is_me?' <span style="color:var(--hint);font-weight:400">(you)</span>':''}</span>
        ${u.files.length>1?`<a class="dl-all" href="${API}/api/download_all?user=${encodeURIComponent(u.id)}" download
             onclick="return confirmBulk(event,this.href,${ub})"
             title="Download all ${u.files.length} of ${escAttr(u.name)}'s files as one ZIP">
             <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 10l5 5 5-5M4 21h16"/></svg>
             All ${u.files.length} files (.zip)</a>`:''}
      </div>
      <div class="u-files"><div class="file-list">
        ${u.files.map(f=>`
          <div class="file-row">
            <span class="f-ico">${fileIcon(f.name)}</span>
            <span class="f-name">${escHtml(f.name)}</span>
            <span class="f-meta">${f.size_str}</span>
            <span class="f-action">
              <a class="dl-btn" href="${API}/api/download?id=${f.id}"
                 download="${escAttr(f.name)}" onclick="return downloadOne(event,'${f.id}')" title="Download"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 10l5 5 5-5M4 21h16"/></svg></a>
            </span>
          </div>`).join('')}
      </div></div>
    </div>`;};
  // Layout: everyone else's files, then the "Download everything" bar acting as a
  // divider, then your own contribution below it (which the bar deliberately omits).
  const mine=all_users.filter(u=>u.is_me).map(groupHtml).join('');
  el.innerHTML=others.map(groupHtml).join('')+everyBar+mine;
}

async function removeFile(id){
  await fetch(`${API}/api/remove_file`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const s=await fetchState();
  renderMyFiles(s.user?.files||[]);
  renderAllUsers(s.all_users||[]);
}

async function renameUser(){
  const inp=document.getElementById('username');
  const name=inp.value.trim();
  if(!name) return;
  await fetch(`${API}/api/rename`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  inp.blur();
}

async function uploadFiles(fileList){
  if(!fileList||!fileList.length) return;
  const prog=document.getElementById('upload-prog');
  const bar=document.getElementById('prog-bar');
  const lbl=document.getElementById('prog-lbl');
  const err=document.getElementById('upload-err');
  err.classList.remove('on');
  prog.classList.add('on');
  bar.style.width='0%';
  const fd=new FormData();
  Array.from(fileList).forEach(f=>fd.append('file',f,f.name));
  const n=fileList.length;
  const noun=`${n} file${n>1?'s':''}`;
  lbl.textContent=`Uploading ${noun}…`;
  const t0=performance.now();
  try{
    // Real progress: XHR reports true bytes sent (fetch cannot). On a stall the
    // bar stops at the actual point instead of creeping to ~100% (item 9).
    const d=await new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest();
      xhr.open('POST',`${API}/api/upload`);
      xhr.upload.onprogress=(e)=>{
        if(!e.lengthComputable) return;
        const pct=Math.min(100,e.loaded/e.total*100);
        bar.style.width=pct.toFixed(1)+'%';
        const secs=(performance.now()-t0)/1000;
        const bps=secs>0?e.loaded/secs:0;
        const eta=bps>0?(e.total-e.loaded)/bps:0;
        lbl.textContent=`Uploading ${noun}… ${Math.floor(pct)}% · `
          +`${fmtBytes(e.loaded)}/${fmtBytes(e.total)}`
          +(eta>0?` · ${fmtTime(eta)} left`:'');
      };
      // All bytes sent; the server is now writing them to disk and replying.
      xhr.upload.onload=()=>{bar.style.width='100%';lbl.textContent=`Saving ${noun}…`;};
      xhr.onload=()=>{
        if(xhr.status>=200&&xhr.status<300){
          try{resolve(JSON.parse(xhr.responseText));}
          catch(_){reject(new Error('bad server response'));}
        }else reject(new Error('server returned '+xhr.status));
      };
      xhr.onerror=()=>reject(new Error('network error'));
      xhr.onabort=()=>reject(new Error('cancelled'));
      xhr.send(fd);
    });
    if(d.error){err.textContent=d.error;err.classList.add('on');prog.classList.remove('on');}
    else{
      bar.style.width='100%';
      lbl.textContent=`✓ ${d.files.length} file${d.files.length>1?'s':''} shared!`;
      setTimeout(()=>{prog.classList.remove('on');bar.style.width='0%';},2200);
    }
    const s=await fetchState();
    renderMyFiles(s.user?.files||[]);
    renderAllUsers(s.all_users||[]);
  }catch(e){
    err.textContent='Upload failed: '+e.message;
    err.classList.add('on');
    prog.classList.remove('on');
  }
  const fi=document.getElementById('file-input');
  if(fi) fi.value='';
}

function fmtBytes(b){
  const u=['B','KB','MB','GB','TB']; let i=0;
  while(b>=1024&&i<u.length-1){b/=1024;i++;}
  return b.toFixed(b<10&&i>0?1:0)+' '+u[i];
}
function fmtTime(sec){
  if(!isFinite(sec)||sec<=0) return 'an unknown amount of time';
  if(sec<60) return 'about '+Math.max(1,Math.round(sec))+' sec';
  if(sec<3600) return 'about '+Math.round(sec/60)+' min';
  return 'about '+(sec/3600).toFixed(1)+' hr';
}
async function probeSpeed(){
  // Time a small sample download to estimate this client's throughput (bytes/sec).
  try{
    const t0=performance.now();
    const r=await fetch(`${API}/api/speedtest?t=`+Date.now());
    const buf=await r.arrayBuffer();
    const secs=(performance.now()-t0)/1000;
    if(secs>0) measuredBps=buf.byteLength/secs;
  }catch(e){}
  return measuredBps;
}
// Start a download WITHOUT navigating the page. A bare `download` anchor clicked
// programmatically downloads in the background (using the server's filename) and
// leaves the current page untouched — so a slow, still-spooling zip no longer
// blanks the page, and a refresh no longer re-fires the download (BUG: host page
// navigated to /api/download_all and duplicated on reload).
const _dlLast = new Map();   // download URL -> ms of last fire (debounce double-taps)
const DL_DEBOUNCE_MS = 1500;
// Native browser download via a detached, self-removing anchor on <body> (NOT the
// polled file list, so a re-render cannot abort it). This gives the browser's
// instant download UI. The duplicate-download fix is SERVER-SIDE: /api/download no
// longer advertises Accept-Ranges, so the browser stops its start/cancel/
// re-segment dance that duplicated small files. This debounce only guards against
// an accidental rapid double-tap. See BACKLOG 10.
function triggerDownload(url){
  const now=Date.now();
  if(now-(_dlLast.get(url)||0) < DL_DEBOUNCE_MS) return;   // ignore a rapid repeat
  _dlLast.set(url, now);
  const a=document.createElement('a');
  a.href=url; a.download='';
  a.style.display='none';
  document.body.appendChild(a);
  a.click();
  setTimeout(()=>a.remove(), 2000);
}
// Single-file button. preventDefault so the anchor never navigates on its own;
// triggerDownload owns the request. href/download stay as a no-JS fallback.
function downloadOne(ev, id){
  ev.preventDefault();
  triggerDownload(`${API}/api/download?id=${encodeURIComponent(id)}`);
  return false;
}

async function confirmBulk(ev, url, totalBytes){
  ev.preventDefault();                       // never let the anchor navigate
  if(totalBytes && totalBytes>warnBytes){
    const bps=await probeSpeed();
    const eta = bps>0 ? fmtTime(totalBytes/bps) : 'an unknown amount of time';
    const speed = bps>0 ? ` (~${fmtBytes(bps)}/s on this network)` : '';
    if(!confirm(`This download is ${fmtBytes(totalBytes)} and will take ${eta}${speed}.\n\nStart the download?`))
      return false;
  }
  triggerDownload(url);
  return false;
}
function escHtml(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(s){return s.replace(/"/g,'&quot;');}

function initDragDrop(){
  const dz=document.getElementById('drop-zone');
  if(!dz) return;
  dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag-over')});
  dz.addEventListener('dragleave',()=>dz.classList.remove('drag-over'));
  dz.addEventListener('drop',e=>{
    e.preventDefault();dz.classList.remove('drag-over');uploadFiles(e.dataTransfer.files);
  });
}

function initTheme(){
  const btn=document.querySelector('[data-tt]');
  const root=document.documentElement;
  const SUN=`<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>`;
  const MOON=`<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;
  let d=matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';
  root.setAttribute('data-theme',d);
  if(btn) btn.innerHTML=d==='dark'?SUN:MOON;
  btn&&btn.addEventListener('click',()=>{
    d=d==='dark'?'light':'dark';
    root.setAttribute('data-theme',d);
    btn.innerHTML=d==='dark'?SUN:MOON;
  });
}
"""

# ─── Reusable HTML blocks ─────────────────────────────────────────────────────

LOGO_SVG = """<svg width="22" height="22" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/>
  <polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/>
</svg>"""

MOON_ICO = """<svg width="18" height="18" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="2">
  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>"""

NAME_CARD = """
<div class="card">
  <div class="card-hdr"><span>👤</span><span class="card-title">Your Name</span></div>
  <div class="card-body">
    <div class="name-row">
      <input class="name-inp" id="username" type="text"
             placeholder="Display name" maxlength="30"
             onkeydown="if(event.key==='Enter')renameUser()">
      <button class="btn-save" onclick="renameUser()">Save</button>
    </div>
    <p class="hint">Others on the network see this name next to your files.</p>
  </div>
</div>
"""

UPLOAD_CARD = """
<div class="card">
  <div class="card-hdr"><span>📂</span><span class="card-title">Share Files</span></div>
  <div class="card-body">
    <div class="drop-zone" id="drop-zone">
      <input type="file" id="file-input" multiple accept="*/*"
             onchange="uploadFiles(this.files)">
      <div class="drop-ico">📁</div>
      <p class="drop-lbl">Tap to choose files</p>
      <p class="drop-sub">Desktop: drag &amp; drop &nbsp;·&nbsp; Any file type &nbsp;·&nbsp; Multiple OK</p>
    </div>
    <div class="prog-wrap" id="upload-prog">
      <div class="prog-track"><div class="prog-bar" id="prog-bar"></div></div>
      <span class="prog-lbl" id="prog-lbl">Uploading…</span>
    </div>
    <p class="err-msg" id="upload-err"></p>
    <div id="my-files"><p class="empty">No files shared yet.</p></div>
  </div>
</div>
"""

NET_CARD = """
<div class="card">
  <div class="card-hdr"><span>🌐</span><span class="card-title">Available on Network</span></div>
  <div class="card-body">
    <div id="net-files"><p class="empty">No files on the network yet.</p></div>
  </div>
</div>
"""

# QR card — used on host (top) and remote (bottom).
# show_invite=True uses warmer invite copy for the remote page's bottom placement.
def qr_card(show_invite=False):
    if show_invite:
        action = "Someone nearby? Show them this QR code."
        desc   = "They scan it with their phone camera and join instantly — no app needed."
    else:
        action = "Scan this QR code to join on your phone."
        desc   = "Opens right in your browser — share and download files with anyone in the room."
    return f"""
<div class="card">
  <div class="card-hdr"><span>📱</span>
    <span class="card-title">{"Invite Others" if show_invite else "Connect via QR Code"}</span>
  </div>
  <div class="card-body">
    <div class="qr-wrap">
      <div class="qr-box" id="qrcode{'_invite' if show_invite else ''}"></div>
      <p class="qr-url" id="remote-url{'_invite' if show_invite else ''}">Loading…</p>
      <p class="qr-action">{action}</p>
      <p class="qr-desc">{desc}</p>
    </div>
  </div>
</div>
"""

def make_page(badge, tagline, body, init_script):
    tagline_html = f'<span class="hdr-tagline">{tagline}</span>' if tagline else ''
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>DropZone — {badge}</title>
<link href="https://api.fontshare.com/v2/css?f[]=satoshi@400,500,600,700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>{CSS}</style>
</head>
<body>
<header class="hdr">
  <div class="hdr-top">
    <a href="/" class="logo">{LOGO_SVG} DropZone</a>
    <span class="badge">{badge}</span>
    <div class="hdr-right">
      <span style="font-size:.72rem;font-weight:500;color:var(--hint);letter-spacing:.02em" title="Running build">v{__version__} &middot; {VERSION_STAGE}</span>
      <button class="tt-btn" data-tt aria-label="Toggle theme">{MOON_ICO}</button>
    </div>
  </div>
  {tagline_html}
</header>
<main class="main">
{body}
</main>
<!-- Floating, out of normal flow: always visible (no scroll to the top) and its
     show/hide never shifts the page content below it. Empty => invisible. -->
<div id="activity" style="position:fixed;left:50%;transform:translateX(-50%);bottom:12px;width:min(680px,calc(100vw - 20px));z-index:60;pointer-events:none"></div>
<script>
{JS}
{init_script}
</script>
</body>
</html>"""

# ─── Host page ────────────────────────────────────────────────────────────────
HOST_BODY = f"""
{qr_card(show_invite=False)}
{NAME_CARD}
{UPLOAD_CARD}
{NET_CARD}
"""

HOST_INIT = """
initTheme(); initDragDrop();
(async()=>{
  await initSession();
  const s = await fetchState();
  const url = `http://${s.local_ip}:${s.port}/remote`;
  document.getElementById('remote-url').textContent = url;
  new QRCode(document.getElementById('qrcode'),{
    text:url, width:200, height:200,
    colorDark:'#01696f', colorLight:'#ffffff',
    correctLevel:QRCode.CorrectLevel.M
  });
  renderMyFiles(s.user?.files||[]);
  renderAllUsers(s.all_users||[]);
})();
"""

# ─── Remote page ─────────────────────────────────────────────────────────────
REMOTE_TAGLINE = "Share &amp; download files with anyone on this WiFi"

REMOTE_BODY = f"""
{NAME_CARD}
{UPLOAD_CARD}
{NET_CARD}
{qr_card(show_invite=True)}
"""

REMOTE_INIT = """
initTheme(); initDragDrop();
(async()=>{
  await initSession();
  const s = await fetchState();
  const url = `http://${s.local_ip}:${s.port}/remote`;
  // populate both QR url labels (invite card at bottom)
  const urlEl = document.getElementById('remote-url_invite');
  if(urlEl) urlEl.textContent = url;
  new QRCode(document.getElementById('qrcode_invite'),{
    text:url, width:180, height:180,
    colorDark:'#01696f', colorLight:'#ffffff',
    correctLevel:QRCode.CorrectLevel.M
  });
  renderMyFiles(s.user?.files||[]);
  renderAllUsers(s.all_users||[]);
})();
"""

HOST_PAGE   = make_page("Host",   "",              HOST_BODY,   HOST_INIT)
REMOTE_PAGE = make_page("Remote", REMOTE_TAGLINE,  REMOTE_BODY, REMOTE_INIT)

# ─── Run ──────────────────────────────────────────────────────────────────────
class _LogErrors:
    """Route otherwise-silent per-request crashes into the log file, so a Termux
    crash still leaves a record. Benign client disconnects are already caught in
    the handlers and never reach here."""
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            # Client vanished at the socket level (e.g. reset mid request-line) —
            # nothing actionable, so log one line instead of a full traceback.
            log(f"client {client_address[0]} dropped the connection ({type(exc).__name__})")
            return
        log(f"UNHANDLED server error from {client_address}:\n{traceback.format_exc()}")

class ThreadingServer(_LogErrors, socketserver.ThreadingMixIn, HTTPServer):
    """Serve each request in its own daemon thread so one big transfer never
    blocks the room. transfer_sem caps how many heavy transfers run at once;
    light requests (state polling) stay snappy."""
    daemon_threads = True

class SingleServer(_LogErrors, HTTPServer):
    pass

def make_server():
    if THREADED:
        try:
            return ThreadingServer(("0.0.0.0", PORT), Handler)
        except Exception as e:
            print(f"  (threaded server unavailable: {e} - falling back to single-threaded)")
    return SingleServer(("0.0.0.0", PORT), Handler)

if __name__ == "__main__":
    # Keep the log from growing without bound across sessions (simple 1-file roll).
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
    except OSError:
        pass
    log(f"=== DropZone v{__version__} ({VERSION_STAGE}) starting; pid={os.getpid()} ===")
    server = make_server()
    print(f"\n  ╔══════════════════════════════════════════╗")
    print(f"  ║         DropZone is running!             ║")
    print(f"  ║  build v{__version__}  ·  {VERSION_STAGE}")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  Host page  →  http://localhost:{PORT}    ║")
    print(f"  ║  Remote URL →  http://{LOCAL_IP}:{PORT}/remote")
    print(f"  ╠══════════════════════════════════════════╣")
    print(f"  ║  Ctrl+C to stop (cleans up temp files)  ║")
    print(f"  ╚══════════════════════════════════════════╝")
    print(f"  Log file →  {LOG_FILE}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping — cleaning up uploads…")
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
        log("=== DropZone stopped (Ctrl+C) ===")
        print("  Done. Goodbye.")
    except Exception:
        log(f"FATAL in serve_forever:\n{traceback.format_exc()}")
        raise
