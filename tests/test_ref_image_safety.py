"""Run: python tests/test_ref_image_safety.py; no external network or live account."""
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


def response(payload=b"reference", mime="image/jpeg"):
    reply = io.BytesIO(payload)
    reply.headers = Message()
    reply.headers["Content-Type"] = mime
    return reply


def addresses(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
             socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in ips]


def redirect(location, code=302):
    headers = Message()
    headers["Location"] = location
    return urllib.error.HTTPError("http://public.example/ref", code,
                                  "redirect", headers, io.BytesIO())


def expect_error(call, exception, text):
    try:
        call()
    except exception as exc:
        assert text in str(exc), (text, str(exc))
    else:
        raise AssertionError(f"expected {exception.__name__}: {text}")


def check(home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="")
    for name in ("MUSE2API_REF_IMAGE_LOCAL_FILES",
                 "MUSE2API_REF_IMAGE_PRIVATE_FETCH"):
        os.environ.pop(name, None)
    from config import Config
    import engine as module

    cfg = Config()
    engine = module.MuseEngine(cfg)
    expected = (base64.b64encode(b"reference").decode(), "image/jpeg")
    fixture = Path(home) / "reference.jpg"
    fixture.write_bytes(b"reference")

    assert not cfg.ref_image_local_files and not cfg.ref_image_private_fetch
    expect_error(lambda: engine._normalize_image(str(fixture)),
                 module.RefImageSafetyError, "MUSE2API_REF_IMAGE_LOCAL_FILES=1")
    with patch.object(module.socket, "getaddrinfo",
                      return_value=addresses("127.0.0.1")), \
            patch.object(module.urllib.request, "build_opener") as build:
        expect_error(lambda: engine._normalize_image("http://localhost/ref"),
                     module.RefImageSafetyError, "MUSE2API_REF_IMAGE_PRIVATE_FETCH=1")
        build.assert_not_called()
    raw_png = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
    assert engine._normalize_image(raw_png) == (raw_png, "image/png")
    data_url = "data:image/jpeg;base64," + expected[0]
    assert engine._normalize_image(data_url) == expected
    print("safe_defaults_local_private_denied_data_and_raw_base64_allowed=PASS")

    with patch.dict(os.environ, MUSE2API_REF_IMAGE_LOCAL_FILES="1",
                    MUSE2API_REF_IMAGE_PRIVATE_FETCH="1"):
        compatible = Config()
    assert compatible.ref_image_local_files and compatible.ref_image_private_fetch
    compat_engine = module.MuseEngine(compatible)
    assert compat_engine._normalize_image(str(fixture)) == expected
    with patch.object(module.urllib.request, "urlopen",
                      return_value=response()) as open_url, \
            patch.object(module.socket, "getaddrinfo") as dns:
        assert compat_engine._normalize_image("http://127.0.0.1/ref") == expected
        open_url.assert_called_once()
        dns.assert_not_called()
    print("explicit_legacy_local_and_private_opt_ins=PASS")

    for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254",
               "::ffff:127.0.0.1", "::1", "192.168.1.1",
               "100.64.0.1", "fe80::1"):
        for ips in ((ip,), ("8.8.8.8", ip)):
            with patch.object(module.socket, "getaddrinfo",
                              return_value=addresses(*ips)), \
                    patch.object(module.urllib.request, "build_opener") as build:
                expect_error(lambda: engine._normalize_image(
                    "http://mixed.example/ref"), module.RefImageSafetyError,
                    "MUSE2API_REF_IMAGE_PRIVATE_FETCH=1")
                build.assert_not_called()
    print("private_loopback_link_local_mapped_and_mixed_dns_refused=PASS")

    for ip in ("8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"):
        opener = Mock()
        opener.open.return_value = response()
        with patch.object(module.socket, "getaddrinfo",
                          return_value=addresses(ip)) as dns, \
                patch.object(module.urllib.request, "build_opener",
                             return_value=opener) as build:
            assert engine._normalize_image("https://public.example/ref") == expected
            dns.assert_called_once_with("public.example", 443, type=socket.SOCK_STREAM)
            handlers = build.call_args.args
            assert any(isinstance(h, module.urllib.request.ProxyHandler) and
                       h.proxies == {} for h in handlers)
            assert any(isinstance(h, module._NoRefImageRedirect) for h in handlers)
            assert opener.open.call_args.args[0].full_url == "https://public.example/ref"
    print("public_ipv4_ipv6_allowed_proxy_and_auto_redirect_disabled=PASS")

    for code in (301, 302, 303, 307, 308):
        opener = Mock()
        opener.open.side_effect = redirect("http://private.example/ref", code)
        with patch.object(module.socket, "getaddrinfo", side_effect=[
                addresses("8.8.8.8"), addresses("10.0.0.1")]) as dns, \
                patch.object(module.urllib.request, "build_opener",
                             return_value=opener):
            expect_error(lambda: engine._normalize_image(
                "http://public.example/ref"), module.RefImageSafetyError,
                "MUSE2API_REF_IMAGE_PRIVATE_FETCH=1")
            assert [call.args[0] for call in dns.call_args_list] == [
                "public.example", "private.example"]
            assert opener.open.call_count == 1
    opener = Mock()
    opener.open.side_effect = [redirect("/next"), response()]
    with patch.object(module.socket, "getaddrinfo",
                      return_value=addresses("8.8.8.8")) as dns, \
            patch.object(module.urllib.request, "build_opener",
                         return_value=opener):
        assert engine._normalize_image("http://public.example/ref") == expected
        assert dns.call_count == 2
        assert opener.open.call_args.args[0].full_url == "http://public.example/next"
    print("all_redirect_codes_rechecked_private_refused_public_allowed=PASS")

    # The real opener must not resolve the hostname again after validation.
    for scheme, port in (("http", 8080), ("https", 443)):
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
            b"Content-Length: 9\r\n\r\nreference")
        with patch.object(module.socket, "getaddrinfo", side_effect=[
                addresses("8.8.8.8"), AssertionError("hostname resolved twice")]) as dns, \
                patch.object(module.socket, "create_connection",
                             return_value=sock) as connect, \
                patch.object(ssl.SSLContext, "wrap_socket", return_value=sock) as tls:
            assert engine._normalize_image(
                f"{scheme}://public.example:{port}/ref") == expected
            connect.assert_called_once_with(("8.8.8.8", port), 20, None)
            assert dns.call_count == 1
            sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
            assert f"Host: public.example:{port}\r\n".encode() in sent
            if scheme == "https":
                assert tls.call_args.kwargs["server_hostname"] == "public.example"
            else:
                tls.assert_not_called()
    print("checked_ip_pinned_original_host_and_tls_sni=PASS")

    for private_fetch in (True, False):
        cfg.ref_image_private_fetch = private_fetch
        with patch.object(module.urllib.request, "urlopen",
                          side_effect=OSError("fixture failure")), \
                patch.object(module.socket, "getaddrinfo",
                             side_effect=socket.gaierror("fixture failure")):
            expect_error(lambda: engine._normalize_image(
                "https://failed.example/ref"), module.MuseGenerationError,
                "下载远程参考图失败")
    cfg.ref_image_private_fetch = False
    asyncio.run(check_endpoints(home, data_url))


