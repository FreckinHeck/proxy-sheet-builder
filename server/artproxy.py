#!/usr/bin/env python3
"""
artproxy — a small CORS shim for the proxy sheet builder.

Browsers refuse to let a page read pixels from an image whose host sends no
CORS headers, which is why Google Drive and the MPC Autofill CDN can't be used
directly for PDF export. Server-side fetches have no such restriction, so this
fetches the file and re-serves it with a permissive header.

Endpoints
  GET /health                  -> {"ok": true}
  GET /img?u=<encoded url>     -> the image bytes, with CORS
  GET /drive?id=<file id>      -> a Google Drive file by id, with CORS
  GET /api/<path>              -> proxied to MPC_BACKEND, with CORS

Only hosts in ALLOWED_HOSTS are fetched. Keep this on your LAN or tailnet —
an open fetch-any-URL service is not something to expose publicly.
"""

import hashlib
import json
import os
import re
import socketserver
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", "8778"))
MPC_BACKEND = os.environ.get("MPC_BACKEND", "https://mpcfill.com").rstrip("/")
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(40 * 1024 * 1024)))
TIMEOUT = int(os.environ.get("TIMEOUT", "30"))
CACHE_DIR = os.environ.get("CACHE_DIR", "/cache")
CACHE_MAX_MB = int(os.environ.get("CACHE_MAX_MB", "4096"))
# When set, every request must carry ?k=<token>. Required if this is reachable
# from the internet — otherwise anyone can spend your bandwidth and your rate
# limits with Google Drive.
TOKEN = os.environ.get("PROXY_TOKEN", "").strip()
# Separate from PROXY_TOKEN, which is readable in the page source. Only this one
# can list or read submitted orders.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
ORDERS_DIR = os.environ.get("ORDERS_DIR", "/orders")
ORDER_MAX_BYTES = int(os.environ.get("ORDER_MAX_BYTES", str(512 * 1024)))
ORDER_MAX_COUNT = int(os.environ.get("ORDER_MAX_COUNT", "2000"))
WEBHOOK = os.environ.get("WEBHOOK_URL", "").strip()
# Google Drive gets unhappy with parallel hammering; keep upstream fetches few.
UPSTREAM = threading.Semaphore(int(os.environ.get("CONCURRENCY", "4")))
# Thumbnails are tiny, so they do not need the full-file budget.
THUMB_UPSTREAM = threading.Semaphore(int(os.environ.get("THUMB_CONCURRENCY", "12")))
THUMB_DEFAULT = int(os.environ.get("THUMB_WIDTH", "400"))
THUMB_MAX = int(os.environ.get("THUMB_MAX", "900"))
THUMB_QUALITY = int(os.environ.get("THUMB_QUALITY", "82"))

# Pillow is optional: without it /thumb just serves the original file, so the
# proxy still works, it just does not save any bandwidth.
try:
    from PIL import Image  # type: ignore
    import io
    HAVE_PIL = True
except Exception:  # pragma: no cover - depends on the image
    HAVE_PIL = False

ALLOWED_HOSTS = {
    "drive.google.com",
    "drive.usercontent.google.com",
    "docs.google.com",
    "lh3.googleusercontent.com",
    "cdn.mpcautofill.com",
    "img.mpcautofill.com",
    "mpcautofill.com",
    "mpcfill.com",
    "mpcautofill.github.io",
    "api.scryfall.com",
    "cards.scryfall.io",
}
ALLOWED_SUFFIXES = (".googleusercontent.com", ".mpcautofill.com", ".mpcfill.com", ".scryfall.io")

OK_TYPES = ("image/", "application/json", "text/json")
# mpcfill.com sits behind Cloudflare, which is happier with a browser-shaped agent.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)


def host_allowed(host: str) -> bool:
    host = (host or "").lower().split(":")[0]
    return host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES)


def drive_url(file_id: str) -> str:
    # Direct-download form for a publicly shared Drive file.
    return "https://drive.google.com/uc?export=download&id=" + urllib.parse.quote(file_id, safe="")


