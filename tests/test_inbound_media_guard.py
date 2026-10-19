"""Inbound URLs cannot escape download policy or carry plugin credentials."""

from __future__ import annotations

import asyncio
import base64
import http.client
import logging
import os
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from clawchat_gateway import adapter, config, media_runtime as media


@pytest.fixture(autouse=True)
def isolate_environment_proxies(monkeypatch):
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(media, "_INBOUND_PROXY_WARNED", False)


def addresses(*ips, port=80):
    return [
        (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
        for ip in ips
    ]


def download(tmp_path, urls, **kwargs):
    return asyncio.run(media.download_inbound_media(
        urls, base_url="https://media.example.test", websocket_url="wss://media.example.test/ws",
        download_dir=tmp_path, **kwargs,
    ))


def forbidden(*args, **kwargs):
    pytest.fail("unexpected resolution, connection or file handler")


@dataclass
class Route:
    status: int = 200
    headers: dict = field(default_factory=lambda: {"Content-Type": "image/png"})
    body: bytes = b"image"
    pause: float = 0
    chunks: int = 1
    header_pause: float = 0


@pytest.fixture
def server():
    routes = {}
    requests = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            route = routes.get(urlsplit(self.path).path, Route())
            try:
                if route.header_pause:
                    release.wait(route.header_pause)
                chunked = route.headers.get("Transfer-Encoding", "").lower() == "chunked"
                if chunked:
                    self.protocol_version = "HTTP/1.1"
                self.send_response(route.status)
                for name, value in route.headers.items():
                    self.send_header(name, value)
                if chunked:
                    self.send_header("Connection", "close")
                self.end_headers()
                for _ in range(route.chunks):
                    if route.pause:
                        release.wait(route.pause)
                    if chunked:
                        self.wfile.write(f"{len(route.body):X}\r\n".encode() + route.body + b"\r\n")
                    else:
                        self.wfile.write(route.body)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    port = httpd.server_port
    yield SimpleNamespace(
        routes=routes, requests=requests, port=port,
        url=lambda path="/a.png", host="media.example.test", scheme="http": f"{scheme}://{host}:{port}{path}",
    )
    release.set()
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=2)


@pytest.fixture
def public_server(server, monkeypatch):
    # Only the address policy is substituted: real HTTP goes to the local fixture.
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("127.0.0.1", port=port))
    monkeypatch.setattr(media, "_public_address", lambda ip: ip == "127.0.0.1")
    return server


@pytest.fixture
def proxy_server(server, monkeypatch):
    requests = []
    resolutions = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.requestline, dict(self.headers)))
            parsed = urlsplit(self.path)
            assert parsed.hostname == "media.example.test" and parsed.port == server.port
            upstream = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
            try:
                target = parsed.path or "/"
                if parsed.query:
                    target += "?" + parsed.query
                headers = {name: value for name, value in self.headers.items()
                           if name.lower() != "proxy-authorization"}
                upstream.request("GET", target, headers=headers)
                response = upstream.getresponse()
                self.send_response(response.status)
                for name, value in response.getheaders():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(response.read())
            finally:
                upstream.close()

        def do_CONNECT(self):
            requests.append((self.requestline, dict(self.headers)))
            assert self.path == f"media.example.test:{server.port}"
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as upstream:
                self.send_response(200, "Connection established")
                self.end_headers()
                self.wfile.flush()
                peers = [self.connection, upstream]
                while True:
                    ready, _, _ = select.select(peers, [], [], 2)
                    if not ready:
                        break
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = upstream if source is self.connection else self.connection
                        destination.sendall(data)

        def log_message(self, *args):
            pass

    def resolve(host, port):
        resolutions.append((host, port))
        assert host == "127.0.0.1", "proxy route must not resolve the target hostname"
        return addresses(host, port=port)

    monkeypatch.setattr(media, "_resolve_host", resolve)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield SimpleNamespace(
            requests=requests, resolutions=resolutions, port=httpd.server_port,
            url=f"http://127.0.0.1:{httpd.server_port}",
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("mode", [None, "on"])
def test_environment_proxy_get_and_connect(tmp_path, server, proxy_server, monkeypatch, scheme, mode):
    monkeypatch.setenv(f"{scheme.upper()}_PROXY", proxy_server.url)
    tls_hosts = []

    def wrap(sock, server_hostname):
        tls_hosts.append(server_hostname)
        return sock

    monkeypatch.setattr(media.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=wrap))
    kwargs = {} if mode is None else {"proxy_mode": mode}
    url = server.url("/a.png?signature=test#fragment", scheme=scheme)
    got = download(tmp_path, [url], allow_http=scheme == "http", **kwargs)
    assert got[0].local_path.read_bytes() == b"image"
    assert proxy_server.resolutions == [("127.0.0.1", proxy_server.port)]
    requestline, headers = proxy_server.requests[0]
    expected = f"CONNECT media.example.test:{server.port} " if scheme == "https" else f"GET {url.split('#')[0]} "
    assert requestline.startswith(expected)
    assert tls_hosts == (["media.example.test"] if scheme == "https" else [])
    for received in [headers, server.requests[0][1]]:
        assert "authorization" not in {key.lower() for key in received}
    assert "proxy-authorization" not in {key.lower() for key in server.requests[0][1]}


def test_auto_without_proxy_still_checks_private_dns(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("127.0.0.1", port=port))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert download(tmp_path, ["https://media.example.test/a"]) == []
    assert "destination address is not allowed" in caplog.text


