"""Per-account HTTP proxies and a loopback authentication relay for Chromium."""
from __future__ import annotations

import base64
import select
import socket
import socketserver
import threading
from urllib.parse import quote, urlsplit


def normalize_server(value: str) -> str:
    """Accept only an HTTP proxy origin; credentials use separate fields."""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port if parsed.port is not None else 80
        host = parsed.hostname
        if (parsed.scheme != "http" or not host or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or not 1 <= port <= 65535
                or any(c.isspace() or ord(c) < 32 for c in value)
                or any(c in host for c in "/\\@%")):
            raise ValueError
        host = f"[{host}]" if ":" in host else host.encode("idna").decode("ascii")
        return f"http://{host}:{port}"
    except (ValueError, UnicodeError):
        raise ValueError("Use http://host:port; enter username and password separately") from None


def public_proxy(proxy: dict | None) -> dict | None:
    if not proxy:
        return None
    return {"server": proxy["server"], "username": proxy.get("username", ""),
            "has_password": bool(proxy.get("password"))}


def request_proxies(proxy: dict | None) -> dict:
    if not proxy:
        return {}
    parsed = urlsplit(proxy["server"])
    auth = ""
    if proxy.get("username") or proxy.get("password"):
        auth = quote(proxy.get("username", ""), safe="") + ":" + quote(proxy.get("password", ""), safe="") + "@"
    url = f"http://{auth}{parsed.netloc}"
    return {"proxies": {"http": url, "https": url}}


class _RelayServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        # Socket errors must never print headers or credentials.
        pass


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        relay = self.server.relay
        client = self.request
        upstream = None
        try:
            if not relay.track(client):
                return
            client.settimeout(15)
            # Read exactly through the header boundary: TLS bytes after CONNECT
            # must remain available for the bidirectional tunnel.
            head = bytearray()
            while not head.endswith(b"\r\n\r\n"):
                byte = client.recv(1)
                if not byte:
                    return
                head.extend(byte)
                if len(head) > 65536:
                    return
            lines = bytes(head[:-4]).split(b"\r\n")
            method, target, version = lines[0].split(b" ", 2)
            if method != b"CONNECT" and urlsplit(target.decode("ascii")).scheme != "http":
                return
            upstream = socket.create_connection(relay.address, timeout=15)
            if not relay.track(upstream):
                return
            headers = [line for line in lines[1:]
                       if line.split(b":", 1)[0].lower() not in
                       (b"proxy-authorization", b"proxy-connection", b"connection")]
            if relay.authorization:
                headers.append(b"Proxy-Authorization: " + relay.authorization)
            if method != b"CONNECT":
                # Each plain HTTP connection gets its own authenticated request.
                headers.append(b"Connection: close")
            upstream.sendall(b"\r\n".join([lines[0], *headers]) + b"\r\n\r\n")
            client.settimeout(30)
            upstream.settimeout(30)
            while not relay.closed:
                readable, _, _ = select.select([client, upstream], [], [], 1)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)
        except (OSError, ValueError, UnicodeError):
            # A failed proxy must never trigger a direct connection.
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            for sock in (client, upstream):
                if sock:
                    relay.untrack(sock)
                    sock.close()


class HTTPProxyRelay:
    """Forward Chrome's requests to one authenticated HTTP proxy, never direct."""
    def __init__(self, proxy: dict):
        parsed = urlsplit(normalize_server(proxy["server"]))
        self.address = (parsed.hostname, parsed.port)
        self.authorization = None
        if proxy.get("username") or proxy.get("password"):
            credentials = (proxy.get("username", "") + ":" + proxy.get("password", "")).encode("latin-1")
            self.authorization = b"Basic " + base64.b64encode(credentials)
        self.closed = False
        self._sockets = set()
        self._lock = threading.Lock()
        self.server = _RelayServer(("127.0.0.1", 0), _RelayHandler)
        self.server.relay = self
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def track(self, sock):
        with self._lock:
            if self.closed:
                sock.close()
                return False
            self._sockets.add(sock)
            return True

    def untrack(self, sock):
        with self._lock:
            self._sockets.discard(sock)

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            for sock in self._sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            self._sockets.clear()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