async def check_endpoints(home, data_url):
    import app
    from test_async_images import Client

    app.CFG.ref_image_local_files = False
    app.CFG.ref_image_private_fetch = False
    variants = [
        ("/v1/images/generations", field, async_mode)
        for field in ("reference_image", "image", "images")
        for async_mode in (False, True)
    ] + [
        ("/v1/images/tasks", field, True)
        for field in ("reference_image", "image", "images")
    ] + [
        ("/v1/images/edits", field, async_mode)
        for field in ("reference_image", "image", "images", "image_url")
        for async_mode in (False, True)
    ] + [
        ("/v1/videos", field, True)
        for field in ("reference_image", "image", "image_url", "images")
    ]
    async with Client(app.app, {"Authorization": "Bearer test-only"}) as client:
        with patch.object(app, "_run_generation") as generate, \
                patch.object(app.store, "create_task") as create_task:
            for value in (str(Path(home) / "reference.jpg"),
                          "http://private.example/ref",
                          "http://public.example/redirect"):
                for endpoint, field, async_mode in variants:
                    body = {"prompt": "fixture", "async": async_mode,
                            field: [{"image_url": value}]
                            if field == "images" else value}
                    opener = Mock()
                    opener.open.side_effect = redirect("http://private.example/ref")
                    dns = lambda host, *a, **kw: addresses(
                        "8.8.8.8" if host == "public.example" else "127.0.0.1")
                    with patch("engine.socket.getaddrinfo", side_effect=dns), \
                            patch("engine.urllib.request.build_opener",
                                  return_value=opener):
                        reply = await client.post(endpoint, json=body)
                    assert reply.status_code == 400, (
                        endpoint, field, reply.status_code, reply.body)
                    message = reply.json()["error"]["message"]
                    assert "images[0]" in message and "MUSE2API_REF_IMAGE_" in message
            generate.assert_not_called()
            create_task.assert_not_called()

        prepared = app._prepare_reference_images([data_url] * 4)
        assert prepared == [data_url] * 4
        app.CFG.ref_image_local_files = True
        local = app._prepare_reference_images([
            str(Path(home) / "reference.jpg"), data_url])
        assert len(local) == 2 and all(value.startswith("data:image/") for value in local)
    print("all_endpoints_reject_before_task_multi_reference_prepared_in_order=PASS")
    print("PASS reference image safety regression")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="muse-ref-image-test-") as home, \
            patch("socket.create_connection",
                  side_effect=AssertionError("unexpected network")):
        check(home)
