"""Run: python tests/test_ref_image_safety.py; no network, browser or live account."""
import asyncio
import base64
from email.message import Message
import io
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import urllib.error
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def response():
    reply = io.BytesIO(b"reference")
    reply.headers = Message()
    reply.headers["Content-Type"] = "image/jpeg"
    return reply


def addresses(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
             socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in ips]


def redirect(location, code=302):
    headers = Message()
    headers["Location"] = location
    return urllib.error.HTTPError("http://public.example/ref", code,
                                  "redirect", headers, io.BytesIO())


def check(home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="")
    for name in ("MUSE2API_REF_IMAGE_LOCAL_FILES", "MUSE2API_REF_IMAGE_PRIVATE_FETCH"):
        os.environ.pop(name, None)
    from config import Config
    import engine as module
    cfg = Config()
    engine = module.MuseEngine(cfg)
    expected = (base64.b64encode(b"reference").decode(), "image/jpeg")
    fixture = Path(home) / "reference.jpg"
    fixture.write_bytes(b"reference")

    assert cfg.ref_image_local_files and cfg.ref_image_private_fetch
    assert engine._normalize_image(str(fixture)) == expected
    assert engine._normalize_image("cmVmZXJlbmNl") == ("cmVmZXJlbmNl", "image/png")
    with patch.object(module.urllib.request, "urlopen", return_value=response()) as open_url, \
            patch.object(module.socket, "getaddrinfo") as dns:
        assert engine._normalize_image("http://127.0.0.1/ref") == expected
        open_url.assert_called_once()
        dns.assert_not_called()
    with patch.dict(os.environ, MUSE2API_REF_IMAGE_LOCAL_FILES=" 0 ",
                    MUSE2API_REF_IMAGE_PRIVATE_FETCH="0"):
        restricted = Config()
        assert not restricted.ref_image_local_files and not restricted.ref_image_private_fetch
    print("defaults_keep_local_raw_base64_and_unrestricted_urlopen=PASS config_switches=PASS")

    cfg.ref_image_local_files = False
    with patch("builtins.open") as read_file, patch.object(module.os.path, "isfile") as stat:
        for value in (str(fixture), str(Path(home) / "missing.png"), "cmVmZXJlbmNl"):
            try:
                engine._normalize_image(value)
                raise AssertionError("non-URL input accepted")
            except module.RefImageSafetyError as exc:
                assert "MUSE2API_REF_IMAGE_LOCAL_FILES=0" in str(exc)
        read_file.assert_not_called()
        stat.assert_not_called()
    data_url = "data:image/jpeg;base64," + expected[0]
    assert engine._normalize_image(data_url) == expected
    print("local_paths_refused_without_reading_and_data_url_accepted=PASS")

    cfg.ref_image_private_fetch = False
    for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::ffff:127.0.0.1",
               "::1", "192.168.1.1", "100.64.0.1", "fe80::1"):
        for ips in ((ip,), ("8.8.8.8", ip)):
            with patch.object(module.socket, "getaddrinfo", return_value=addresses(*ips)), \
                    patch.object(module.urllib.request, "build_opener") as build:
                try:
                    engine._normalize_image("http://mixed.example/ref")
                    raise AssertionError("non-global DNS address accepted: " + ip)
                except module.RefImageSafetyError as exc:
                    assert "MUSE2API_REF_IMAGE_PRIVATE_FETCH=0" in str(exc)
                build.assert_not_called()
    print("private_loopback_link_local_mapped_and_mixed_dns_refused=PASS")

    for ip in ("8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"):
        opener = Mock()
        opener.open.return_value = response()
        with patch.object(module.socket, "getaddrinfo", return_value=addresses(ip)) as dns, \
                patch.object(module.urllib.request, "build_opener", return_value=opener) as build:
            assert engine._normalize_image("https://public.example/ref") == expected
            dns.assert_called_once_with("public.example", 443, type=socket.SOCK_STREAM)
            handlers = build.call_args.args
            assert any(isinstance(h, module.urllib.request.ProxyHandler) and h.proxies == {}
                       for h in handlers)
            assert any(isinstance(h, module._NoRefImageRedirect) for h in handlers)
            opener.open.assert_called_once()
            assert opener.open.call_args.args[0].full_url == "https://public.example/ref"
    assert module._NoRefImageRedirect().redirect_request(None, None, 302, "", {}, "x") is None
    print("public_ipv4_ipv6_and_mapped_allowed_proxy_and_auto_redirect_disabled=PASS")

    for code in (301, 302, 303, 307, 308):
        opener = Mock()
        opener.open.side_effect = redirect("http://private.example/ref", code)
        with patch.object(module.socket, "getaddrinfo", side_effect=[
                addresses("8.8.8.8"), addresses("10.0.0.1")]) as dns, \
                patch.object(module.urllib.request, "build_opener", return_value=opener):
            try:
                engine._normalize_image("http://public.example/ref")
                raise AssertionError("private redirect accepted")
            except module.RefImageSafetyError:
                pass
            assert [call.args[0] for call in dns.call_args_list] == ["public.example", "private.example"]
            assert opener.open.call_count == 1
    opener = Mock()
    opener.open.side_effect = [redirect("/next"), response()]
    with patch.object(module.socket, "getaddrinfo", return_value=addresses("8.8.8.8")) as dns, \
            patch.object(module.urllib.request, "build_opener", return_value=opener):
        assert engine._normalize_image("http://public.example/ref") == expected
        assert dns.call_count == 2
        assert opener.open.call_args.args[0].full_url == "http://public.example/next"
    print("all_redirect_codes_checked_private_refused_relative_public_allowed=PASS")

    # Exercise the actual opener with fake sockets: a second DNS lookup cannot change its target.
    for scheme, port in (("http", 8080), ("https", 443)):
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: 9\r\n\r\nreference")
        with patch.object(module.socket, "getaddrinfo", side_effect=[addresses("8.8.8.8"),
                          AssertionError("hostname resolved twice")]) as dns, \
                patch.object(module.socket, "create_connection", return_value=sock) as connect, \
                patch.object(ssl.SSLContext, "wrap_socket", return_value=sock) as tls:
            assert engine._normalize_image(f"{scheme}://public.example:{port}/ref") == expected
            connect.assert_called_once_with(("8.8.8.8", port), 20, None)
            assert dns.call_count == 1
            sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
            host = f"public.example:{port}"
            assert f"Host: {host}\r\n".encode() in sent
            if scheme == "https":
                assert tls.call_args.kwargs["server_hostname"] == "public.example"
            else:
                tls.assert_not_called()
    print("checked_ip_connected_without_second_dns_original_host_and_tls_sni=PASS")

    for private_fetch in (True, False):
        cfg.ref_image_private_fetch = private_fetch
        with patch.object(module.urllib.request, "urlopen", side_effect=OSError("fixture failure")), \
                patch.object(module.socket, "getaddrinfo", side_effect=socket.gaierror("fixture failure")):
            assert engine._normalize_image("https://failed.example/ref") == ("", "image/png")
    print("ordinary_download_failures_unchanged=PASS")
    asyncio.run(check_endpoints(home, data_url))


