import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from redact import RedactionOptions, RedactionSuite
from redact.backends import LocalOllamaBackend, PhilterBackend, RedactAIBackend
from redact.cli import main
from redact.document import load_document


@pytest.fixture
def ollama(monkeypatch):
    requests = []
    responses = {
        "/api/status": (200, {"cloud": {"disabled": True}}),
        "/api/show": (200, {"model_info": {"general.architecture": "llama"}}),
        "/api/generate": (200, {"done": True, "done_reason": "stop", "response": "[]"}),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond(None)

        def do_POST(self):
            self.respond(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def respond(self, payload):
            requests.append((self.path, payload))
            status, body = responses[self.path]
            if callable(body):
                body = body(payload)
            self.send_response(status)
            if status == 307:
                self.send_header("Location", "/api/generate")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("OLLAMA_MODEL", "llama3")
        try:
            yield requests, responses
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("path,status,body", [
    ("/api/status", 200, {"cloud": {"disabled": False}}),
    ("/api/status", 404, {}),
    ("/api/status", 307, {}),
    ("/api/show", 200, {"remote_host": "example.invalid", "model_info": {"size": 1}}),
    ("/api/show", 404, {}),
])
def test_failed_local_preflight_sends_zero_document_bytes(ollama, tmp_path, path, status, body):
    requests, responses = ollama
    responses[path] = status, body
    src = tmp_path / "source.txt"
    src.write_text("Sensitive source text.")
    result = RedactionSuite().redact_path(src, RedactionOptions(backend="ollama-local", dry_run=True))
    assert not result.success
    assert all(p != "/api/generate" and "Sensitive source text." not in str(data) for p, data in requests)
    assert result.output_path is None


def test_nonlocal_ollama_host_rejected_without_connecting(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://example.invalid")
    src = tmp_path / "source.txt"
    src.write_text("Sensitive source text.")
    result = LocalOllamaBackend().redact(load_document(src), RedactionOptions())
    assert not result.success
    assert "loopback" in result.message


def test_guide_selects_local_model_and_redacts_all_repeats(ollama, tmp_path, monkeypatch):
    requests, responses = ollama
    responses["/api/generate"] = 200, {
        "done": True, "done_reason": "stop",
        "response": json.dumps([{"text": "Sample Name", "type": "Sample Name"}]),
    }
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    src = tmp_path / "source.txt"
    src.write_text("Sample Name and Sample Name. Mail demo@example.com")
    monkeypatch.setattr("sys.stdin", io.StringIO("6\n\n"))
    assert main(["guide", str(src)]) == 0
    output = tmp_path / "source-redacted" / "source.redacted.txt"
    assert output.read_text() == "<SENSITIVE> and <SENSITIVE>. Mail <EMAIL_ADDRESS>"
    assert [path for path, _ in requests] == ["/api/status", "/api/show", "/api/generate"]
    assert requests[-1][1]["model"] == "llama3:local"
    assert requests[-1][1]["truncate"] is False
    requests.clear()
    monkeypatch.setattr("sys.stdin", io.StringIO("6\n\n"))
    assert main(["guide", str(src)]) == 1
    assert requests == []
    assert output.read_text().count("<SENSITIVE>") == 2


@pytest.mark.parametrize("reply", [
    {"done": True, "done_reason": "length", "response": "[]"},
    {"done": True, "done_reason": "stop", "response": "I refuse."},
    {"done": True, "done_reason": "stop", "response": '[{"text": 42}]'},
    {"done": True, "done_reason": "stop", "response": '[{"text": "hallucination"}]'},
])
def test_failure_in_later_section_produces_no_artifact(ollama, tmp_path, reply):
    requests, responses = ollama

    def finish_one_section(payload):
        if "END OF SOURCE" in payload["prompt"]:
            return reply
        return {"done": True, "done_reason": "stop", "response": "[]"}

    responses["/api/generate"] = 200, finish_one_section
    src = tmp_path / "source.txt"
    src.write_text("A paragraph. " * 200 + "END OF SOURCE")
    result = RedactionSuite().redact_path(src, RedactionOptions(backend="ollama-local"))
    generated = [payload["prompt"] for path, payload in requests if path == "/api/generate"]
    assert len(generated) == 2
    assert generated[0][-256:] in generated[1]
    assert not result.success
    assert result.redacted_text is None
    assert not (tmp_path / "source.redacted.txt").exists()


@pytest.mark.parametrize("backend", [PhilterBackend, RedactAIBackend])
def test_direct_service_calls_require_consent_even_in_dry_run(backend, tmp_path, monkeypatch):
    def reject_network(*args, **kwargs):
        pytest.fail("service contacted without consent")

    monkeypatch.setattr("urllib.request.urlopen", reject_network)
    src = tmp_path / "source.txt"
    src.write_text("Sensitive source text.")
    result = backend().redact(load_document(src), RedactionOptions(dry_run=True))
    assert not result.success
    assert "--allow-service" in result.message
