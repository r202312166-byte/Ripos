# browser-test.py -- host gate for the Phase 2 Python network layer:
# socket.py shim, web.py HTTP client, and the browser HTML parser.
# kern is stubbed with a fake smoltcp-backed stack that serves canned
# HTTP responses, so the real client code runs end to end on the host.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "kernel", "initramfs_extra", "apps"))

passed = 0
failed = 0


def check(name, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print("browser-test PASS", name, extra)
    else:
        failed += 1
        print("browser-test FAIL", name, extra)


class FakeNet:
    """A fake smoltcp-backed kern.net_*: serves canned HTTP responses."""

    def __init__(self, responses):
        # responses: {(host, port, path): (status_line, headers, body)}
        self.responses = responses
        self._next_fd = 1
        self._conns = {}
        self.requests = []

    def net_ready(self):
        return True

    def net_ip(self):
        return (10, 0, 2, 15)

    def net_mac(self):
        return (0, 12, 29, 10, 20, 30)

    def net_socket(self, is_tcp):
        fd = self._next_fd
        self._next_fd += 1
        self._conns[fd] = {"peer": None, "req": b"", "resp": None}
        return fd

    def net_connect(self, fd, ip, port):
        self._conns[fd]["peer"] = (ip, port)
        return 0

    def net_established(self, fd):
        # socket.connect() now waits on the handshake completing; this
        # fake is always ready immediately.
        return 1

    def net_poll(self, fd, timeout_ms):
        # simulate an instant, always-ready connection (the response is
        # served from net_recv once the request has been sent)
        return 1

    def _build_response(self, conn):
        ip, port = conn["peer"]
        # parse the request line
        lines = conn["req"].split(b"\r\n")
        parts = lines[0].decode("latin-1", "replace").split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        key = None
        for (h, p, pa), val in self.responses.items():
            if pa == path:
                key = (h, p, pa)
                break
        if key is None:
            body = b"404 Not Found"
            head = b"HTTP/1.1 404 Not Found\r\nContent-Length: 13\r\n\r\n"
            return {"left": head + body}
        status, headers, body = self.responses[key]
        headers = dict(headers)
        headers.pop("Content-Length", None)
        headers["Content-Length"] = str(len(body))
        hdr = b"".join((k.encode() + b": " + v.encode() + b"\r\n")
                        for k, v in headers.items())
        head = (status + "\r\n").encode() + hdr + b"\r\n"
        return {"left": head + body}

    def net_send(self, fd, data):
        conn = self._conns[fd]
        conn["req"] += bytes(data)
        self.requests.append(bytes(data))
        return len(bytes(data))

    def net_recv(self, fd, cap=65536):
        conn = self._conns[fd]
        if conn["resp"] is None:
            if not conn["req"]:
                return None
            conn["resp"] = self._build_response(conn)
        if not conn["resp"]["left"]:
            return None
        n = min(cap, len(conn["resp"]["left"]))
        out = conn["resp"]["left"][:n]
        conn["resp"]["left"] = conn["resp"]["left"][n:]
        return out

    def net_close(self, fd):
        self._conns.pop(fd, None)
        return 0

    def audio_ready(self):
        return False

    def audio_play(self, pcm, rate, ch, vol=128):
        return len(pcm)

    def audio_stop(self):
        pass

    def audio_busy(self):
        return 0

    def write(self, s):
        pass

    def tick(self):
        return 0

    def after(self, ms, cb):
        pass

    def on_key(self, cb):
        pass

    def eval(self, src):
        return None


RESP = {
    ("10.0.2.2", 8000, "/"): (
        "HTTP/1.1 200 OK",
        {"Content-Type": "text/html", "Content-Length": "24"},
        b"<h1>Ripos</h1><p>Hello</p>",
    ),
    ("10.0.2.2", 8000, "/chunked"): (
        "HTTP/1.1 200 OK",
        {"Transfer-Encoding": "chunked"},
        b"",
    ),
}

net = FakeNet(RESP)
sys.modules["kern"] = net

import web
import socket as sockmod

# --- URL parsing --------------------------------------------------------
check("parse http url", web._parse_url("http://10.0.2.2:8000/a/b") == ("10.0.2.2", 8000, "/a/b"))
check("parse default port", web._parse_url("http://example.com/x") == ("example.com", 80, "/x"))
check("parse no path", web._parse_url("http://10.0.2.2:8000") == ("10.0.2.2", 8000, "/"))
try:
    web._parse_url("https://x/");
    check("https rejected", False)
except web.HttpError:
    check("https rejected", True)

# --- HTTP GET through the shim + fake stack -----------------------------
status, headers, body = web.get("http://10.0.2.2:8000/")
check("get status", status == 200, str(status))
check("get body", body == b"<h1>Ripos</h1><p>Hello</p>", repr(body))
check("get headers", headers.get("content-type") == "text/html")
check("request has Host", any(b"Host: 10.0.2.2" in r for r in net.requests))

# --- socket shim basics ------------------------------------------------
s = sockmod.socket()
check("socket fd", isinstance(s.fd, int))
s.connect(("10.0.2.2", 8000))
n = s.sendall(b"GET / HTTP/1.0\r\n\r\n")
check("sendall", n is None, str(n))
check("recv_ready", s.recv_ready(100))
s.close()

# --- HTML parser --------------------------------------------------------
import browser  # noqa: E402
p = browser._Parser()
p.feed("<title>T</title><h1>Head</h1><p>Some <b>text</b> here.</p><a href='/x'>Link</a><img src='/i.png'>")
kinds = [b[0] for b in p.blocks]
check("parser title", p.title == "T", repr(p.title))
check("parser h1", ("h1", "Head", None) in p.blocks)
check("parser a", any(b[0] == "a" and b[2] == "/x" for b in p.blocks))
check("parser img", ("img", "/i.png", None) in p.blocks)
check("parser text merged", any(b[0] == "p" and "text here" in b[1] for b in p.blocks))

print("browser-test: %d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