def drive_thumb_url(file_id: str, width: int) -> str:
    """Drive renders and serves its own thumbnails. Asking for one costs a few
    tens of kilobytes instead of pulling the whole 5-15 MB original down just to
    shrink it — the difference between a cold art window taking minutes and
    taking seconds."""
    return ("https://drive.google.com/thumbnail?id=%s&sz=w%d"
            % (urllib.parse.quote(file_id, safe=""), max(80, min(width, 1600))))


def cache_path(key: str) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()
    return os.path.join(CACHE_DIR, h[:2], h)


def cache_get(key):
    p = cache_path(key)
    try:
        with open(p + ".meta", "r") as f:
            ctype = f.read().strip()
        with open(p, "rb") as f:
            return f.read(), ctype
    except OSError:
        return None


def cache_put(key, data, ctype):
    p = cache_path(key)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
        with open(p + ".meta", "w") as f:
            f.write(ctype)
    except OSError as e:
        print("cache write failed: %s" % e, file=sys.stderr)


def cache_trim():
    """Drop least-recently-used files when the cache outgrows its budget."""
    entries = []
    total = 0
    for root, _dirs, files in os.walk(CACHE_DIR):
        for name in files:
            p = os.path.join(root, name)
            try:
                size = os.path.getsize(p)
                entries.append((os.path.getatime(p), size, p))
                total += size
            except OSError:
                pass
    budget = CACHE_MAX_MB * 1024 * 1024
    if total <= budget:
        return
    entries.sort()
    for _atime, size, p in entries:
        if total <= budget * 0.8:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def order_id():
    import time
    return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), os.urandom(3).hex())


def order_count():
    try:
        return len([n for n in os.listdir(ORDERS_DIR) if n.endswith(".json")])
    except OSError:
        return 0


