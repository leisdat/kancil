"""kancil serve-proxy — level 2: local HTTP(S) proxy.

Your real browser (phone Chrome) routes its traffic through Kancil:
  - plain HTTP: forwarded, agent.js injected into HTML, logged.
  - CONNECT: with --mitm (+ CA installed on the phone) the proxy decrypts,
    injects the agent, and re-encrypts; without MITM it blind-tunnels
    (host/port are still logged).

The phone's real TLS fingerprint + real login session defeat bot walls;
Kancil becomes the agent layer on top of a real browser, while staying tiny
(the browser itself is not bundled).

HTTPS MITM needs a CA: `kancil proxy-ca` generates ~/.kancil/ca.crt —
install it once in Android Settings > Security > Install certificate.
"""

import http.client
import json
import os
import re
import select
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse as up

from .agent_bridge import route_agent

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
              "proxy-authorization", "te", "trailer", "transfer-encoding",
              "upgrade", "proxy-connection"}


def ca_dir():
    from . import session as session_mod
    d = os.path.join(session_mod.BASE, "ca")
    os.makedirs(d, exist_ok=True)
    return d


def ca_paths():
    d = ca_dir()
    return os.path.join(d, "ca.crt"), os.path.join(d, "ca.key")


CA_CN = "Kancil Proxy CA"


def _host_key_path():
    return os.path.join(ca_dir(), "host.key")


def openssl_ok():
    try:
        subprocess.run(["openssl", "version"], capture_output=True,
                       check=True, timeout=10)
        return True
    except Exception:
        return False


def ensure_ca():
    """Generate the MITM CA. Pure Python (kancil.x509) by default;
    openssl CLI only as a fallback. Returns (crt, key)."""
    crt, key = ca_paths()
    hkey = _host_key_path()
    if all(os.path.exists(p) for p in (crt, key, hkey)):
        return crt, key
    try:
        from . import x509
        ca_key = x509.gen_rsa(2048)
        host_key = x509.gen_rsa(2048)  # one shared key for all host certs
        ca_der = x509.make_ca(CA_CN, ca_key)
        with open(crt, "w") as f:
            f.write(x509.pem_encode("CERTIFICATE", ca_der))
        with open(key, "w") as f:
            f.write(x509.rsa_private_pem(ca_key))
        with open(hkey, "w") as f:
            f.write(x509.rsa_private_pem(host_key))
        os.chmod(key, 0o600)
        os.chmod(hkey, 0o600)
        return crt, key
    except Exception as e:
        if not openssl_ok():
            raise RuntimeError(
                "CA generation failed (%s) and openssl CLI is not "
                "installed (Termux: pkg install openssl)" % str(e)[:100])
        return _ensure_ca_openssl()


def _ensure_ca_openssl():
    """Fallback: original openssl-based CA generation."""
    crt, key = ca_paths()
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", key, "-out", crt, "-days", "825", "-nodes",
         "-subj", "/CN=" + CA_CN], check=True, timeout=60,
        capture_output=True)
    os.chmod(key, 0o600)
    # openssl path uses per-host keys; create a shared host key for uniformity
    subprocess.run(
        ["openssl", "genrsa", "-out", _host_key_path(), "2048"],
        check=True, timeout=60, capture_output=True)
    os.chmod(_host_key_path(), 0o600)
    return crt, key


def ca_fingerprint(crt):
    """SHA256 fingerprint, openssl-style. Pure Python."""
    try:
        from . import x509
        with open(crt) as f:
            der = x509.pem_decode(f.read())
        return "SHA256 Fingerprint=" + x509.cert_fingerprint_sha256(der)
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", crt, "-noout",
             "-fingerprint", "-sha256"], capture_output=True, timeout=10,
            text=True, check=True).stdout.strip()
        return out
    except Exception:
        return "?"


def host_cert(host):
    """Per-host cert signed by our CA (cached). Returns (crt, key)."""
    crt, key = ensure_ca()
    d = os.path.join(ca_dir(), "hosts")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9.-]", "_", host)
    h_crt = os.path.join(d, safe + ".crt")
    h_key = os.path.join(d, safe + ".key")
    if os.path.exists(h_crt) and os.path.exists(h_key):
        return h_crt, h_key  # legacy openssl pair
    if os.path.exists(h_crt):
        return h_crt, _host_key_path()  # pure-python cert + shared key
    try:
        from . import x509
        ca_key = x509.parse_rsa_private_pem(open(key).read())
        host_key = x509.parse_rsa_private_pem(open(_host_key_path()).read())
        h_der = x509.make_host_cert(host, host_key, CA_CN, ca_key)
        with open(h_crt, "w") as f:
            f.write(x509.pem_encode("CERTIFICATE", h_der))
        return h_crt, _host_key_path()
    except Exception:
        return _host_cert_openssl(host, crt, key, d, safe)