async def check_endpoints(home, data_url):
    import app
    from test_async_images import Client
    app.CFG.ref_image_local_files = False
    app.CFG.ref_image_private_fetch = False
    variants = [
        ("/v1/images/generations", fields, async_mode)
        for fields in ("reference_image", "image", "images") for async_mode in (False, True)
    ] + [
        ("/v1/images/tasks", fields, True) for fields in ("reference_image", "image", "images")
    ] + [
        ("/v1/images/edits", fields, async_mode)
        for fields in ("reference_image", "image", "images", "image_url") for async_mode in (False, True)
    ] + [
        ("/v1/videos", fields, True) for fields in ("reference_image", "image", "image_url")
    ]
    async with Client(app.app, {"Authorization": "Bearer test-only"}) as client:
        with patch.object(app, "_run_generation") as generate, \
                patch.object(app.store, "create_task") as create_task:
            for value in (str(Path(home) / "reference.jpg"), "http://private.example/ref",
                          "http://public.example/redirect"):
                for endpoint, field, async_mode in variants:
                    body = {"prompt": "fixture", "async": async_mode,
                            field: [{"image_url": value}] if field == "images" else value}
                    opener = Mock()
                    opener.open.side_effect = redirect("http://private.example/ref")
                    dns = (lambda host, *a, **kw: addresses(
                        "8.8.8.8" if host == "public.example" else "127.0.0.1"))
                    with patch("engine.socket.getaddrinfo", side_effect=dns), \
                            patch("engine.urllib.request.build_opener", return_value=opener):
                        reply = await client.post(endpoint, json=body)
                    assert reply.status_code == 400, (endpoint, field, reply.status_code, reply.body)
                    assert "MUSE2API_REF_IMAGE_" in reply.json()["error"]["message"]
            generate.assert_not_called()
            create_task.assert_not_called()
        assert app._prepare_reference_image(data_url) == data_url
        # A normal download failure must remain a truthy reference that fails attachment.
        with patch("engine.socket.getaddrinfo", side_effect=socket.gaierror("fixture")):
            failed = app._prepare_reference_image("http://failed.example/ref")
        assert failed == "data:image/png;base64,"
        try:
            app.engine._attach_image(failed)
            raise AssertionError("failed download silently discarded")
        except app.MuseGenerationError:
            pass
    print("all_endpoint_fields_sync_async_return_400_before_generation_or_queueing=PASS")
    print("PASS reference image safety regression")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="muse-ref-image-test-") as home, \
            patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
        check(home)