def test_off_ignores_environment_proxy(tmp_path, public_server, proxy_server, monkeypatch):
    # proxy_server installs a resolver too; restore the direct fixture's mapping.
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("127.0.0.1", port=port))
    monkeypatch.setenv("HTTP_PROXY", proxy_server.url)
    monkeypatch.setattr(media.urllib.request, "getproxies", forbidden)
    monkeypatch.setattr(media.urllib.request, "proxy_bypass", forbidden)
    assert len(download(tmp_path, [public_server.url()], allow_http=True, proxy_mode="off")) == 1
    assert proxy_server.requests == []


@pytest.mark.parametrize("value", [None, "socks5://127.0.0.1:9999", "http://[invalid"])
def test_on_requires_supported_proxy(tmp_path, monkeypatch, caplog, value):
    if value is not None:
        monkeypatch.setenv("HTTPS_PROXY", value)
    monkeypatch.setattr(media, "_resolve_host", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert download(tmp_path, ["https://media.example.test/a"], proxy_mode="on") == []
    assert "proxy is required but unavailable or unsupported" in caplog.text
    assert not list(tmp_path.iterdir())


def test_auto_unsupported_proxy_warns_once_without_url_and_validates_direct(tmp_path, monkeypatch, caplog):
    proxy_url = "socks5://user:pw@127.0.0.1:9999"  # redact-allow:email
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("127.0.0.1", port=port))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for _ in range(2):
        assert download(tmp_path, ["https://media.example.test/a"]) == []
    assert caplog.text.count("unsupported environment proxy") == 1
    assert caplog.text.count("destination address is not allowed") == 2
    for secret in (proxy_url, "user", "pw", "socks5", "9999"):
        assert secret not in caplog.text


@pytest.mark.parametrize("mode", ["auto", "on"])
@pytest.mark.parametrize("has_proxy", [True, False])
def test_no_proxy_direct_route_retains_private_checks(tmp_path, server, proxy_server, monkeypatch, caplog, mode, has_proxy):
    if has_proxy:
        monkeypatch.setenv("HTTP_PROXY", proxy_server.url)
    monkeypatch.setenv("no_proxy", "media.example.test")
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("127.0.0.1", port=port))
    assert download(tmp_path, [server.url()], allow_http=True, proxy_mode=mode) == []
    assert "destination address is not allowed" in caplog.text
    assert proxy_server.requests == [] and server.requests == []
    monkeypatch.setattr(media, "_public_address", lambda ip: ip == "127.0.0.1")
    assert len(download(tmp_path, [server.url()], allow_http=True, proxy_mode=mode)) == 1
    assert proxy_server.requests == []


@pytest.mark.parametrize("host", [
    "127.0.0.1", "192.168.1.1", "172.16.0.1",  # redact-allow:private-ip
    "169.254.169.254", "[::1]", "[fe80::1]", "[::ffff:127.0.0.1]",
    "2130706433", "0177.0.0.1", "0x7f.0.0.1",
    "localhost", "a.localhost", "LOCALHOST.",
    # IDNA maps U+3002 to "." before CONNECT; trailing-dot numeric literal.
    "127\u30020\u30020\u30021", "localhost\u3002", "127.0.0.1.",
    "%31%32%37.0.0.1",
])
def test_proxy_never_receives_private_literals_or_localhost(tmp_path, proxy_server, monkeypatch, caplog, host):
    monkeypatch.setenv("HTTPS_PROXY", proxy_server.url)
    monkeypatch.setattr(media, "_resolve_host", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert download(tmp_path, [f"https://{host}/a"]) == []
    assert "destination address is not allowed" in caplog.text
    assert proxy_server.requests == []


@pytest.mark.parametrize("location", ["file:///nonexistent/a", "http://media.example.test/a"])
def test_proxy_redirect_scheme_is_rechecked(tmp_path, server, proxy_server, monkeypatch, caplog, location):
    monkeypatch.setenv("HTTPS_PROXY", proxy_server.url)
    monkeypatch.setattr(media.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=lambda sock, **kw: sock))
    server.routes["/start"] = Route(302, {"Location": location})
    assert download(tmp_path, [server.url("/start", scheme="https")]) == []
    assert "URL scheme is not allowed" in caplog.text
    assert len(proxy_server.requests) == 1
    assert [path for path, _ in server.requests] == ["/start"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", ["auto", "on"])
@pytest.mark.parametrize("public", [True, False])
def test_proxy_redirect_to_no_proxy_checks_dns(tmp_path, server, proxy_server, monkeypatch, caplog, mode, public):
    proxy_url = f"http://user:pw@127.0.0.1:{proxy_server.port}"  # redact-allow:email
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("NO_PROXY", "direct.example.test")
    server.routes["/start"] = Route(302, {"Location": server.url("/end", host="direct.example.test")})
    resolved = []

    def resolve(host, port):
        resolved.append((host, port))
        assert host in {"127.0.0.1", "direct.example.test"}
        return addresses("127.0.0.1", port=port)

    monkeypatch.setattr(media, "_resolve_host", resolve)
    if public:
        monkeypatch.setattr(media, "_public_address", lambda ip: ip == "127.0.0.1")
    got = download(tmp_path, [server.url("/start")], allow_http=True, proxy_mode=mode)
    assert len(got) == int(public)
    assert resolved == [("127.0.0.1", proxy_server.port), ("direct.example.test", server.port)]
    assert len(proxy_server.requests) == 1
    assert "Proxy-Authorization" in proxy_server.requests[0][1]
    assert [path for path, _ in server.requests] == (["/start", "/end"] if public else ["/start"])
    assert all(not any("authorization" in key.lower() for key in headers) for _, headers in server.requests)
    if not public:
        assert "destination address is not allowed" in caplog.text


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_proxy_userinfo_only_authenticates_proxy(tmp_path, server, proxy_server, monkeypatch, caplog, scheme):
    proxy_url = f"http://user:pw@127.0.0.1:{proxy_server.port}"  # redact-allow:email
    monkeypatch.setenv(f"{scheme.upper()}_PROXY", proxy_url)
    monkeypatch.setattr(media.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=lambda sock, **kw: sock))
    server.routes["/bad"] = Route(403)
    assert download(tmp_path, [server.url("/bad", scheme=scheme)], allow_http=scheme == "http") == []
    requestline, headers = proxy_server.requests[0]
    expected = "Basic " + base64.b64encode(b"user:pw").decode()
    assert headers["Proxy-Authorization"] == expected
    assert "user" not in requestline and "pw" not in requestline
    assert "authorization" not in {key.lower() for key in headers}
    assert not any("authorization" in key.lower() for key in server.requests[0][1])
    assert "HTTP status 403" in caplog.text
    for secret in (proxy_url, "user", "pw", expected):
        assert secret not in caplog.text