def notify(summary):
    """Best-effort ping. The webhook url is set by the operator, so it is not
    subject to the fetch allowlist."""
    if not WEBHOOK:
        return
    try:
        payload = json.dumps({"content": summary}).encode()
        req = urllib.request.Request(
            WEBHOOK, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        urllib.request.urlopen(req, timeout=10).read(1000)
    except Exception as e:
        print("webhook failed: %s" % e, file=sys.stderr)


def make_thumb(data, width):
    """Downscale to `width` px wide, flattening transparency onto black so the
    rounded-corner PNGs match the square-cornered JPEGs."""
    if not HAVE_PIL:
        return None, None
    try:
        im = Image.open(io.BytesIO(data))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, (0, 0, 0))
            flat.paste(im, mask=im.split()[-1])
            im = flat
        else:
            im = im.convert("RGB")
        if im.width > width:
            height = max(1, round(im.height * width / im.width))
            im = im.resize((width, height), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print("thumb failed: %s" % e, file=sys.stderr)
        return None, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "artproxy"

    # ---- helpers -------------------------------------------------------
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")

    def fail(self, code, message):
        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, content_type, cache=True):
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def authorised(self, params):
        if not TOKEN:
            return True
        given = (params.get("k") or [""])[0] or self.headers.get("X-Proxy-Token", "")
        return given == TOKEN

    def fetch(self, url, data=None, content_type=None):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("only http and https are fetched")
        if not host_allowed(parsed.netloc):
            raise ValueError("host not allowed: " + parsed.netloc)
        headers = {"User-Agent": UA, "Accept": "*/*"}
        if data is not None:
            headers["Content-Type"] = content_type or "application/json"
            headers["Origin"] = "%s://%s" % (parsed.scheme, parsed.netloc)
            headers["Referer"] = "%s://%s/editor" % (parsed.scheme, parsed.netloc)
        req = urllib.request.Request(url, data=data, headers=headers)
        with UPSTREAM:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                ctype = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
                body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("file larger than the %d byte limit" % MAX_BYTES)
        return body, ctype

    def fetch_light(self, url):
        """Small fetch on the thumbnail lane, no size ceiling concerns."""
        parsed = urllib.parse.urlparse(url)
        if not host_allowed(parsed.netloc):
            raise ValueError("host not allowed: " + parsed.netloc)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*"})
        with THUMB_UPSTREAM:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
                body = resp.read(8 * 1024 * 1024)
        return body, ctype

    def fetch_image_cached(self, url):
        """Images are immutable here, so a disk cache saves repeat downloads."""
        hit = cache_get(url)
        if hit:
            return hit[0], hit[1], True
        data, ctype = self.fetch(url)
        if ctype.startswith("image/"):
            cache_put(url, data, ctype)
            threading.Thread(target=cache_trim, daemon=True).start()
        return data, ctype, False

    # ---- routes --------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"

        if route == "/order":
            if not self.authorised(params):
                return self.fail(401, "missing or wrong access token")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > ORDER_MAX_BYTES:
                return self.fail(413, "order must be between 1 and %d bytes" % ORDER_MAX_BYTES)
            if order_count() >= ORDER_MAX_COUNT:
                return self.fail(507, "the order folder is full")
            raw = self.rfile.read(length)
            try:
                order = json.loads(raw)
                if not isinstance(order, dict):
                    raise ValueError("expected an object")
            except Exception as e:
                return self.fail(400, "could not read that order: %s" % e)

            # Editing an existing order: the caller must present its key.
            want_id = (params.get("id") or [""])[0]
            want_key = (params.get("key") or [""])[0]
            existing = None
            if want_id:
                if not re.fullmatch(r"[0-9A-Za-z-]{1,60}", want_id):
                    return self.fail(400, "bad order id")
                try:
                    with open(os.path.join(ORDERS_DIR, want_id + ".json")) as f:
                        existing = json.load(f)
                except OSError:
                    return self.fail(404, "no such order")
                if not want_key or want_key != existing.get("edit_key"):
                    return self.fail(403, "that edit link is not valid for this order")

            now = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
            if existing:
                oid = want_id
                order["id"] = oid
                order["edit_key"] = existing["edit_key"]
                order["received"] = existing.get("received", now)
                order["updated"] = now
                order["status"] = existing.get("status", "new")
                if existing.get("admin_note"):
                    order["admin_note"] = existing["admin_note"]
            else:
                oid = order_id()
                order["id"] = oid
                # 96 bits: enough that an edit link cannot be guessed.
                order["edit_key"] = os.urandom(12).hex()
                order["received"] = now
                order.setdefault("status", "new")
            order["from_ip"] = self.address_string()

            try:
                os.makedirs(ORDERS_DIR, exist_ok=True)
                with open(os.path.join(ORDERS_DIR, oid + ".json"), "w") as f:
                    json.dump(order, f, indent=2)
            except OSError as e:
                return self.fail(500, "could not save the order: %s" % e)

            who = str(order.get("name") or "someone")[:60]
            cards = order.get("totalCards") or len(order.get("cards") or [])
            finish = str(order.get("finish") or "")[:40]
            paper = str(order.get("paper") or "")[:20]
            threading.Thread(
                target=notify,
                args=("%s proxy order %s from %s — %s cards, %s%s"
                      % ("Updated" if existing else "New", oid, who, cards, finish,
                         (", " + paper + " core") if paper else ""),),
                daemon=True,
            ).start()

            body = json.dumps({"ok": True, "id": oid,
                               "key": order["edit_key"]}).encode()
            return self.send_bytes(body, "application/json", cache=False)

        if route == "/order/status":
            admin = (params.get("a") or [""])[0] or self.headers.get("X-Admin-Token", "")
            if not ADMIN_TOKEN or admin != ADMIN_TOKEN:
                return self.fail(401, "admin token required")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                change = json.loads(self.rfile.read(length))
            except Exception as e:
                return self.fail(400, "could not read that request: %s" % e)
            oid = str(change.get("id") or "")
            status = str(change.get("status") or "").lower()
            if not re.fullmatch(r"[0-9A-Za-z-]{1,60}", oid):
                return self.fail(400, "bad order id")
            if status not in ("new", "in_progress", "done"):
                return self.fail(400, "status must be new, in_progress or done")
            path = os.path.join(ORDERS_DIR, oid + ".json")
            try:
                with open(path) as f:
                    order = json.load(f)
            except OSError:
                return self.fail(404, "no such order")
            order["status"] = status
            order["status_changed"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
            if "note" in change:
                order["admin_note"] = str(change.get("note") or "")[:500]
            try:
                with open(path, "w") as f:
                    json.dump(order, f, indent=2)
            except OSError as e:
                return self.fail(500, "could not save: %s" % e)
            body = json.dumps({"ok": True, "id": oid, "status": status}).encode()
            return self.send_bytes(body, "application/json", cache=False)

        if not parsed.path.startswith("/api"):
            return self.fail(404, "only /api, /order and /order/status accept POST")
        if not self.authorised(params):
            return self.fail(401, "missing or wrong access token")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""
        tail = self.path[len("/api"):] or "/"
        ctype = self.headers.get("Content-Type", "application/json")
        try:
            data, upstream_type = self.fetch(MPC_BACKEND + tail, data=body, content_type=ctype)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(2000).decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (403, 503) and "cloudflare" in detail.lower():
                return self.fail(e.code, "Cloudflare blocked the proxy's request to the backend")
            return self.fail(e.code, "backend said %s for %s: %s" % (e.code, tail, detail[:300]))
        except Exception as e:
            return self.fail(502, "fetch failed: %s" % e)
        return self.send_bytes(data, upstream_type, cache=False)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/health"):
            body = json.dumps({"ok": True, "backend": MPC_BACKEND,
                               "auth": bool(TOKEN), "thumbs": HAVE_PIL}).encode()
            return self.send_bytes(body, "application/json", cache=False)

        if route == "/order":
            # Anyone holding the order's own key may read it back. No admin
            # token, and nothing about anyone else's orders is reachable.
            oid = (params.get("id") or [""])[0]
            key = (params.get("key") or [""])[0]
            if not re.fullmatch(r"[0-9A-Za-z-]{1,60}", oid or ""):
                return self.fail(400, "bad order id")
            try:
                with open(os.path.join(ORDERS_DIR, oid + ".json")) as f:
                    order = json.load(f)
            except OSError:
                return self.fail(404, "no such order")
            if not key or key != order.get("edit_key"):
                return self.fail(403, "that edit link is not valid for this order")
            # The submitter has no business seeing these.
            order.pop("from_ip", None)
            order.pop("admin_note", None)
            return self.send_bytes(json.dumps(order).encode(),
                                   "application/json", cache=False)

        if route == "/orders":
            admin = (params.get("a") or [""])[0] or self.headers.get("X-Admin-Token", "")
            if not ADMIN_TOKEN or admin != ADMIN_TOKEN:
                return self.fail(401, "admin token required")
            wanted = (params.get("id") or [""])[0]
            if wanted:
                if not re.fullmatch(r"[0-9A-Za-z-]{1,60}", wanted):
                    return self.fail(400, "bad order id")
                try:
                    with open(os.path.join(ORDERS_DIR, wanted + ".json"), "rb") as f:
                        return self.send_bytes(f.read(), "application/json", cache=False)
                except OSError:
                    return self.fail(404, "no such order")
            try:
                names = sorted(n for n in os.listdir(ORDERS_DIR) if n.endswith(".json"))
            except OSError:
                names = []
            summary = []
            for n in names[-100:]:
                try:
                    with open(os.path.join(ORDERS_DIR, n)) as f:
                        o = json.load(f)
                    summary.append({"id": o.get("id") or n[:-5], "name": o.get("name"),
                                    "received": o.get("received"),
                                    "totalCards": o.get("totalCards"),
                                    "finish": o.get("finish"),
                                    "paper": o.get("paper"),
                                    "status": o.get("status") or "new",
                                    "adminNote": o.get("admin_note") or ""})
                except Exception:
                    summary.append({"id": n[:-5], "name": "(unreadable)"})
            body = json.dumps({"orders": list(reversed(summary))}).encode()
            return self.send_bytes(body, "application/json", cache=False)

        if not self.authorised(params):
            return self.fail(401, "missing or wrong access token")

        if route == "/img":
            url = (params.get("u") or [""])[0]
            if not url:
                return self.fail(400, "pass the image url as ?u=")
            m = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", url)
            if m:
                url = drive_url(m.group(1))
            try:
                data, ctype, hit = self.fetch_image_cached(url)
            except ValueError as e:
                return self.fail(403, str(e))
            except urllib.error.HTTPError as e:
                return self.fail(e.code, "upstream said %s" % e.code)
            except Exception as e:
                return self.fail(502, "fetch failed: %s" % e)
            if not ctype.startswith("image/"):
                return self.fail(415, "that url returned %s, not an image" % ctype)
            return self.send_bytes(data, ctype)

        if route == "/thumb":
            fid = (params.get("id") or [""])[0]
            src = (params.get("u") or [""])[0]
            try:
                width = int((params.get("w") or [str(THUMB_DEFAULT)])[0])
            except ValueError:
                width = THUMB_DEFAULT
            width = max(80, min(width, THUMB_MAX))
            if fid:
                if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", fid):
                    return self.fail(400, "bad Google Drive file id")
                url = drive_url(fid)
            elif src:
                m = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", src)
                url = drive_url(m.group(1)) if m else src
            else:
                return self.fail(400, "pass ?id= or ?u=")

            key = "thumb:%d:%s" % (width, url)
            hit = cache_get(key)
            if hit:
                return self.send_bytes(hit[0], hit[1])

            # Fast path: let Drive render the thumbnail. Falls through to the
            # full download if it declines or hands back something that is not
            # an image (an unshared file returns an HTML notice).
            if fid:
                try:
                    body, ctype = self.fetch_light(drive_thumb_url(fid, width))
                    if ctype.startswith("image/") and len(body) > 512:
                        small, small_type = make_thumb(body, width)
                        if small is None:
                            small, small_type = body, ctype
                        cache_put(key, small, small_type)
                        threading.Thread(target=cache_trim, daemon=True).start()
                        return self.send_bytes(small, small_type)
                except Exception as e:
                    print("drive thumbnail miss for %s: %s" % (fid, e), file=sys.stderr)

            try:
                data, ctype, _cached = self.fetch_image_cached(url)
            except ValueError as e:
                return self.fail(403, str(e))
            except urllib.error.HTTPError as e:
                return self.fail(e.code, "upstream said %s" % e.code)
            except Exception as e:
                return self.fail(502, "fetch failed: %s" % e)
            if not ctype.startswith("image/"):
                return self.fail(415, "that url returned %s, not an image" % ctype)
            small, small_type = make_thumb(data, width)
            if small is None:
                # No Pillow, or an image it could not read: serve the original.
                return self.send_bytes(data, ctype)
            cache_put(key, small, small_type)
            threading.Thread(target=cache_trim, daemon=True).start()
            return self.send_bytes(small, small_type)

        if route == "/drive":
            fid = (params.get("id") or [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", fid or ""):
                return self.fail(400, "pass a Google Drive file id as ?id=")
            try:
                data, ctype, hit = self.fetch_image_cached(drive_url(fid))
            except urllib.error.HTTPError as e:
                return self.fail(e.code, "Drive said %s — is the file shared publicly?" % e.code)
            except Exception as e:
                return self.fail(502, "fetch failed: %s" % e)
            if not ctype.startswith("image/"):
                return self.fail(
                    415,
                    "Drive returned %s, not an image. Large files show a confirmation "
                    "page instead of the file; try a smaller one or a direct CDN url." % ctype,
                )
            return self.send_bytes(data, ctype)

        if route.startswith("/api"):
            tail = self.path[len("/api"):] or "/"
            try:
                data, ctype = self.fetch(MPC_BACKEND + tail)
            except urllib.error.HTTPError as e:
                return self.fail(e.code, "backend said %s for %s" % (e.code, tail))
            except Exception as e:
                return self.fail(502, "fetch failed: %s" % e)
            return self.send_bytes(data, ctype, cache=False)

        return self.fail(404, "no such route")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print("artproxy listening on :%d, backend %s" % (PORT, MPC_BACKEND), file=sys.stderr)
    Server(("0.0.0.0", PORT), Handler).serve_forever()
