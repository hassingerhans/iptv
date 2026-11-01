#!/usr/bin/env python3
"""IPTV Browser: a local web page to browse, check and play IPTV playlists in VLC.

Usage:
    python3 iptv.py            # normal mode (cards with logos)
    python3 iptv.py --lite     # lite mode (text only, no images)
    python3 iptv.py --port 9000 --no-browser
    python3 iptv.py --list https://example.com/my.m3u --list ~/Videos/mine.m3u

Needs only Python 3.6+ (standard library) and VLC. Works on Linux, macOS, Windows.
Data (playlist cache, favorites, link results, logos) is stored in ./iptv-data
next to this script, or in ~/.iptv-browser if that folder isn't writable.
Set IPTV_BROWSER_HOME to choose another folder, or VLC_PATH to point at VLC.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

# Starter lists, copied into your saved settings once on first run.
# After that they are ordinary lists: rename, replace or remove them freely.
DEFAULT_SOURCES = [
    ("us", "US channels", "https://iptv-org.github.io/iptv/countries/us.m3u"),
    ("world", "Worldwide", "https://iptv-org.github.io/iptv/index.m3u"),
]
UA = "Mozilla/5.0 (X11; Linux x86_64) IPTV-Browser/1.0"
FRESH_SECONDS = 12 * 3600
MAX_LOGO_BYTES = 512 * 1024


def pick_data_dir():
    cands = []
    env = os.environ.get("IPTV_BROWSER_HOME")
    if env:
        cands.append(env)
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "iptv-data"))
    cands.append(os.path.join(os.path.expanduser("~"), ".iptv-browser"))
    for d in cands:
        try:
            os.makedirs(os.path.join(d, "logos"), exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except OSError:
            continue
    raise SystemExit("Couldn't find a writable folder for the cache.")


DATA = pick_data_dir()


# ---------------------------------------------------------------- state

class State(object):
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.d = {"fav": [], "recent": [], "alive": {}, "tags": {}, "sources": [], "seeded": False}
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k in self.d:
                if k in loaded and isinstance(loaded[k], type(self.d[k])):
                    self.d[k] = loaded[k]
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.d, f)
                os.replace(tmp, self.path)
            except OSError:
                pass


STATE = State(os.path.join(DATA, "state.json"))

if not STATE.d.get("seeded"):
    have = set(s.get("id") for s in STATE.d["sources"])
    starters = [{"id": i, "name": n, "loc": loc} for i, n, loc in DEFAULT_SOURCES if i not in have]
    STATE.d["sources"] = starters + STATE.d["sources"]
    STATE.d["seeded"] = True
    STATE.save()


# ---------------------------------------------------------------- playlist

ATTR = re.compile(r'([\w-]+)="([^"]*)"')
HTTP_URL = re.compile(r"https?://", re.I)


def split_extinf(line):
    inq = False
    for i, ch in enumerate(line):
        if ch == '"':
            inq = not inq
        elif ch == "," and not inq:
            return line[:i], line[i + 1:]
    return line, ""


def parse_m3u(text):
    out = []
    seen = set()
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            head, name = split_extinf(line)
            attrs = dict(ATTR.findall(head))
            name = name.strip() or attrs.get("tvg-name", "") or "Unnamed"
            genres = []
            for g in attrs.get("group-title", "").split(";"):
                g = g.strip()
                if g and g.lower() != "undefined" and g not in genres:
                    genres.append(g)
            cur = {"name": name, "logo": attrs.get("tvg-logo", "").strip(),
                   "genres": genres or ["Uncategorized"], "referrer": "", "ua": ""}
        elif line.startswith("#EXTVLCOPT:") and cur is not None:
            k, _, v = line[len("#EXTVLCOPT:"):].partition("=")
            k = k.strip().lower()
            if k == "http-referrer":
                cur["referrer"] = v.strip()
            elif k == "http-user-agent":
                cur["ua"] = v.strip()
        elif line.startswith("#"):
            continue
        else:
            if cur is not None and HTTP_URL.match(line):
                cur["url"] = line
                key = (cur["name"] + "\n" + line).encode("utf-8", "replace")
                cur["id"] = hashlib.sha1(key).hexdigest()[:12]
                if cur["id"] not in seen:
                    seen.add(cur["id"])
                    out.append(cur)
            cur = None
    return out


LOADED = {}
BYID = {}
LOAD_LOCK = threading.Lock()


def all_sources():
    with STATE.lock:
        return [{"id": s["id"], "name": s["name"], "loc": s["loc"]} for s in STATE.d["sources"]]


def get_source(sid):
    for s in all_sources():
        if s["id"] == sid:
            return s
    return None


def remove_source(sid):
    with STATE.lock:
        STATE.d["sources"] = [s for s in STATE.d["sources"] if s["id"] != sid]
    with LOAD_LOCK:
        LOADED.pop(sid, None)
    try:
        os.remove(os.path.join(DATA, sid + ".m3u"))
    except OSError:
        pass
    STATE.save()


def add_source(name, loc):
    """Add a playlist by web link or local file path. Returns {"id"} or {"error"}."""
    loc = (loc or "").strip().strip('"').strip("'")
    if HTTP_URL.match(loc):
        parsed = urlparse(loc)
        default = parsed.netloc + parsed.path
    else:
        path = os.path.abspath(os.path.expanduser(loc)) if loc else ""
        if not path or not os.path.isfile(path):
            return {"error": "Enter a link starting with http:// or https://, or the full path of an .m3u file."}
        loc = path
        default = os.path.basename(path)
    sid = "c" + hashlib.sha1(loc.encode("utf-8", "replace")).hexdigest()[:8]
    if get_source(sid):
        return {"id": sid}
    name = (name or "").strip()[:60] or default[:60]
    with STATE.lock:
        STATE.d["sources"].append({"id": sid, "name": name, "loc": loc})
    try:
        chans, _ = load_source(sid, force=True)
        if not chans:
            raise RuntimeError("No channels found in that list.")
    except Exception as e:
        remove_source(sid)
        return {"error": str(e)}
    STATE.save()
    return {"id": sid}


def load_source(src, force=False):
    with LOAD_LOCK:
        if src in LOADED and not force:
            return LOADED[src], None
        s = get_source(src)
        if not s:
            raise RuntimeError("Unknown list.")
        note = None
        if HTTP_URL.match(s["loc"]):
            url = s["loc"]
            path = os.path.join(DATA, src + ".m3u")
            download = force or not os.path.exists(path)
        else:
            url = None
            path = os.path.expanduser(s["loc"])
            download = False
            if not os.path.isfile(path):
                raise RuntimeError("File not found: %s" % path)
            if os.path.getsize(path) > 100000000:
                raise RuntimeError("That file is too large.")
        if download:
            try:
                req = Request(url, headers={"User-Agent": UA})
                with urlopen(req, timeout=60) as r:
                    data = r.read()
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except Exception as e:
                if not os.path.exists(path):
                    raise RuntimeError("Couldn't download the playlist: %s" % e)
                note = "Couldn't refresh the list (%s). Showing the saved copy." % e
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            chans = parse_m3u(f.read())
        LOADED[src] = chans
        for c in chans:
            BYID[c["id"]] = c
        return chans, note


def channel_payload(src):
    chans, note = load_source(src)
    with STATE.lock:
        tags = dict(STATE.d["tags"])
        fav = list(STATE.d["fav"])
        recent = list(STATE.d["recent"])
        alive_raw = dict(STATE.d["alive"])
    out = []
    ids = set()
    for c in chans:
        ids.add(c["id"])
        g = [tags[c["id"]]] if c["id"] in tags else c["genres"]
        out.append({"id": c["id"], "n": c["name"], "g": g, "l": 1 if c["logo"] else 0})
    out.sort(key=lambda x: x["n"].lower())
    alive = {}
    for k, v in alive_raw.items():
        if k in ids:
            alive[k] = 1 if v[0] else 0
    return {"channels": out, "fav": [i for i in fav if i in ids],
            "recent": [i for i in recent if i in ids], "alive": alive, "note": note}


# ---------------------------------------------------------------- VLC

_VLC = []


def find_vlc():
    if _VLC:
        return _VLC[0]
    cmd = None
    p = os.environ.get("VLC_PATH")
    if p and os.path.exists(p):
        cmd = [p]
    if not cmd:
        w = shutil.which("vlc")
        if w:
            cmd = [w]
    if not cmd:
        for p in ("/Applications/VLC.app/Contents/MacOS/VLC",
                  r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                  r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"):
            if os.path.exists(p):
                cmd = [p]
                break
    if not cmd and shutil.which("flatpak"):
        try:
            if subprocess.call(["flatpak", "info", "org.videolan.VLC"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
                cmd = ["flatpak", "run", "org.videolan.VLC"]
        except OSError:
            pass
    _VLC.append(cmd)
    return cmd


def do_play(cid):
    ch = BYID.get(cid)
    if not ch:
        return {"ok": False, "error": "Unknown channel. Refresh the list."}
    base = find_vlc()
    if not base:
        return {"ok": False, "error": "VLC wasn't found. Install VLC, or set VLC_PATH to its location."}
    args = list(base)
    if ch["referrer"]:
        args.append("--http-referrer=" + ch["referrer"])
    if ch["ua"]:
        args.append("--http-user-agent=" + ch["ua"])
    args.append(ch["url"])
    kw = {}
    if os.name == "posix":
        kw["start_new_session"] = True
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **kw)
    except OSError as e:
        return {"ok": False, "error": "Couldn't start VLC: %s" % e}
    with STATE.lock:
        r = [i for i in STATE.d["recent"] if i != cid]
        STATE.d["recent"] = ([cid] + r)[:50]
    STATE.save()
    return {"ok": True, "name": ch["name"]}


# ---------------------------------------------------------------- link checker

CHECK = {"running": False, "done": 0, "total": 0, "alive": 0, "stop": False, "log": []}
CHECK_LOCK = threading.Lock()


def check_one(ch):
    headers = {"User-Agent": ch["ua"] or UA}
    if ch["referrer"]:
        headers["Referer"] = ch["referrer"]
    try:
        with urlopen(Request(ch["url"], headers=headers), timeout=8) as r:
            if r.getcode() >= 400:
                return False
            head = r.read(2048)
        path = ch["url"].lower().split("?")[0]
        if path.endswith(".m3u8"):
            return b"#EXTM3U" in head
        return len(head) > 0
    except Exception:
        return False


def run_checks(chans):
    try:
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = dict((ex.submit(check_one, c), c) for c in chans)
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    ok = bool(fut.result())
                except Exception:
                    ok = False
                with STATE.lock:
                    STATE.d["alive"][c["id"]] = [ok, int(time.time())]
                with CHECK_LOCK:
                    CHECK["done"] += 1
                    if ok:
                        CHECK["alive"] += 1
                    CHECK["log"].append([c["id"], 1 if ok else 0])
                    done = CHECK["done"]
                    stop = CHECK["stop"]
                if done % 50 == 0:
                    STATE.save()
                if stop:
                    for f in futs:
                        f.cancel()
                    break
    finally:
        STATE.save()
        with CHECK_LOCK:
            CHECK["running"] = False


def start_check(ids, force):
    with CHECK_LOCK:
        if CHECK["running"]:
            return {"error": "A check is already running."}
    now = time.time()
    chans = []
    with STATE.lock:
        alive = STATE.d["alive"]
        for i in ids:
            c = BYID.get(i)
            if not c:
                continue
            old = alive.get(i)
            if not force and old and now - old[1] < FRESH_SECONDS:
                continue
            chans.append(c)
    if not chans:
        return {"total": 0}
    with CHECK_LOCK:
        CHECK.update({"running": True, "done": 0, "total": len(chans), "alive": 0,
                      "stop": False, "log": []})
    t = threading.Thread(target=run_checks, args=(chans,))
    t.daemon = True
    t.start()
    return {"total": len(chans)}


# ---------------------------------------------------------------- logos

LOGO_SEM = threading.Semaphore(6)


def sniff(b):
    if b.startswith(b"\x89PNG"):
        return "image/png"
    if b[:2] == b"\xff\xd8":
        return "image/jpeg"
    if b[:4] == b"GIF8":
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    head = b[:300].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in b[:1000].lower()):
        return "image/svg+xml"
    return None


def public_host(host):
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except (OSError, ValueError):
        return False


def get_logo(cid):
    ch = BYID.get(cid)
    if not ch or not ch["logo"] or not HTTP_URL.match(ch["logo"]):
        return None
    url = ch["logo"]
    key = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()
    path = os.path.join(DATA, "logos", key)
    neg = path + ".none"
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        mime = sniff(data)
        return (data, mime) if mime else None
    if os.path.exists(neg) and time.time() - os.path.getmtime(neg) < 86400:
        return None
    with LOGO_SEM:
        try:
            host = urlparse(url).hostname or ""
            if not public_host(host):
                raise ValueError("not a public host")
            with urlopen(Request(url, headers={"User-Agent": UA}), timeout=8) as r:
                data = r.read(MAX_LOGO_BYTES + 1)
            if len(data) > MAX_LOGO_BYTES:
                raise ValueError("too large")
            mime = sniff(data)
            if not mime:
                raise ValueError("not an image")
            with open(path, "wb") as f:
                f.write(data)
            return data, mime
        except Exception:
            try:
                open(neg, "w").close()
            except OSError:
                pass
            return None


# ---------------------------------------------------------------- web server

class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    mode = "full"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_bytes(self, code, body, ctype, cache="no-store"):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self.host_ok():
            return self.send_json({"error": "Forbidden"}, 403)
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path == "/":
                page = PAGE.replace("__MODE__", self.server.mode)
                self.send_bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/channels":
                src = qs.get("src", ["us"])[0]
                if not get_source(src):
                    return self.send_json({"error": "Unknown list"}, 400)
                self.send_json(channel_payload(src))
            elif u.path == "/api/sources":
                self.send_json({"sources": [{"id": s["id"], "name": s["name"]} for s in all_sources()]})
            elif u.path == "/api/check/status":
                try:
                    since = max(0, int(qs.get("since", ["0"])[0]))
                except ValueError:
                    since = 0
                with CHECK_LOCK:
                    out = {"running": CHECK["running"], "done": CHECK["done"],
                           "total": CHECK["total"], "alive": CHECK["alive"],
                           "log": CHECK["log"][since:], "next": len(CHECK["log"])}
                self.send_json(out)
            elif u.path == "/logo":
                res = get_logo(qs.get("id", [""])[0])
                if not res:
                    return self.send_bytes(404, b"", "text/plain")
                self.send_bytes(200, res[0], res[1], "max-age=604800")
            else:
                self.send_bytes(404, b"Not found", "text/plain")
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-IPTV") != "1":
            return self.send_json({"error": "Forbidden"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2000000:
                return self.send_json({"error": "Too large"}, 413)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            path = urlparse(self.path).path
            if path == "/api/play":
                self.send_json(do_play(str(body.get("id", ""))))
            elif path == "/api/fav":
                cid = str(body.get("id", ""))
                with STATE.lock:
                    f = STATE.d["fav"]
                    if cid in f:
                        f.remove(cid)
                    elif cid in BYID:
                        f.append(cid)
                    fav = list(f)
                STATE.save()
                self.send_json({"fav": fav})
            elif path == "/api/tag":
                cid = str(body.get("id", ""))
                tag = str(body.get("tag", "")).strip()[:40]
                if cid in BYID:
                    with STATE.lock:
                        if tag:
                            STATE.d["tags"][cid] = tag
                        else:
                            STATE.d["tags"].pop(cid, None)
                    STATE.save()
                self.send_json({"ok": True})
            elif path == "/api/check":
                ids = [str(i) for i in body.get("ids", [])][:20000]
                self.send_json(start_check(ids, bool(body.get("force"))))
            elif path == "/api/check/stop":
                with CHECK_LOCK:
                    CHECK["stop"] = True
                self.send_json({"ok": True})
            elif path == "/api/refresh":
                src = str(body.get("src", "us"))
                if not get_source(src):
                    return self.send_json({"error": "Unknown list"}, 400)
                load_source(src, force=True)
                self.send_json({"ok": True})
            elif path == "/api/source/add":
                self.send_json(add_source(str(body.get("name", "")), str(body.get("loc", ""))))
            elif path == "/api/source/remove":
                sid = str(body.get("id", ""))
                s = get_source(sid)
                if not s:
                    return self.send_json({"error": "Unknown list"}, 400)
                remove_source(sid)
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "Not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPTV Browser</title>
<style>
:root{--bg:#fff;--fg:#1f1f1f;--mut:#6b6b6b;--line:#dcdcdc;--card:#f6f6f4;--acc:#185fa5;--accbg:#e6f1fb;--ok:#2f9e44;--bad:#d64545;}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1a;--fg:#ececea;--mut:#a0a09a;--line:#3a3a38;--card:#262624;--acc:#85b7eb;--accbg:#0c447c;--ok:#6fcf86;--bad:#f09595;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 14px;z-index:5}
h1{font-size:17px;font-weight:500;margin:0 0 8px}
h1 small{font-size:12px;color:var(--mut);font-weight:400;margin-left:6px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input[type=text],select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
input[type=text]{flex:1;min-width:160px}
label{color:var(--mut);white-space:nowrap}
button{background:transparent;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 10px;font:inherit;cursor:pointer}
button:hover{background:var(--card)}
button.play{background:var(--accbg);color:var(--acc);border-color:var(--acc)}
button.ico{padding:4px 8px}
button.ico.on{color:#e0a100}
#status{min-height:18px;color:var(--mut);margin-top:6px;font-size:13px}
#count{color:var(--mut);padding:8px 14px 0}
main{padding:8px 14px 14px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--line);margin-right:6px;flex:none}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
.nm{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{color:var(--mut);padding:20px 0}
body.lite .row{display:flex;align-items:center;gap:8px;padding:3px 0;border-bottom:1px solid var(--line)}
body.lite .row .nm{flex:1;min-width:0}
body.lite .row .gn{color:var(--mut);width:22%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
body.lite button{padding:2px 8px}
body.full main{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;align-content:start}
body.full .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:6px;min-width:0}
body.full .lg{height:56px;border-radius:6px;background:var(--bg);display:flex;align-items:center;justify-content:center;color:var(--mut);font-size:18px;overflow:hidden}
body.full .lg img{max-width:100%;max-height:100%;object-fit:contain}
body.full .mt{color:var(--mut);font-size:12px;display:flex;align-items:center;overflow:hidden;white-space:nowrap}
body.full .acts{display:flex;gap:6px;margin-top:auto}
body.full .acts .play{flex:1}
#more{text-align:center;padding:12px;display:none}
footer{padding:8px 14px;color:var(--mut);border-top:1px solid var(--line)}
</style></head>
<body class="__MODE__">
<header>
<h1>IPTV Browser<small>__MODE__ mode</small></h1>
<div class="bar">
<input type="text" id="q" placeholder="Search channels" autocomplete="off">
<select id="src"></select>
<button id="addList" title="Add a playlist by link or file path">Add list</button>
<button id="rmList" title="Remove this list">Remove list</button>
<select id="genre"></select>
<label><input type="checkbox" id="favOnly"> Favorites</label>
<label><input type="checkbox" id="recentOnly"> Recent</label>
<label><input type="checkbox" id="hideDead"> Hide dead</label>
<button id="check" title="Shift-click to recheck channels that were checked recently">Check links</button>
<button id="refresh">Refresh list</button>
</div>
<div id="status"></div>
</header>
<div id="count"></div>
<main id="list"></main>
<div id="more"><button id="moreBtn">Show more</button></div>
<footer id="now">Double-click a channel, or press Play, to open it in VLC.</footer>
<script>
(function(){
var MODE="__MODE__";
var S={src:"us",chans:[],byid:{},fav:{},alive:{},recent:[],view:[],shown:0,page:MODE==="lite"?300:100,since:0,checking:false};
function $(id){return document.getElementById(id);}
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function api(path,body){
 var o={method:body?"POST":"GET",headers:{"X-IPTV":"1"}};
 if(body){o.headers["Content-Type"]="application/json";o.body=JSON.stringify(body);}
 return fetch(path,o).then(function(r){return r.json();});
}
function setStatus(t){$("status").textContent=t||"";}
function netErr(){setStatus("Couldn't reach the local server. Is the script still running?");}
function toSet(a){var o={};for(var i=0;i<a.length;i++)o[a[i]]=1;return o;}
function ini(n){var m=n.replace(/[^A-Za-z0-9 ]/g,"").trim();return m.substr(0,2).toUpperCase();}

function load(){
 if(!S.src){
  S.chans=[];S.byid={};buildGenres();applyFilter();
  setStatus("No lists yet. Press Add list and enter a playlist link or file path.");
  return;
 }
 setStatus("Loading channels…");
 api("/api/channels?src="+S.src).then(function(d){
  if(d.error){setStatus(d.error);return;}
  S.chans=d.channels;S.byid={};
  d.channels.forEach(function(c){S.byid[c.id]=c;});
  S.fav=toSet(d.fav);S.alive=d.alive;S.recent=d.recent;
  buildGenres();applyFilter();setStatus(d.note||"");
 }).catch(netErr);
}
function buildGenres(){
 var cnt={};
 S.chans.forEach(function(c){c.g.forEach(function(g){cnt[g]=(cnt[g]||0)+1;});});
 var names=Object.keys(cnt).sort(function(a,b){return cnt[b]-cnt[a]||(a<b?-1:1);});
 var sel=$("genre"),cur=sel.value;
 var h='<option value="">All genres ('+S.chans.length+')</option>';
 names.forEach(function(g){h+='<option value="'+esc(g)+'">'+esc(g)+' ('+cnt[g]+')</option>';});
 sel.innerHTML=h;
 if(cur&&cnt[cur])sel.value=cur;
}
function applyFilter(){
 var q=$("q").value.trim().toLowerCase(),g=$("genre").value;
 var fo=$("favOnly").checked,ro=$("recentOnly").checked,hd=$("hideDead").checked;
 var rank={};S.recent.forEach(function(id,i){rank[id]=i;});
 var v=S.chans.filter(function(c){
  if(g&&c.g.indexOf(g)<0)return false;
  if(q&&c.n.toLowerCase().indexOf(q)<0)return false;
  if(fo&&!S.fav[c.id])return false;
  if(ro&&rank[c.id]===undefined)return false;
  if(hd&&S.alive[c.id]===0)return false;
  return true;
 });
 if(ro)v.sort(function(a,b){return rank[a.id]-rank[b.id];});
 S.view=v;S.shown=0;$("list").innerHTML="";
 if(!v.length){$("list").innerHTML='<p class="empty">No channels match. Try another genre or clear the search.</p>';}
 more();
}
function dot(c){
 var a=S.alive[c.id];
 return '<span class="dot '+(a===1?"ok":a===0?"bad":"")+'" title="'+(a===1?"Alive":a===0?"Dead":"Not checked")+'"></span>';
}
function favBtn(c){
 var on=S.fav[c.id];
 return '<button class="ico'+(on?" on":"")+'" data-act="fav" title="Favorite" aria-label="Favorite">'+(on?"\u2605":"\u2606")+'</button>';
}
function item(c){
 var tag='<button class="ico" data-act="tag" title="Change genre">Tag</button>';
 var play='<button class="play" data-act="play">Play</button>';
 if(MODE==="lite"){
  return '<div class="row" data-id="'+c.id+'">'+dot(c)+'<span class="nm" title="'+esc(c.n)+'">'+esc(c.n)+'</span><span class="gn">'+esc(c.g.join(", "))+'</span>'+favBtn(c)+tag+play+'</div>';
 }
 var lg=c.l?'<img loading="lazy" alt="" src="/logo?id='+c.id+'">':'<span>'+esc(ini(c.n))+'</span>';
 return '<div class="card" data-id="'+c.id+'"><div class="lg" data-ini="'+esc(ini(c.n))+'">'+lg+'</div><div class="nm" title="'+esc(c.n)+'">'+esc(c.n)+'</div><div class="mt">'+dot(c)+esc(c.g[0]||"")+'</div><div class="acts">'+favBtn(c)+tag+play+'</div></div>';
}
function more(){
 var end=Math.min(S.shown+S.page,S.view.length),h="";
 for(var i=S.shown;i<end;i++)h+=item(S.view[i]);
 $("list").insertAdjacentHTML("beforeend",h);
 S.shown=end;
 $("count").textContent=S.view.length+" channels"+(S.shown<S.view.length?" · showing "+S.shown:"");
 $("more").style.display=S.shown<S.view.length?"block":"none";
}
function up(el,attr,stop){
 while(el&&el!==stop){if(el.getAttribute&&el.getAttribute(attr))return el;el=el.parentNode;}
 return null;
}
function play(id){
 var c=S.byid[id];if(!c)return;
 setStatus("Opening "+c.n+" in VLC…");
 api("/api/play",{id:id}).then(function(r){
  if(r.ok){
   S.recent=[id].concat(S.recent.filter(function(x){return x!==id;})).slice(0,50);
   $("now").textContent="Last played: "+c.n;
   setStatus("Launched in VLC: "+c.n+". If nothing shows up, the stream may be down. Try another channel.");
  }else setStatus(r.error||"Couldn't start VLC.");
 }).catch(netErr);
}
var list=$("list");
list.addEventListener("click",function(e){
 var b=up(e.target,"data-act",list);if(!b)return;
 var row=up(b,"data-id",list);if(!row)return;
 var id=row.getAttribute("data-id"),act=b.getAttribute("data-act");
 if(act==="play")play(id);
 else if(act==="fav"){
  api("/api/fav",{id:id}).then(function(r){
   S.fav=toSet(r.fav);var on=!!S.fav[id];
   b.className="ico"+(on?" on":"");b.textContent=on?"\u2605":"\u2606";
  }).catch(netErr);
 }else if(act==="tag"){
  var c=S.byid[id];
  var t=prompt("Genre for this channel (leave empty to reset):",c?c.g[0]:"");
  if(t===null)return;
  api("/api/tag",{id:id,tag:t}).then(function(){load();}).catch(netErr);
 }
});
list.addEventListener("dblclick",function(e){
 if(up(e.target,"data-act",list))return;
 var row=up(e.target,"data-id",list);if(row)play(row.getAttribute("data-id"));
});
list.addEventListener("error",function(e){
 var t=e.target;
 if(t&&t.tagName==="IMG"&&t.parentNode){var p=t.parentNode;p.innerHTML="<span>"+esc(p.getAttribute("data-ini")||"")+"</span>";}
},true);

function paint(id,v){
 var el=list.querySelector('[data-id="'+id+'"]');if(!el)return;
 if(v===0&&$("hideDead").checked){el.parentNode.removeChild(el);return;}
 var d=el.querySelector(".dot");
 if(d){d.className="dot "+(v?"ok":"bad");d.title=v?"Alive":"Dead";}
}
function poll(){
 api("/api/check/status?since="+S.since).then(function(r){
  r.log.forEach(function(p){S.alive[p[0]]=p[1];paint(p[0],p[1]);});
  S.since=r.next;
  setStatus((r.running?"Checking ":"Checked ")+r.done+" of "+r.total+" · "+r.alive+" alive"+(r.running?"":". Use Hide dead to clean up the list."));
  if(r.running)setTimeout(poll,1200);
  else{S.checking=false;$("check").textContent="Check links";}
 }).catch(function(){S.checking=false;$("check").textContent="Check links";netErr();});
}
$("check").addEventListener("click",function(e){
 if(S.checking){api("/api/check/stop",{});return;}
 var ids=S.view.map(function(c){return c.id;});
 if(!ids.length)return;
 if(ids.length>1500&&!confirm("Check "+ids.length+" channels? This can take several minutes.\nTip: filter by genre first to check fewer."))return;
 api("/api/check",{ids:ids,force:!!e.shiftKey}).then(function(r){
  if(r.error){setStatus(r.error);return;}
  if(!r.total){setStatus("These channels were checked in the last 12 hours. Shift-click Check links to check again.");return;}
  S.checking=true;S.since=0;$("check").textContent="Stop checking";poll();
 }).catch(netErr);
});
var tm=null;
$("q").addEventListener("input",function(){clearTimeout(tm);tm=setTimeout(applyFilter,150);});
["genre","favOnly","recentOnly","hideDead"].forEach(function(id){$(id).addEventListener("change",applyFilter);});
function loadSources(want){
 return api("/api/sources").then(function(d){
  var h="";
  d.sources.forEach(function(s){h+='<option value="'+esc(s.id)+'">'+esc(s.name)+'</option>';});
  $("src").innerHTML=h;
  var ok=d.sources.filter(function(s){return s.id===want;}).length>0;
  S.src=ok?want:(d.sources.length?d.sources[0].id:"");
  $("src").value=S.src;
  $("rmList").style.display=S.src?"":"none";
  if(S.src){try{localStorage.setItem("iptvSrc",S.src);}catch(e){}}
 });
}
$("src").addEventListener("change",function(){
 loadSources(this.value).then(function(){$("genre").value="";load();}).catch(netErr);
});
$("addList").addEventListener("click",function(){
 var loc=prompt("Playlist link (http/https) or full path of an .m3u file:");
 if(!loc||!loc.trim())return;
 var name=prompt("Name for this list (optional):","");
 if(name===null)return;
 setStatus("Adding list…");
 api("/api/source/add",{loc:loc.trim(),name:name.trim()}).then(function(r){
  if(r.error){setStatus(r.error);return;}
  return loadSources(r.id).then(function(){$("genre").value="";load();});
 }).catch(netErr);
});
$("rmList").addEventListener("click",function(){
 var opt=$("src").options[$("src").selectedIndex];
 if(!confirm('Remove the list "'+(opt?opt.text:"")+'"? Your favorites stay saved.'))return;
 api("/api/source/remove",{id:S.src}).then(function(r){
  if(r.error){setStatus(r.error);return;}
  return loadSources("us").then(function(){$("genre").value="";load();});
 }).catch(netErr);
});
$("refresh").addEventListener("click",function(){
 setStatus("Downloading the latest list…");
 api("/api/refresh",{src:S.src}).then(function(r){if(r.error){setStatus(r.error);return;}load();}).catch(netErr);
});
$("moreBtn").addEventListener("click",more);
var last="us";
try{last=localStorage.getItem("iptvSrc")||"us";}catch(e){}
loadSources(last).then(load).catch(netErr);
api("/api/check/status?since=0").then(function(r){
 if(r.running){S.checking=true;S.since=0;$("check").textContent="Stop checking";poll();}
}).catch(function(){});
})();
</script>
</body></html>
"""


