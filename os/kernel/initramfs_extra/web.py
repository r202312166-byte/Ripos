# web.py -- minimal HTTP client for Ripos (Phase 2).
#
# Plain-HTTP only (no TLS): GET with Host + Connection: close, then
# Content-Length / chunked / close-delimited body handling.  Returns
# (status, headers, body-bytes) so the browser can render.

import socket

class HttpError(Exception):
    pass


def _parse_url(url):
    url = url.strip()
    if url.startswith("http://"):
        rest = url[len("http://"):]
    elif url.startswith("https://"):
        raise HttpError("https not supported (no TLS); use http://")
    else:
        rest = url
    hostport, slash, path = rest.partition("/")
    if ":" in hostport:
        host, _, port = hostport.partition(":")
        port = int(port)
    else:
        host, port = hostport, 80
    if not path.startswith("/"):
        path = "/" + path
    return host, port, path


def _decode_chunked(reader):
    body = b""
    while True:
        line = reader.readline().strip()
        if not line:
            break
        size = int(line.split(b";")[0], 16)
        if size == 0:
            # trailer headers until blank line
            while True:
                tl = reader.readline()
                if tl in (b"", b"\r\n", b"\n"):
                    break
            break
        body += reader.read(size)
        reader.readline()  # CRLF after chunk
    return body


def get(url, timeout_ms=20000):
    """GET url; returns (status_code, headers_dict, body_bytes)."""
    host, port, path = _parse_url(url)
    sock = socket.create_connection((host, port))
    try:
        request = ("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n"
                   "User-Agent: Ripos/0.1\r\n\r\n") % (path, host)
        sock.sendall(request.encode("latin-1"))
        reader = sock.makefile()
        status_line = reader.readline().decode("latin-1", "replace").strip()
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        headers = {}
        while True:
            line = reader.readline()
            if line in (b"", b"\r\n", b"\n"):
                break
            name, _, value = line.decode("latin-1", "replace").partition(":")
            headers[name.strip().lower()] = value.strip()
        cl = headers.get("content-length")
        if cl is not None and cl.isdigit() and int(cl) >= 0:
            body = reader.read(int(cl))
        elif headers.get("transfer-encoding", "").lower() == "chunked":
            body = _decode_chunked(reader)
        else:
            body = reader.read()
        return status, headers, body
    finally:
        try:
            sock.close()
        except Exception:
            pass


def fetch(url, timeout_ms=20000):
    """Convenience: raise on non-2xx, return body bytes."""
    status, headers, body = get(url, timeout_ms)
    if not (200 <= status < 300):
        raise HttpError("HTTP %d for %s" % (status, url))
    return body
