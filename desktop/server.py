"""PC의 음악 폴더를 같은 Wi-Fi에 있는 아이폰으로 스트리밍하는 작은 웹 서버.

GUI 없이도 실행할 수 있습니다:  python server.py "C:\\Users\\me\\Music"
"""
import hashlib
import http.cookies
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import mutagen  # 곡 정보(제목/가수/앨범/커버) 읽기 — 없어도 동작함
except ImportError:  # pragma: no cover
    mutagen = None

AUDIO_EXTS = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.alac', '.mp4', '.aif', '.aiff'}
MIME = {'.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.mp4': 'audio/mp4', '.aac': 'audio/aac',
        '.wav': 'audio/wav', '.flac': 'audio/flac', '.alac': 'audio/mp4', '.aif': 'audio/aiff',
        '.aiff': 'audio/aiff'}
CHUNK = 256 * 1024


def resource_path(name):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def config_dir():
    root = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    d = os.path.join(root, 'MusicPlayer')
    os.makedirs(d, exist_ok=True)
    return d


def load_config():
    path = os.path.join(config_dir(), 'config.json')
    cfg = {}
    try:
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg.setdefault('folder', os.path.join(os.path.expanduser('~'), 'Music'))
    cfg.setdefault('port', 8765)
    cfg.setdefault('pin', f'{secrets.randbelow(10000):04d}')
    cfg.setdefault('secret', secrets.token_hex(16))
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(os.path.join(config_dir(), 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def local_ip():
    """같은 네트워크의 폰이 접속할 PC의 IP. (실제로 데이터를 보내지는 않음)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def _first(tags, *keys):
    for k in keys:
        v = tags.get(k) if tags else None
        if v:
            v = v[0] if isinstance(v, list) else v
            return str(v).strip()
    return ''


def parse_name(fname):
    base = os.path.splitext(fname)[0].replace('_', ' ').strip()
    m = re.match(r'^(.+?)\s+-\s+(.+)$', base)
    return (m.group(2).strip(), m.group(1).strip()) if m else (base, '')


class Library:
    def __init__(self, folder):
        self.folder = folder
        self.tracks = {}       # id -> dict
        self.order = []
        self.lock = threading.Lock()
        self.scanned_at = 0

    def scan(self):
        tracks, order = {}, []
        folder = self.folder
        if folder and os.path.isdir(folder):
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in AUDIO_EXTS or f.startswith('.'):
                        continue
                    path = os.path.join(root, f)
                    tid = hashlib.sha1(os.path.relpath(path, folder).encode('utf-8')).hexdigest()[:16]
                    title, artist = parse_name(f)
                    album, has_cover = os.path.basename(root) if root != folder else '', False
                    if mutagen:
                        try:
                            mf = mutagen.File(path, easy=True)
                            if mf is not None and mf.tags:
                                title = _first(mf.tags, 'title') or title
                                artist = _first(mf.tags, 'artist', 'albumartist') or artist
                                album = _first(mf.tags, 'album') or album
                            has_cover = _cover_bytes(path) is not None
                        except Exception:
                            pass
                    tracks[tid] = {'id': tid, 'title': title, 'artist': artist, 'album': album,
                                   'cover': has_cover, 'path': path, 'ext': ext}
                    order.append(tid)
        order.sort(key=lambda i: (tracks[i]['artist'].lower(), tracks[i]['album'].lower(), tracks[i]['title'].lower()))
        with self.lock:
            self.tracks, self.order, self.scanned_at = tracks, order, time.time()
        return len(order)

    def listing(self):
        if time.time() - self.scanned_at > 30:
            self.scan()
        with self.lock:
            return [{k: v for k, v in self.tracks[i].items() if k != 'path'} for i in self.order]

    def get(self, tid):
        with self.lock:
            return self.tracks.get(tid)


def _cover_bytes(path):
    if not mutagen:
        return None
    try:
        mf = mutagen.File(path)
        if mf is None:
            return None
        tags = mf.tags
        # MP3 (ID3)
        if tags is not None and hasattr(tags, 'getall'):
            pics = tags.getall('APIC')
            if pics:
                return pics[0].mime, pics[0].data
        # M4A
        if tags is not None and 'covr' in tags and tags['covr']:
            c = tags['covr'][0]
            return ('image/png' if getattr(c, 'imageformat', 13) == 14 else 'image/jpeg'), bytes(c)
        # FLAC
        if getattr(mf, 'pictures', None):
            p = mf.pictures[0]
            return p.mime, p.data
    except Exception:
        pass
    return None


class MusicServer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.library = Library(cfg['folder'])
        self.httpd = None
        self.thread = None
        self.clients = {}  # ip -> last seen

    @property
    def token(self):
        return hashlib.sha256((self.cfg['secret'] + self.cfg['pin']).encode()).hexdigest()

    def url(self, with_pin=True):
        u = f"http://{local_ip()}:{self.cfg['port']}/"
        return u + (f"?pin={self.cfg['pin']}" if with_pin else '')

    def start(self):
        self.library.folder = self.cfg['folder']
        self.library.scan()
        server = self

        class Handler(_Handler):
            srv = server

        self.httpd = ThreadingHTTPServer(('0.0.0.0', int(self.cfg['port'])), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


class _Handler(BaseHTTPRequestHandler):
    srv: MusicServer = None
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    # ---------- helpers ----------
    def _authed(self):
        c = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
        ok = 'mp_auth' in c and secrets.compare_digest(c['mp_auth'].value.encode(), self.srv.token.encode())
        if ok:
            self.srv.clients[self.client_address[0]] = time.time()
        return ok

    def _send(self, code, body=b'', ctype='text/plain; charset=utf-8', headers=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False), 'application/json; charset=utf-8')

    def _cookie(self):
        return f'mp_auth={self.srv.token}; Max-Age=31536000; Path=/; SameSite=Lax; HttpOnly'

    # ---------- routes ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path.startswith('/icons/'):
            return self._static(resource_path(os.path.join('icons', os.path.basename(path))), 'image/png', cache=True)
        if path == '/manifest.webmanifest':
            return self._static(resource_path('manifest.webmanifest'), 'application/manifest+json')

        if path == '/':
            if 'pin' in q and secrets.compare_digest(q['pin'][0].encode(), self.srv.cfg['pin'].encode()):
                # QR로 들어온 경우: 로그인 처리 후 PIN이 없는 주소로 이동
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Set-Cookie', self._cookie())
                self.send_header('Content-Length', '0')
                self.end_headers()
                self.srv.clients[self.client_address[0]] = time.time()
                return
            if not self._authed():
                return self._static(resource_path('login.html'), 'text/html; charset=utf-8')
            return self._static(resource_path('player.html'), 'text/html; charset=utf-8')

        if not self._authed():
            return self._send(401, 'PIN이 필요합니다')

        if path == '/api/tracks':
            return self._json({'name': socket.gethostname(), 'tracks': self.srv.library.listing()})
        m = re.match(r'^/stream/([0-9a-f]{16})$', path)
        if m:
            t = self.srv.library.get(m.group(1))
            if not t:
                return self._send(404, 'not found')
            return self._file(t['path'], MIME.get(t['ext'], 'application/octet-stream'))
        m = re.match(r'^/cover/([0-9a-f]{16})$', path)
        if m:
            t = self.srv.library.get(m.group(1))
            cb = _cover_bytes(t['path']) if t else None
            if not cb:
                return self._send(404, 'no cover')
            return self._send(200, cb[1], cb[0] or 'image/jpeg', {'Cache-Control': 'max-age=86400'})
        return self._send(404, 'not found')

    def do_POST(self):
        if self.path == '/login':
            n = int(self.headers.get('Content-Length') or 0)
            data = urllib.parse.parse_qs(self.rfile.read(min(n, 1024)).decode('utf-8', 'ignore'))
            pin = (data.get('pin') or [''])[0].strip()
            if secrets.compare_digest(pin.encode(), self.srv.cfg['pin'].encode()):
                self.send_response(303)
                self.send_header('Location', '/')
                self.send_header('Set-Cookie', self._cookie())
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                time.sleep(1)  # 무작위 대입 지연
                self.send_response(303)
                self.send_header('Location', '/?wrong=1')
                self.send_header('Content-Length', '0')
                self.end_headers()
            return
        if self.path == '/api/rescan' and self._authed():
            return self._json({'count': self.srv.library.scan()})
        self._send(404, 'not found')

    def _static(self, fp, ctype, cache=False):
        try:
            with open(fp, 'rb') as f:
                body = f.read()
        except OSError:
            return self._send(404, 'not found')
        self._send(200, body, ctype, {'Cache-Control': 'max-age=86400'} if cache else None)

    def _file(self, fp, ctype):
        """Range 요청 지원 (아이폰 사파리는 오디오에 Range가 꼭 필요)."""
        try:
            size = os.path.getsize(fp)
            f = open(fp, 'rb')
        except OSError:
            return self._send(404, 'not found')
        with f:
            start, end, code = 0, size - 1, 200
            rng = self.headers.get('Range')
            if rng:
                m = re.match(r'bytes=(\d*)-(\d*)', rng)
                if m:
                    a, b = m.groups()
                    if a == '' and b:
                        start = max(0, size - int(b))
                    else:
                        start = int(a or 0)
                        if b:
                            end = min(int(b), size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header('Content-Range', f'bytes */{size}')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    code = 206
            length = end - start + 1
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            if code == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command == 'HEAD':
                return
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass


if __name__ == '__main__':
    cfg = load_config()
    if len(sys.argv) > 1:
        cfg['folder'] = sys.argv[1]
    if len(sys.argv) > 2:
        cfg['port'] = int(sys.argv[2])
    s = MusicServer(cfg)
    s.start()
    print(f"음악 폴더: {cfg['folder']} ({len(s.library.order)}곡)")
    print(f"아이폰에서 열기: {s.url(False)}   PIN: {cfg['pin']}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        s.stop()