BROWSERS = ["firefox", "chromium", "chromium-browser", "google-chrome",
            "google-chrome-stable", "brave-browser", "microsoft-edge", "epiphany", "midori"]


def open_browser(url, wanted=None):
    """Open url in a browser that is actually installed. Never raises."""
    try:
        if os.name == "posix" and not sys.platform.startswith("linux"):
            if webbrowser.open(url):
                return True
        names = ([wanted] if wanted else []) + BROWSERS
        for n in names:
            exe = shutil.which(n)
            if not exe:
                continue
            kw = {"start_new_session": True} if os.name == "posix" else {}
            subprocess.Popen([exe, url], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
            return True
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    print("Couldn't find a browser to open automatically.")
    print("Open this address in any browser: " + url)
    return False


def main():
    ap = argparse.ArgumentParser(description="Browse and play IPTV playlists in VLC.")
    ap.add_argument("--browser", help="browser command to use, for example firefox")
    ap.add_argument("--list", action="append", metavar="LINK_OR_FILE",
                    help="add a playlist (web link or .m3u file path); can be repeated")
    ap.add_argument("--lite", action="store_true", help="text-only interface, no logos")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args()

    for loc in args.list or []:
        r = add_source("", loc)
        print(("Added list: %s" % loc) if "id" in r else ("Couldn't add %s: %s" % (loc, r["error"])))

    srv = None
    port = args.port
    for p in range(args.port, args.port + 20):
        try:
            srv = Server(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit("No free port found near %d." % args.port)
    srv.mode = "lite" if args.lite else "full"

    url = "http://127.0.0.1:%d/" % port
    print("IPTV Browser (%s mode) running at %s" % (srv.mode, url))
    print("Data folder: %s" % DATA)
    if not find_vlc():
        print("Note: VLC wasn't found. Install it, or set VLC_PATH.")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: open_browser(url, args.browser)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.save()
        srv.server_close()


if __name__ == "__main__":
    main()