@pytest.mark.parametrize("mode", ["auto", "on"])
def test_proxy_selection_requires_target_scheme(tmp_path, monkeypatch, caplog, mode):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("127.0.0.1", port=port))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert download(tmp_path, ["https://media.example.test/a"], proxy_mode=mode) == []
    expected = "destination address is not allowed" if mode == "auto" else "proxy is required"
    assert expected in caplog.text


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("declared", [True, False])
def test_proxy_size_limit_cleans_partial_files(tmp_path, server, proxy_server, monkeypatch, caplog, scheme, declared):
    monkeypatch.setenv(f"{scheme.upper()}_PROXY", proxy_server.url)
    monkeypatch.setattr(media.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=lambda sock, **kw: sock))
    headers = {"Content-Length": "9"} if declared else {}
    server.routes["/large"] = Route(headers=headers, body=b"oversized")
    assert download(tmp_path, [server.url("/large", scheme=scheme)],
                    allow_http=scheme == "http", max_bytes=4) == []
    assert "attachment exceeds size limit" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["ftp://files.example.test/a", "data:text/plain,hello", "javascript:void(0)", "C:/clawchat-test.txt"])
def test_non_http_schemes_never_join_resolve_or_connect(tmp_path, monkeypatch, caplog, value):
    monkeypatch.setattr(media, "urljoin", forbidden)
    monkeypatch.setattr(media, "urlopen", forbidden)
    monkeypatch.setattr(media, "_resolve_host", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(media.InboundMediaRejected):
        media._resolve_inbound_media_url(value, base_url="https://media.example.test", websocket_url="")
    assert download(tmp_path, [value]) == []
    assert "rejected" in caplog.text
    assert value.split(":", 1)[1] not in caplog.text
    assert not list(tmp_path.iterdir())


def test_file_url_does_not_read_a_real_file(tmp_path, monkeypatch):
    source = tmp_path / "clawchat-test.txt"
    source.write_text("test attachment")
    monkeypatch.setattr(media, "urljoin", forbidden)
    monkeypatch.setattr(media, "urlopen", forbidden)
    monkeypatch.setattr(media, "_resolve_host", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(type(source), "read_bytes", forbidden)
    with pytest.raises(media.InboundMediaRejected):
        media._resolve_inbound_media_url(source.as_uri(), base_url="https://media.example.test", websocket_url="")
    assert download(tmp_path / "downloads", [source.as_uri()]) == []
    assert not (tmp_path / "downloads").exists()


def test_http_requires_explicit_opt_in(tmp_path, public_server):
    assert download(tmp_path, [public_server.url()]) == []
    assert public_server.requests == []
    got = download(tmp_path, [public_server.url()], allow_http=True)
    assert got[0].local_path.read_bytes() == b"image"
    assert got[0].size == 5
    assert got[0].mime == "image/png"
    assert len(public_server.requests) == 1


def test_https_downgrade_is_rejected(tmp_path, public_server, monkeypatch):
    # Simulate the first TLS hop's response using the local HTTP fixture.
    monkeypatch.setattr(media, "_InboundHTTPSConnection", media._InboundHTTPConnection)
    public_server.routes["/start"] = Route(302, {"Location": public_server.url("/end")})
    assert download(tmp_path, [public_server.url("/start", scheme="https")]) == []
    assert [path for path, _ in public_server.requests] == ["/start"]


def test_https_is_allowed_by_default(tmp_path, public_server, monkeypatch):
    monkeypatch.setattr(media, "_InboundHTTPSConnection", media._InboundHTTPConnection)
    got = download(tmp_path, [public_server.url(scheme="https")])
    assert got[0].local_path.read_bytes() == b"image"
    assert "authorization" not in {name.lower() for name in public_server.requests[0][1]}


@pytest.mark.parametrize("ips", [
    ("127.0.0.1",), ("172.16.0.1",), ("192.168.1.1",), ("169.254.169.254",),  # redact-allow:private-ip (test data)
    ("::1",), ("fe80::1",), ("100.64.0.1",), ("::ffff:127.0.0.1",),
    ("224.0.0.1",), ("8.8.8.8", "172.16.0.1"), ("172.16.0.1", "8.8.8.8"),  # redact-allow:private-ip
])
def test_every_resolved_address_must_be_public(tmp_path, monkeypatch, caplog, ips):
    seen = []

    def resolve(host, port):
        seen.append((host, port))
        return addresses(*ips, port=port)

    monkeypatch.setattr(media, "_resolve_host", resolve)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert download(tmp_path, ["https://media.example.test/a"]) == []
    assert seen == [("media.example.test", 443)]
    assert "destination address is not allowed" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("host", ["localhost", "a.localhost", "LOCALHOST.", "a.LOCALHOST."])
def test_localhost_is_rejected_without_dns(tmp_path, monkeypatch, host):
    monkeypatch.setattr(media, "_resolve_host", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert download(tmp_path, [f"https://{host}/a"]) == []


@pytest.mark.parametrize("empty", [True, False])
def test_resolution_failure_is_closed(tmp_path, monkeypatch, caplog, empty):
    def resolve(*args):
        if empty:
            return []
        raise socket.gaierror("https://media.example.test/a?sig=private")

    monkeypatch.setattr(media, "_resolve_host", resolve)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert download(tmp_path, ["https://media.example.test/a?sig=private"]) == []
    assert "resolution failed" in caplog.text
    assert "sig=private" not in caplog.text


def test_private_network_opt_in_preserves_scheme_policy(tmp_path, server, monkeypatch):
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("127.0.0.1", port=port))
    assert download(tmp_path, [server.url(host="localhost")], allow_private_network=True) == []
    got = download(tmp_path, [server.url(host="localhost")], allow_http=True, allow_private_network=True)
    assert got[0].local_path.read_bytes() == b"image"


def test_dns_rebinding_connects_only_to_checked_ip_per_hop(tmp_path, server, monkeypatch):
    resolved = []
    connections = []
    real_connect = socket.create_connection

    def resolve(host, port):
        resolved.append((host, port))
        return addresses("8.8.8.8" if len(resolved) == 1 else "127.0.0.1", port=port)

    def connect(address, timeout):
        connections.append(address)
        return real_connect(("127.0.0.1", server.port), timeout)

    monkeypatch.setattr(media, "_resolve_host", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    got = download(tmp_path, [server.url()], allow_http=True)
    assert len(got) == 1
    assert resolved == [("media.example.test", server.port)]
    assert connections == [("8.8.8.8", server.port)]
    server.routes["/start"] = Route(302, {"Location": "/end"})
    resolved.clear()
    connections.clear()
    assert download(tmp_path, [server.url("/start")], allow_http=True) == []
    assert resolved == [("media.example.test", server.port)] * 2
    assert connections == [("8.8.8.8", server.port)]


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("failure", [OSError, TimeoutError])
def test_checked_ips_are_deduplicated_and_retried_without_dns(tmp_path, server, monkeypatch, scheme, failure):
    resolved = []
    connected = []
    tls_hosts = []
    real_connect = socket.create_connection

    def resolve(host, port):
        resolved.append((host, port))
        return addresses("8.8.8.8", "8.8.8.8", "1.1.1.1", port=port)

    def connect(address, timeout):
        connected.append(address)
        if address[0] == "8.8.8.8":
            raise failure("first address unavailable")
        return real_connect(("127.0.0.1", server.port), timeout)

    def wrap(sock, server_hostname):
        tls_hosts.append(server_hostname)
        return sock

    monkeypatch.setattr(media, "_resolve_host", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    # Keep real HTTP transport while recording the TLS hostname and fallback.
    monkeypatch.setattr(media.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=wrap))
    got = download(tmp_path, [server.url(scheme=scheme)], allow_http=True)
    assert got[0].local_path.read_bytes() == b"image"
    assert resolved == [("media.example.test", server.port)]
    assert connected == [("8.8.8.8", server.port), ("1.1.1.1", server.port)]
    assert tls_hosts == (["media.example.test"] if scheme == "https" else [])
    assert server.requests[0][1]["Host"] == f"media.example.test:{server.port}"


def test_all_checked_ips_must_fail_before_download_fails(tmp_path, monkeypatch, caplog):
    connected = []
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("8.8.8.8", "1.1.1.1", port=port))

    def connect(address, timeout):
        connected.append(address)
        raise OSError("address unavailable")

    monkeypatch.setattr(socket, "create_connection", connect)
    assert download(tmp_path, ["https://media.example.test/a"]) == []
    assert connected == [("8.8.8.8", 443), ("1.1.1.1", 443)]
    assert "download failed" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("stage", ["tcp", "tls"])
def test_connection_stages_share_deadline_and_recompute_timeout(tmp_path, monkeypatch, caplog, stage):
    now = [0.0]
    tcp_timeouts = []
    tls_timeouts = []
    tls_hosts = []
    closed = []
    sock = SimpleNamespace(settimeout=tls_timeouts.append, close=lambda: closed.append(True))

    def connect(address, timeout):
        tcp_timeouts.append(timeout)
        now[0] += 1.1 if stage == "tcp" else 0.7
        return sock

    def wrap(raw_sock, server_hostname):
        tls_hosts.append(server_hostname)
        now[0] += 0.4
        return raw_sock

    monkeypatch.setattr(media.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(media, "_resolve_host", lambda host, port: addresses("8.8.8.8", "1.1.1.1", port=port))
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(media.ssl, "create_default_context", lambda: SimpleNamespace(wrap_socket=wrap))
    assert download(tmp_path, ["https://media.example.test/a"], timeout_seconds=30, deadline_seconds=1) == []
    assert tcp_timeouts == [1]
    if stage == "tcp":
        assert tls_timeouts == [] and tls_hosts == []
    else:
        assert tls_timeouts == [pytest.approx(0.3)]
        assert tls_hosts == ["media.example.test"]
    assert closed
    assert "deadline exceeded" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("host", ["2130706433", "0177.0.0.1", "0x7f.0.0.1", "localhost.", "[::ffff:127.0.0.1]"])
def test_loopback_host_spellings_are_rejected_with_real_resolver(tmp_path, monkeypatch, host):
    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(media.InboundMediaRejected, match="address"):
        media._download_inbound_media_sync(url=f"https://{host}/a", download_dir=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("location", [
    "//other.example.test/a", r"\\other.example.test\a", r"/\other.example.test/a",
])
def test_alternate_redirect_authorities_are_resolved_and_checked(tmp_path, server, monkeypatch, location):
    server.routes["/start"] = Route(302, {"Location": location})
    resolved = []
    connected = []
    real_connect = socket.create_connection

    def resolve(host, port):
        resolved.append((host, port))
        return addresses("8.8.8.8" if host == "media.example.test" else "127.0.0.1", port=port)

    def connect(address, timeout):
        connected.append(address)
        return real_connect(("127.0.0.1", server.port), timeout)

    monkeypatch.setattr(media, "_resolve_host", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    assert download(tmp_path, [server.url("/start")], allow_http=True) == []
    assert resolved == [("media.example.test", server.port), ("other.example.test", 80)]
    assert connected == [("8.8.8.8", server.port)]
    assert len(server.requests) == 1


def test_cross_host_redirect_to_private_address_is_rejected(tmp_path, server, monkeypatch):
    server.routes["/start"] = Route(302, {"Location": server.url("/end", host="private.example.test")})
    real_connect = socket.create_connection
    connected = []
    resolved = []

    def resolve(host, port):
        resolved.append(host)
        return addresses("8.8.8.8" if host == "media.example.test" else "172.16.0.1", port=port)  # redact-allow:private-ip

    def connect(address, timeout):
        connected.append(address)
        return real_connect(("127.0.0.1", server.port), timeout)

    monkeypatch.setattr(media, "_resolve_host", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    assert download(tmp_path, [server.url("/start")], allow_http=True) == []
    assert resolved == ["media.example.test", "private.example.test"]
    assert connected == [("8.8.8.8", server.port)]
    assert len(server.requests) == 1


@pytest.mark.parametrize("location", ["file:///nonexistent/clawchat-test.txt", "ftp://files.example.test/a", "data:text/plain,hello"])
def test_redirect_cannot_change_to_non_http_scheme(tmp_path, public_server, location):
    public_server.routes["/start"] = Route(302, {"Location": location})
    assert download(tmp_path, [public_server.url("/start")], allow_http=True) == []
    assert len(public_server.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_succeed_without_authorization_at_any_hop(tmp_path, public_server, monkeypatch, status):
    resolved = []

    def resolve(host, port):
        resolved.append(host)
        return addresses("127.0.0.1", port=port)

    monkeypatch.setattr(media, "_resolve_host", resolve)
    public_server.routes["/start"] = Route(status, {"Location": "/middle"})
    public_server.routes["/middle"] = Route(status, {"Location": public_server.url("/end", host="other.example.test")})
    got = download(tmp_path, [public_server.url("/start")], allow_http=True)
    assert got[0].local_path.read_bytes() == b"image"
    assert got[0].source_url == public_server.url("/start")
    assert resolved == ["media.example.test", "media.example.test", "other.example.test"]
    assert [path for path, _ in public_server.requests] == ["/start", "/middle", "/end"]
    for _, headers in public_server.requests:
        assert "authorization" not in {name.lower() for name in headers}
    assert public_server.requests[-1][1]["Host"] == f"other.example.test:{public_server.port}"


@pytest.mark.parametrize("count,success", [(5, True), (6, False)])
def test_redirect_limit_is_five(tmp_path, public_server, count, success):
    for n in range(count):
        public_server.routes[f"/{n}"] = Route(302, {"Location": f"/{n + 1}"})
    got = download(tmp_path, [public_server.url("/0")], allow_http=True)
    assert bool(got) is success
    assert len(public_server.requests) == 6
    assert not list(tmp_path.glob("*.part"))


def test_redirect_without_location_is_rejected(tmp_path, public_server, caplog):
    public_server.routes["/start"] = Route(302, {})
    assert download(tmp_path, [public_server.url("/start")], allow_http=True) == []
    assert "rejected" in caplog.text
    assert len(public_server.requests) == 1


def test_same_origin_relative_url_has_no_authorization(tmp_path, public_server):
    got = asyncio.run(media.download_inbound_media(
        ["/media/a.png?signature=private#fragment"], base_url="https://unused.example.test",
        websocket_url="wss://unused.example.test/ws", media_base_url=public_server.url(""),
        download_dir=tmp_path, allow_http=True,
    ))
    assert got[0].local_path.read_bytes() == b"image"
    assert public_server.requests[0][0] == "/media/a.png?signature=private"
    assert "authorization" not in {name.lower() for name in public_server.requests[0][1]}


def test_same_base_origin_has_no_authorization(tmp_path, public_server):
    got = asyncio.run(media.download_inbound_media(
        [public_server.url()], base_url=public_server.url(""), websocket_url="",
        download_dir=tmp_path, allow_http=True,
    ))
    assert got[0].local_path.read_bytes() == b"image"
    assert "authorization" not in {name.lower() for name in public_server.requests[0][1]}


def test_off_bypasses_environment_proxies_and_urlopen(tmp_path, public_server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(media, "urlopen", forbidden)
    connected = []
    real_connect = socket.create_connection

    def connect(address, timeout):
        connected.append(address)
        return real_connect(address, timeout)

    monkeypatch.setattr(socket, "create_connection", connect)
    assert len(download(tmp_path, [public_server.url()], allow_http=True, proxy_mode="off")) == 1
    assert connected == [("127.0.0.1", public_server.port)]


def test_relative_url_uses_derived_websocket_base():
    assert media._resolve_inbound_media_url(
        "/media/a.png", base_url="https://unused.example.test", websocket_url="wss://media.example.test/ws",
    ) == "https://media.example.test/media/a.png"


def test_https_keeps_hostname_for_sni(monkeypatch):
    seen = []
    raw_sock = SimpleNamespace(close=lambda: None)
    wrapped_sock = object()
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: seen.append((address, timeout)) or raw_sock)
    context = SimpleNamespace(wrap_socket=lambda sock, server_hostname: seen.append((sock, server_hostname)) or wrapped_sock)
    monkeypatch.setattr(media.ssl, "create_default_context", lambda: context)
    connection = media._PublicHTTPSConnection("media.example.test", 443, ip="2606:4700:4700::1111", timeout=30)
    connection.connect()
    assert seen == [(("2606:4700:4700::1111", 443), 30), (raw_sock, "media.example.test")]
    assert connection.sock is wrapped_sock


def test_oversized_content_length_rejected_before_file_creation(tmp_path, public_server, caplog):
    public_server.routes["/large"] = Route(headers={"Content-Length": "1000000"})
    assert download(tmp_path, [public_server.url("/large")], allow_http=True, max_bytes=10) == []
    assert not list(tmp_path.iterdir())
    assert "size limit" in caplog.text


@pytest.mark.parametrize("with_length", [False, True])
def test_stream_limit_cleans_partial_file(tmp_path, public_server, monkeypatch, caplog, with_length):
    headers = {"Content-Length": "1000000"} if with_length else {}
    public_server.routes["/large"] = Route(headers=headers, body=b"a" * 65536, chunks=32)
    observed = []
    original_open = type(tmp_path).open

    def record_open(path, *args, **kwargs):
        if args and args[0] == "xb":
            observed.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "open", record_open)
    assert download(tmp_path, [public_server.url("/large")], allow_http=True, max_bytes=100000) == []
    assert not list(tmp_path.iterdir())
    assert "size limit" in caplog.text
    assert bool(observed) is not with_length
    assert all(path.suffix == ".part" for path in observed)


def test_oversized_chunked_response_is_stopped_and_partial_removed(tmp_path, public_server, monkeypatch, caplog):
    public_server.routes["/chunked"] = Route(
        headers={"Transfer-Encoding": "chunked"}, body=b"x" * 65536, chunks=32,
    )
    received = []
    read1 = media.http.client.HTTPResponse.read1

    def read(response, size):
        assert response.chunked
        assert response.version == 11
        assert response.headers.get("Content-Length") is None
        chunk = read1(response, size)
        received.append(len(chunk))
        return chunk

    monkeypatch.setattr(media.http.client.HTTPResponse, "read1", read)
    assert download(tmp_path, [public_server.url("/chunked")], allow_http=True, max_bytes=100000) == []
    assert 100000 < sum(received) <= 100000 + 65536
    assert max(received) <= 65536
    assert not list(tmp_path.iterdir())
    assert "size limit" in caplog.text


def test_success_streams_in_chunks_and_renames(tmp_path, public_server, monkeypatch):
    reads = []
    read1 = media.http.client.HTTPResponse.read1

    def read(response, size):
        reads.append(size)
        return read1(response, size)

    monkeypatch.setattr(media.http.client.HTTPResponse, "read1", read)
    body = b"x" * (65536 * 2 + 1)
    public_server.routes["/good"] = Route(headers={"Content-Length": str(len(body))}, body=body)
    got = download(tmp_path, [public_server.url("/good")], allow_http=True, max_bytes=len(body))
    assert got[0].size == len(body)
    assert got[0].local_path.read_bytes() == body
    assert list(tmp_path.iterdir()) == [got[0].local_path]
    assert got[0].local_path.suffix != ".part"
    assert len(reads) >= 3 and set(reads) == {65536}


@pytest.mark.parametrize("headers", [{"Content-Length": "invalid"}, {"Content-Length": "20"}])
def test_malformed_or_truncated_response_fails_and_cleans_up(tmp_path, public_server, headers):
    public_server.routes["/bad"] = Route(headers=headers, body=b"short")
    assert download(tmp_path, [public_server.url("/bad")], allow_http=True) == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [400, 403, 404, 500])
def test_non_success_status_is_a_logged_failure(tmp_path, public_server, caplog, status):
    public_server.routes["/bad"] = Route(status=status)
    assert download(tmp_path, [public_server.url("/bad")], allow_http=True) == []
    assert f"HTTP status {status}" in caplog.text
    assert "download failed" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("stage", ["body", "headers"])
def test_socket_read_timeout_only_logs_and_returns_empty(tmp_path, public_server, caplog, stage):
    public_server.routes["/hang"] = Route(pause=0.8 if stage == "body" else 0, header_pause=0.8 if stage == "headers" else 0)
    started = time.monotonic()
    assert download(tmp_path, [public_server.url("/hang")], allow_http=True, timeout_seconds=0.2) == []
    assert time.monotonic() - started < 0.7
    assert "download failed" in caplog.text
    assert "TimeoutError" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("stage", ["body", "headers"])
def test_total_deadline_interrupts_an_active_socket(tmp_path, public_server, caplog, stage):
    public_server.routes["/slow"] = Route(body=b"x", pause=0.04, chunks=30, header_pause=0.8 if stage == "headers" else 0)
    started = time.monotonic()
    assert download(tmp_path, [public_server.url("/slow")], allow_http=True, timeout_seconds=1, deadline_seconds=0.2) == []
    assert time.monotonic() - started < 0.7
    assert "inbound media" in caplog.text
    assert not list(tmp_path.iterdir())


def test_total_deadline_bounds_dns_and_never_connects_late(tmp_path, monkeypatch, caplog):
    release = threading.Event()
    finished = threading.Event()

    def resolve(host, port):
        release.wait(2)
        finished.set()
        return addresses("8.8.8.8", port=port)

    monkeypatch.setattr(media, "_resolve_host", resolve)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    try:
        started = time.monotonic()
        assert download(tmp_path, ["https://media.example.test/a"], deadline_seconds=0.1) == []
        assert time.monotonic() - started < 0.7
        assert "deadline exceeded" in caplog.text
    finally:
        release.set()
        assert finished.wait(1)


def test_pending_dns_is_bounded_and_fifth_lookup_starts_no_thread(monkeypatch):
    slots = threading.BoundedSemaphore(4)
    release = threading.Event()
    workers = []
    thread_type = threading.Thread

    def thread(*args, **kwargs):
        worker = thread_type(*args, **kwargs)
        workers.append(worker)
        return worker

    def resolve(host, port):
        release.wait(5)
        return addresses("8.8.8.8", port=port)

    monkeypatch.setattr(media, "_INBOUND_DNS_SLOTS", slots)
    monkeypatch.setattr(media, "_resolve_host", resolve)
    monkeypatch.setattr(media.threading, "Thread", thread)
    try:
        for _ in range(4):
            with pytest.raises(media.InboundMediaRejected, match="deadline"):
                media._resolve_inbound_host("media.example.test", 443, 0.01)
        assert len(workers) == 4
        with pytest.raises(media.InboundMediaRejected, match="dns"):
            media._resolve_inbound_host("media.example.test", 443, 30)
        assert len(workers) == 4
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()
    assert all(slots.acquire(blocking=False) for _ in range(4))
    assert not slots.acquire(blocking=False)
    for _ in range(4):
        slots.release()


@pytest.mark.parametrize("failure", ["resolution", "start"])
def test_failed_dns_releases_its_slot(monkeypatch, failure):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(media, "_INBOUND_DNS_SLOTS", slots)

    def fail(*args, **kwargs):
        raise OSError("lookup unavailable")

    monkeypatch.setattr(media, "_resolve_host", fail)
    if failure == "start":
        monkeypatch.setattr(media.threading, "Thread", fail)
    with pytest.raises(media.InboundMediaRejected, match="dns"):
        media._resolve_inbound_host("media.example.test", 443, 1)
    assert slots.acquire(blocking=False)
    slots.release()


def test_total_deadline_spans_redirects(tmp_path, public_server):
    for n in range(5):
        public_server.routes[f"/{n}"] = Route(302, {"Location": f"/{n + 1}"}, header_pause=0.06)
    assert download(tmp_path, [public_server.url("/0")], allow_http=True,
                    timeout_seconds=1, deadline_seconds=0.2) == []
    assert len(public_server.requests) < 6
    assert not list(tmp_path.iterdir())


def test_one_rejected_and_one_failed_attachment_do_not_stop_others(tmp_path, public_server, caplog):
    public_server.routes["/bad"] = Route(403)
    with caplog.at_level(logging.WARNING):
        got = download(tmp_path, [
            "file:///nonexistent/clawchat-test.txt?sig=private#hidden",
            public_server.url("/bad?sig=private#hidden"), public_server.url("/good"),
        ], allow_http=True)
    assert len(got) == 1
    assert got[0].local_path.read_bytes() == b"image"
    assert "rejected" in caplog.text and "download failed" in caplog.text
    assert "sig=private" not in caplog.text and "hidden" not in caplog.text


@pytest.mark.parametrize("exception", [ValueError, OSError, media.InboundMediaRejected])
def test_exception_urls_and_malformed_urls_cannot_leak_or_escape(tmp_path, caplog, exception):
    def fail(**kwargs):
        raise exception("https://media.example.test/a?private-query#private-fragment")

    assert download(tmp_path, ["https://media.example.test/a?private-query#private-fragment"], download_file=fail) == []
    assert download(tmp_path, ["https://[invalid?private-query#private-fragment"]) == []
    assert "private-query" not in caplog.text and "private-fragment" not in caplog.text


def test_rejected_userinfo_never_appears_in_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(media, "_resolve_host", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert download(tmp_path, ["https://user:pw@media.example.test/a.png?x=1"]) == []  # redact-allow:email (userinfo test data)
    assert "rejected" in caplog.text
    assert "https://media.example.test/a.png" in caplog.text
    for value in ("user", "pw", "x=1"):
        assert value not in caplog.text


@pytest.mark.parametrize("url,expected", [
    ("https://user:pw@media.example.test:8443/a.png?x=1#fragment", "https://media.example.test:8443/a.png"),  # redact-allow:email (userinfo test data)
    ("https://user:pw@[::1]:8443/a.png?x=1", "https://[::1]:8443/a.png"),
    ("//user:pw@media.example.test/a.png?x=1", "//media.example.test/a.png"),  # redact-allow:email (userinfo test data)
    ("https://media.example.test:bad/a.png?x=1", "<invalid>"),
    ("https://[invalid/a.png?x=1", "<invalid>"),
    ("https:///a.png?x=1", "<invalid>"),
    ("data:text/plain,private-payload", "data:<redacted>"),
])
def test_inbound_log_url_rebuilds_only_safe_http_components(url, expected):
    assert media._inbound_log_url(url) == expected


def test_injected_downloader_receives_only_new_keyword_parameters(tmp_path):
    seen = []

    def injected(**kwargs):
        seen.append(kwargs)
        return media.DownloadedMedia(tmp_path / "a.png", "image/png", 5, kwargs["url"])

    assert len(download(tmp_path, ["/a.png"], download_file=injected, allow_http=True,
                        allow_private_network=True, proxy_mode="on", max_bytes=19,
                        timeout_seconds=0.2, deadline_seconds=0.4)) == 1
    assert seen == [dict(url="https://media.example.test/a.png", download_dir=tmp_path,
                         allow_http=True, allow_private_network=True, proxy_mode="on", max_bytes=19,
                         timeout_seconds=0.2, deadline_seconds=0.4)]


MEDIA_DEFAULTS = {
    "media_allow_http": False, "media_allow_private_network": False,
    "media_max_bytes": 104857600, "media_download_timeout_seconds": 30,
    "media_download_deadline_seconds": 300,
    "media_proxy_mode": "auto",
}


@pytest.fixture
def make_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    # Isolate profile/env lookup while exercising real env-before-extra parsing.
    monkeypatch.setattr(config, "_get_env", lambda *names: next((os.environ[name] for name in names if os.environ.get(name)), ""))
    for key in MEDIA_DEFAULTS:
        monkeypatch.delenv(f"CLAWCHAT_{key.upper()}", raising=False)
    return lambda extra=None: config.ClawChatConfig.from_platform_config(SimpleNamespace(extra=extra or {}))


def test_config_defaults(make_config):
    parsed = make_config()
    direct = config.ClawChatConfig(websocket_url="wss://media.example.test/ws")
    for key, expected in MEDIA_DEFAULTS.items():
        assert getattr(parsed, key) == getattr(direct, key) == expected


def test_config_extra_values(make_config):
    extra = dict(media_allow_http="yes", media_allow_private_network="on", media_max_bytes="12345",
                 media_download_timeout_seconds="0.2", media_download_deadline_seconds="0.4")
    parsed = make_config(extra)
    for key, expected in zip(MEDIA_DEFAULTS, [True, True, 12345, 0.2, 0.4]):
        assert getattr(parsed, key) == expected


def test_config_env_overrides_extra(make_config, monkeypatch):
    extra = dict(media_allow_http=True, media_allow_private_network=True, media_max_bytes=1,
                 media_download_timeout_seconds=1, media_download_deadline_seconds=1)
    values = ["false", "0", "9876", "0.3", "0.6"]
    for key, value in zip(MEDIA_DEFAULTS, values):
        monkeypatch.setenv(f"CLAWCHAT_{key.upper()}", value)
    parsed = make_config(extra)
    for key, expected in zip(MEDIA_DEFAULTS, [False, False, 9876, 0.3, 0.6]):
        assert getattr(parsed, key) == expected
    monkeypatch.setenv("CLAWCHAT_MEDIA_ALLOW_HTTP", "true")
    monkeypatch.setenv("CLAWCHAT_MEDIA_ALLOW_PRIVATE_NETWORK", "1")
    assert make_config().media_allow_http is True
    assert make_config().media_allow_private_network is True


@pytest.mark.parametrize("value,expected", [
    ("AUTO", "auto"), ("On", "on"), (" OFF ", "off"),
    (True, "on"), (False, "off"),
])
def test_config_proxy_mode_extra_is_case_insensitive(make_config, value, expected):
    assert make_config({"media_proxy_mode": value}).media_proxy_mode == expected


@pytest.mark.parametrize("value,expected", [("AuTo", "auto"), (" ON ", "on"), ("OFF", "off")])
def test_config_proxy_mode_env_overrides_extra(make_config, monkeypatch, value, expected):
    monkeypatch.setenv("CLAWCHAT_MEDIA_PROXY_MODE", value)
    assert make_config({"media_proxy_mode": "invalid"}).media_proxy_mode == expected


@pytest.mark.parametrize("source", ["extra", "env"])
@pytest.mark.parametrize("value", [None, "", "invalid", "socks5", 1, [], {}])
def test_config_invalid_proxy_mode_falls_back_and_warns_once(make_config, monkeypatch, caplog, source, value):
    extra = {"media_proxy_mode": value}
    if source == "env":
        monkeypatch.setenv("CLAWCHAT_MEDIA_PROXY_MODE", str(value))
        extra = {"media_proxy_mode": "off"} if value != "" else {}
    assert make_config(extra).media_proxy_mode == "auto"
    expected = 0 if source == "env" and value == "" else 1
    assert caplog.text.count("invalid media_proxy_mode") == expected


def test_config_empty_proxy_mode_env_uses_extra(make_config, monkeypatch):
    monkeypatch.setenv("CLAWCHAT_MEDIA_PROXY_MODE", "")
    assert make_config({"media_proxy_mode": "off"}).media_proxy_mode == "off"


@pytest.mark.parametrize("value", [None, "", "nonsense", "0", "-1", 0, -1, True, False, "nan", "inf", "-inf", float("inf")])
@pytest.mark.parametrize("source", ["extra", "env"])
def test_config_invalid_numeric_limits_fall_back(make_config, monkeypatch, value, source):
    keys = [key for key, default in MEDIA_DEFAULTS.items() if type(default) in {int, float}]
    extra = {key: value for key in keys}
    if source == "env":
        for key in keys:
            monkeypatch.setenv(f"CLAWCHAT_{key.upper()}", str(value))
        extra = {key: 1 for key in keys}
        if value == "":
            # Empty env values follow media_base_url's existing extra fallback.
            extra = {}
    parsed = make_config(extra)
    for key in keys:
        assert getattr(parsed, key) == MEDIA_DEFAULTS[key]


def test_adapter_passes_limits_and_no_token(monkeypatch, make_config):
    settings = make_config(dict(media_allow_http=True, media_allow_private_network=True, media_max_bytes=100,
                                media_download_timeout_seconds=0.2, media_download_deadline_seconds=0.4,
                                media_proxy_mode="on"))
    seen = []

    async def capture(urls, **kwargs):
        seen.append((urls, kwargs))
        return []

    monkeypatch.setattr(adapter, "download_inbound_media", capture)
    inbound = SimpleNamespace(media_urls=["https://media.example.test/a"], chat_id="test-chat")
    instance = SimpleNamespace(_clawchat_config=settings)
    assert asyncio.run(adapter.ClawChatAdapter._download_inbound_media(instance, inbound)) == []
    assert seen == [(inbound.media_urls, dict(
        base_url=settings.base_url, websocket_url=settings.websocket_url,
        download_dir=settings.media_download_dir, media_base_url=settings.media_base_url,
        allow_http=True, allow_private_network=True, proxy_mode="on", max_bytes=100,
        timeout_seconds=0.2, deadline_seconds=0.4,
    ))]


def test_ipv6_zone_name_keeps_its_case():
    assert media._normalize_inbound_host("FE80::1%En0") == "fe80::1%En0"
