"""The Hugging Face client refuses to START a download of a file over 50 GB unless Xet is available ("The file is too large to be downloaded using
the regular download method ... Install hf_xet"). ModelDepot does not use Xet, so the ingester lifts that guard; without it a repo with one 55 GB part
failed after its smaller parts had arrived (2026-11-05).

Offline: a local HTTP server stands in for the Hub, and the client's own `http_get` is asked for a 60 GB file. Run live against huggingface.co the same
way (hf_hub_download of a 55 GB GGUF): unpatched it raised the error above, patched it streamed from byte 0 and a restart resumed from the partial file.
"""
from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ingester import hf_source

constants = pytest.importorskip("huggingface_hub.constants")
file_download = pytest.importorskip("huggingface_hub.file_download")
if not hasattr(constants, "MAX_HTTP_DOWNLOAD_SIZE"):          # a client old enough to have no guard has nothing to lift
    pytest.skip("this huggingface_hub has no download size guard", allow_module_level=True)

SIXTY_GB = 60 * 1000 ** 3


@pytest.fixture
def server():
    hits: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"x" * 4096
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/big.gguf", hits
    srv.shutdown()


def _get(url):
    file_download.http_get(url, io.BytesIO(), expected_size=SIXTY_GB)


def test_the_client_refuses_a_file_over_50_gb_by_default(monkeypatch, server):
    """Positive control: with the client's own limit the error of the owner's log comes before any request is made."""
    url, hits = server
    monkeypatch.setattr(constants, "MAX_HTTP_DOWNLOAD_SIZE", 50 * 1000 ** 3)       # restored after the test
    with pytest.raises(ValueError, match="too large to be downloaded"):
        _get(url)
    assert hits == []


def test_lifting_the_guard_lets_the_download_start(monkeypatch, server):
    """After the lift the stream starts (the server is asked); it then ends in a size-mismatch error because the stand-in sends 4 KB, not 60 GB."""
    url, hits = server
    monkeypatch.setattr(constants, "MAX_HTTP_DOWNLOAD_SIZE", 50 * 1000 ** 3)
    hf_source._lift_size_guard()
    with pytest.raises(Exception) as err:
        _get(url)
    assert "too large to be downloaded" not in str(err.value)
    assert hits, "the guard was lifted but no request reached the server"