def _host_cert_openssl(host, crt, key, d, safe):
    """Fallback: original openssl-based per-host cert."""
    h_crt = os.path.join(d, safe + ".crt")
    h_key = os.path.join(d, safe + ".key")
    csr = os.path.join(d, safe + ".csr")
    ext = os.path.join(d, safe + ".ext")
    subprocess.run(
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
         "-keyout", h_key, "-out", csr, "-subj", "/CN=" + host],
        check=True, timeout=60, capture_output=True)
    with open(ext, "w") as f:
        f.write("subjectAltName=DNS:%s\n" % host)
    subprocess.run(
        ["openssl", "x509", "-req", "-in", csr, "-CA", crt, "-CAkey", key,
         "-CAcreateserial", "-out", h_crt, "-days", "825", "-extfile", ext],
        check=True, timeout=60, capture_output=True)
    for p in (csr, ext):
        try:
            os.remove(p)
        except OSError:
            pass
    os.chmod(h_key, 0o600)
    return h_crt, h_key


def inject_agent(html, page_url, agent_base):
    """Insert the agent.js script tag before </body> (or append)."""
    if isinstance(html, bytes):
        try:
            html = html.decode("utf-8")
        except Exception:
            return html
    tag = ('<script src="%s/agent.js" data-kancil-url="%s"></script>'
           % (agent_base, page_url.replace('"', "&quot;")))
    if re.search(r"</body\s*>", html, re.I):
        html = re.sub(r"</body\s*>", tag + "</body>", html, count=1,
                      flags=re.I)
    else:
        html += tag
    return html.encode("utf-8")


