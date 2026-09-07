# socket.py -- pure-Python socket API over the kernel smoltcp stack (Phase 2).
#
# The stdlib socket module needs the _socket C extension, which Ripos does
# not have; this shim (excluded from the initramfs stdlib copy, like
# tkinter) provides the client-side surface the web client / browser need,
# backed by kern.net_* (e1000 + smoltcp).  Host tests stub kern.

import kern

AF_INET = 2
SOCK_STREAM = 1
SOCK_DGRAM = 2

class error(Exception):
    pass


def _ip_to_u32(ip):
    parts = ip.split(".")
    return (int(parts[0]) | (int(parts[1]) << 8)
            | (int(parts[2]) << 16) | (int(parts[3]) << 24))


def _u32_to_ip(v):
    return "%d.%d.%d.%d" % (v & 0xFF, (v >> 8) & 0xFF,
                             (v >> 16) & 0xFF, (v >> 24) & 0xFF)


def gethostbyname(name):
    parts = name.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return name
    # No DNS resolver yet: require literal IPs (documented limitation).
    raise error("cannot resolve %r: DNS not implemented; use a literal IP" % name)


class _HttpReader:
    """Buffered reader over a socket for HTTP response parsing."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _fill(self):
        while not self.buf:
            if not self.sock.recv_ready(2000):
                return False
            d = self.sock.recv(8192)
            if d in (b"", None):
                return False
            self.buf += d
        return True

    def readline(self):
        while b"\n" not in self.buf:
            if not self._fill():
                break
        i = self.buf.find(b"\n")
        if i < 0:
            line = self.buf
            self.buf = b""
            return line
        line = self.buf[:i + 1]
        self.buf = self.buf[i + 1:]
        return line

    def read(self, n=-1):
        if n < 0:
            out = self.buf
            self.buf = b""
            while True:
                if not self.sock.recv_ready(2000):
                    break
                d = self.sock.recv(65536)
                if d in (b"", None):
                    break
                out += d
            return out
        while len(self.buf) < n:
            if not self.sock.recv_ready(2000):
                break
            d = self.sock.recv(n - len(self.buf))
            if d in (b"", None):
                break
            self.buf += d
        out = self.buf[:n]
        self.buf = self.buf[n:]
        return out


class socket:
    """Minimal client socket over kern.net_*."""

    def __init__(self, family=AF_INET, type=SOCK_STREAM, proto=0):
        self._type = type
        self._timeout = None
        self._closed = False
        self.fd = kern.net_socket(type == SOCK_STREAM)
        if self.fd is None:
            raise error("no sockets available")

    def connect(self, address):
        host = address[0]
        port = int(address[1])
        ip = _ip_to_u32(gethostbyname(host))
        if kern.net_connect(self.fd, ip, port) != 0:
            raise error("connect failed: %s:%d" % (host, port))
        # Wait for the TCP handshake to COMPLETE (Established), not for
        # response data: data only arrives after we send the request, so
        # kern.net_poll (which waits for readable data) would deadlock here.
        deadline = kern.tick() + 20000
        while kern.net_established(self.fd) != 1:
            if kern.tick() > deadline:
                raise error("connect timed out: %s:%d" % (host, port))
            kern.sleep(50)

    def send(self, data):
        if self._closed:
            raise error("socket closed")
        n = kern.net_send(self.fd, bytes(data))
        return n if n is not None else 0

    def sendall(self, data):
        data = bytes(data)
        off = 0
        while off < len(data):
            n = self.send(data[off:])
            if n <= 0:
                raise error("send failed")
            off += n

    def recv(self, bufsize=4096):
        if self._closed:
            raise error("socket closed")
        data = kern.net_recv(self.fd, bufsize)
        if data is None:
            return b""
        return data

    def recv_ready(self, timeout_ms=1000):
        if self._closed:
            return False
        return kern.net_poll(self.fd, timeout_ms) == 1

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                kern.net_close(self.fd)
            except Exception:
                pass

    def settimeout(self, t):
        self._timeout = t

    def makefile(self, mode="r"):
        return _HttpReader(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def shutdown(self, how):
        pass

    def fileno(self):
        return self.fd

    def getsockname(self):
        ip = kern.net_ip()
        return (_u32_to_ip(ip[0] | (ip[1] << 8) | (ip[2] << 16) | (ip[3] << 24)), 0)

    def getpeername(self):
        return ("0.0.0.0", 0)

    def bind(self, address):
        raise error("bind not supported")

    def listen(self, n):
        raise error("listen not supported")

    def accept(self):
        raise error("accept not supported")

    def gettimeout(self):
        return self._timeout

    def setblocking(self, flag):
        self._timeout = None if flag else 0


def create_connection(address, timeout=None):
    s = socket(AF_INET, SOCK_STREAM)
    if timeout is not None:
        s.settimeout(timeout)
    s.connect(address)
    return s


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return [(AF_INET, SOCK_STREAM, 6, "", (gethostbyname(host), int(port)))]
