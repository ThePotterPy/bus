"""
Servidor local para el tracker de colectivos JAHA.

Sirve la pagina web (index.html) y actua de proxy hacia la API real de
JAHA (https://www.jaha.com.py/rest_backend), que no permite CORS, por lo
que el navegador no puede llamarla directamente desde una pagina web.

Uso:
    python server.py
    (abre http://localhost:8787 en el navegador)
"""

import json
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 8787
API_BASE = "https://www.jaha.com.py/rest_backend"
STATIC_DIR = Path(__file__).parent / "static"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def call_api(path: str, method: str = "GET", body: dict | None = None) -> tuple[int, bytes]:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        return 502, json.dumps({"success": False, "error": str(e.reason)}).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencia el log por defecto, ya lo hacemos mas abajo

    def _send_json(self, status: int, payload: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, rel_path: str):
        file_path = STATIC_DIR / rel_path
        if not file_path.is_file():
            self.send_error(404, "Not found")
            return
        content_type = "text/html; charset=utf-8"
        if rel_path.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        elif rel_path.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        print(f"GET {parsed.path} {qs}")

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file("index.html")
            return

        if parsed.path == "/api/lines":
            status, body = call_api("/bus/lines")
            self._send_json(status, body)
            return

        if parsed.path == "/api/positions":
            line = qs.get("line", [None])[0]
            if not line:
                self._send_json(400, b'{"success": false, "error": "falta line"}')
                return
            status, body = call_api("/bus/positions", method="POST", body={"line": line})
            self._send_json(status, body)
            return

        if parsed.path == "/api/path":
            line = qs.get("line", [None])[0]
            if not line:
                self._send_json(400, b'{"success": false, "error": "falta line"}')
                return
            status, body = call_api(f"/bus/lineServices/{line}")
            self._send_json(status, body)
            return

        # cualquier otro archivo estatico (por si se agregan mas)
        self._send_file(parsed.path.lstrip("/"))


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Servidor corriendo en http://localhost:{PORT}")
    print("Presiona Ctrl+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo servidor...")
        server.shutdown()


if __name__ == "__main__":
    main()