class ProxyServer:
    def __init__(self, kancil=None, host="127.0.0.1", port=8080, mitm=False):
        self.kancil = kancil
        self.host = host
        self.port = port
        self.mitm = mitm
        self.log = []          # proxied request entries
        self._lock = threading.Lock()
        self._sock = None
        self._running = False
        self._ca_ready = False
        if mitm:
            try:
                ensure_ca()
                self._ca_ready = True
            except Exception as e:
                print("MITM disabled: %s" % e)
                self.mitm = False

    @property
    def agent_base(self):
        return "http://%s:%d/__kancil__" % (self.host, self.port)

    def _log(self, entry):
        entry["t"] = time.strftime("%H:%M:%S")
        with self._lock:
            self.log.append(entry)
            if len(self.log) > 500:
                self.log.pop(0)

    # ---------------- serving ----------------
    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(100)
        self._running = True
        th = threading.Thread(target=self._accept_loop, daemon=True,
                              name="kancil-proxy")
        th.start()
        return "http://%s:%d/" % (self.host, self.port)

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            th = threading.Thread(target=self._handle, args=(conn,),
                                  daemon=True)
            th.start()

    # ---------------- request handling ----------------
    def _read_head(self, fp):
        """Read request line + headers from a file object."""
        line = fp.readline(8192).decode("latin-1").strip()
        if not line:
            return None, {}
        parts = line.split()
        if len(parts) < 2:
            return None, {}
        method, target = parts[0].upper(), parts[1]
        headers = {}
        while True:
            h = fp.readline(8192).decode("latin-1")
            if not h or h in ("\r\n", "\n"):
                break
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        return (method, target), headers

    def _read_body(self, fp, headers):
        try:
            n = int(headers.get("content-length", 0))
        except Exception:
            n = 0
        if n > 0 and n < 50_000_000:
            return fp.read(n)
        return b""

    def _handle(self, conn):
        try:
            fp = conn.makefile("rwb")
            while True:
                req, headers = self._read_head(fp)
                if req is None:
                    break
                method, target = req
                body = self._read_body(fp, headers)
                if method == "CONNECT":
                    self._handle_connect(conn, fp, target, headers)
                    break  # connection hijacked or tunneled; done here
                else:
                    should_close = self._handle_http(
                        conn, target, headers, body, method)
                    if should_close:
                        break
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ---------------- plain HTTP ----------------
    def _forward(self, method, page_url, headers, body):
        """Forward via urllib (honors HTTP(S)_PROXY env — the sandbox egress
        proxy here; direct connection on a normal device). No redirects."""
        import urllib.request
        import urllib.error

        class _NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a):
                return None

        fwd = {k: v for k, v in headers.items()
               if k not in HOP_BY_HOP and not k.startswith("proxy-")
               and k not in ("host", "content-length")}
        req = urllib.request.Request(page_url, data=body or None,
                                     headers=fwd, method=method)
        op = urllib.request.build_opener(_NoRedir)
        try:
            resp = op.open(req, timeout=25)
            return resp.status, dict(resp.headers), resp.read(), None
        except urllib.error.HTTPError as e:
            try:
                eb = e.read()
            except Exception:
                eb = b""
            return e.code, dict(e.headers or {}), eb, None
        except Exception as e:
            return 0, {}, b"", str(e)[:120]

    def _handle_http(self, conn, target, headers, body, method,
                     scheme="http", authority=None):
        """Forward one HTTP request. Returns True if connection should close."""
        # agent endpoints served locally
        u = up.urlsplit(target)
        if not u.netloc and target.startswith("/__kancil__/"):
            return self._serve_agent(conn, target, body)
        if scheme == "http" and not u.netloc:
            self._simple(conn, 400, b"proxy needs absolute URI")
            return True

        host = u.hostname or authority
        if not host:
            self._simple(conn, 400, b"no host")
            return True
        port = u.port or (443 if scheme == "https" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        page_url = "%s://%s%s" % (scheme, host, path)

        # forward (urllib honors egress proxy env when present)
        status, rheaders, rbody, err = self._forward(method, page_url,
                                                     headers, body)
        if err is not None:
            self._simple(conn, 502,
                         ("proxy fetch failed: %s" % err).encode())
            self._log({"method": method, "url": page_url, "status": 502,
                       "error": err})
            return True

        ctype = ""
        for k, v in rheaders.items():
            if k.lower() == "content-type":
                ctype = v
                break
        if "text/html" in ctype.lower() and len(rbody) < 5_000_000:
            rbody = inject_agent(rbody, page_url, self.agent_base)

        # relay (de-chunked, content-length)
        out_h = {}
        for k, v in rheaders.items():
            lk = k.lower()
            if lk in ("transfer-encoding", "content-length", "connection"):
                continue
            out_h[k] = v
        head = "HTTP/1.1 %d %s\r\n" % (status, "OK")
        for k, v in out_h.items():
            head += "%s: %s\r\n" % (k, v)
        head += "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(rbody)
        try:
            conn.sendall(head.encode("latin-1") + rbody)
        except OSError:
            pass
        self._log({"method": method, "url": page_url, "status": status,
                   "bytes": len(rbody), "mitm": scheme == "https"})
        return True  # we always close (simple + correct)

    def _serve_agent(self, conn, target, body):
        u = up.urlsplit(target)
        query = {k: v[0] for k, v in up.parse_qs(u.query).items()}
        try:
            bobj = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            bobj = {}
        routed = route_agent(u.path, query, bobj)
        if routed is None:
            self._simple(conn, 404, b"not found")
            return True
        code, ctype, rbody = routed
        head = ("HTTP/1.1 %d OK\r\nContent-Type: %s\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (code, ctype, len(rbody)))
        try:
            conn.sendall(head.encode("latin-1") + rbody)
        except OSError:
            pass
        return True

    def _simple(self, conn, code, body, ctype="text/plain"):
        head = ("HTTP/1.1 %d -\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                "Connection: close\r\n\r\n" % (code, ctype, len(body)))
        try:
            conn.sendall(head.encode("latin-1") + body)
        except OSError:
            pass

    # ---------------- CONNECT ----------------
    def _handle_connect(self, conn, fp, target, headers):
        host_port = target.strip()
        if ":" in host_port:
            host, _, port_s = host_port.rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                port = 443
        else:
            host, port = host_port, 443

        if self.mitm and self._ca_ready and port == 443:
            try:
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            except OSError:
                return
            try:
                h_crt, h_key = host_cert(host)
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(h_crt, h_key)
                tls = ctx.wrap_socket(conn, server_side=True)
            except Exception as e:
                self._log({"method": "CONNECT", "url": host, "status": 0,
                           "error": "tls wrap failed: %s" % str(e)[:80]})
                return
            # serve decrypted requests on this TLS connection
            try:
                tfp = tls.makefile("rwb")
                while True:
                    req, h2 = self._read_head(tfp)
                    if req is None:
                        break
                    method, target2 = req
                    body = self._read_body(tfp, h2)
                    # target2 is origin-form (/path); build absolute
                    if target2.startswith("/__kancil__/"):
                        if self._serve_agent(tls, target2, body):
                            break
                        continue
                    if not target2.startswith("/"):
                        target2 = "/" + target2
                    abs_url = "https://%s%s" % (host, target2)
                    if self._handle_http(tls, abs_url, h2, body, method,
                                         scheme="https", authority=host):
                        break
            except (ConnectionResetError, BrokenPipeError, OSError,
                    ssl.SSLError):
                pass
            finally:
                try:
                    tls.close()
                except OSError:
                    pass
            return

        # blind tunnel
        try:
            up_sock = socket.create_connection((host, port), timeout=25)
        except Exception as e:
            self._simple(conn, 502, b"connect failed")
            self._log({"method": "CONNECT", "url": host_port, "status": 502,
                       "error": str(e)[:80]})
            return
        try:
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        except OSError:
            up_sock.close()
            return
        self._log({"method": "CONNECT", "url": host_port, "status": 200,
                   "tunneled": True})
        self._relay(conn, up_sock)

    @staticmethod
    def _relay(a, b):
        a.setblocking(False)
        b.setblocking(False)
        socks = [a, b]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 60)
                if not r:
                    break
                for s in r:
                    other = b if s is a else a
                    try:
                        data = s.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass
