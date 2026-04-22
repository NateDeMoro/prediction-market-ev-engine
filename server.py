#!/usr/bin/env python3
"""
Local Arbitrage Dashboard server.

Serves index.html and syncs bet state across devices on the LAN via a single
state.json file on the host machine. Last-write-wins; simultaneous edits
from two devices are not reconciled.

Run:    python3 server.py
Access: http://localhost:8080 (this machine)
        http://<lan-ip>:8080   (other devices on same WiFi)
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(DIR, 'state.json')
PORT = 8080

CTYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js':   'application/javascript; charset=utf-8',
    '.css':  'text/css; charset=utf-8',
    '.json': 'application/json',
    '.png':  'image/png',
    '.svg':  'image/svg+xml',
    '.ico':  'image/x-icon',
}


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/state':
            data = b'{}'
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, 'rb') as f:
                    data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return

        if path in ('/', ''):
            path = '/index.html'

        # Prevent directory traversal
        safe = os.path.normpath(path).lstrip(os.sep)
        filepath = os.path.join(DIR, safe)
        if not filepath.startswith(DIR) or not os.path.isfile(filepath):
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        ext = os.path.splitext(filepath)[1].lower()
        ctype = CTYPES.get(ext, 'application/octet-stream')
        with open(filepath, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != '/state':
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        if length > 5_000_000:
            self.send_response(413)
            self._cors()
            self.end_headers()
            return
        body = self.rfile.read(length)
        try:
            state = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self._cors()
            self.end_headers()
            return
        # Atomic write
        tmp = STATE_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_PATH)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self._cors()
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        # Quiet the default access log
        pass


def get_lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


if __name__ == '__main__':
    ip = get_lan_ip()
    print(f'Arbitrage Dashboard:  http://localhost:{PORT}')
    if ip:
        print(f'LAN (laptop, etc.):   http://{ip}:{PORT}')
    print(f'State file:           {STATE_PATH}')
    print('Ctrl+C to stop.')
    try:
        ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        sys.exit(0)
