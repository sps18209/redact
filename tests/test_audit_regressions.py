"""Regression tests for defects found in the post-build audit."""

import stat
import sys
from pathlib import Path

import pytest

from redact.media import ffmpeg_bin as _ffmpeg_bin

from redact.backends.anonymizer import AnonymizerBackend
from redact.backends.base import Backend
from redact.backends.builtin import detect_entities
from redact.cli import _parse_entities, build_parser, main
from redact.document import Document, _expand_input, is_redaction_output, iter_documents, output_path
from redact.types import MediaType, RedactionOptions


# -- builtin recognizers -----------------------------------------------------

def test_clock_times_are_not_ip_addresses():
    ents = detect_entities("job ran at 10:23:45 and 2026-09-12 14:05:07")
    assert not any(e.entity_type == "IP_ADDRESS" for e in ents)


def test_real_ipv6_still_detected():
    ents = detect_entities("host 2001:0db8:85a3:0000:0000:8a2e:0370:7334 up")
    assert any(e.entity_type == "IP_ADDRESS" for e in ents)


def test_credit_card_does_not_swallow_trailing_separator():
    ents = [e for e in detect_entities("card 4111 1111 1111 1111 now") if e.entity_type == "CREDIT_CARD"]
    assert len(ents) == 1
    assert ents[0].text == "4111 1111 1111 1111"


# -- ingestion ---------------------------------------------------------------

def test_absolute_glob_pattern_does_not_crash(tmp_path):
    (tmp_path / "x.txt").write_text("hi")
    _, matches = _expand_input(str(tmp_path / "*.txt"), recursive=True)
    assert [m.name for m in matches] == ["x.txt"]


def test_is_redaction_output_patterns():
    assert is_redaction_output(Path("a.redacted.txt"))
    assert is_redaction_output(Path("HEAD.redacted"))
    assert is_redaction_output(Path("doc-final.pdf"))
    assert not is_redaction_output(Path("a.txt"))
    assert not is_redaction_output(Path("final-report.pdf"))


def test_batch_skips_prior_outputs_and_hidden_dirs(tmp_path):
    (tmp_path / "a.txt").write_text("a@b.com")
    (tmp_path / "a.redacted.txt").write_text("<EMAIL_ADDRESS>")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "index.js").write_text("x@y.io")

    names = {d.name for d in iter_documents([str(tmp_path)])}
    assert names == {"a.txt"}


def test_running_twice_is_idempotent(tmp_path):
    from redact import RedactionSuite

    (tmp_path / "a.txt").write_text("a@b.com")
    suite = RedactionSuite()
    list(suite.redact_paths([str(tmp_path)]))
    list(suite.redact_paths([str(tmp_path)]))
    produced = sorted(p.name for p in tmp_path.iterdir())
    assert produced == ["a.redacted.txt", "a.txt"]  # no a.redacted.redacted.txt


# -- output naming -----------------------------------------------------------

def test_redactai_sidecar_name_is_not_doubled(tmp_path):
    doc = Document(path=Path("report.pdf"), media_type=MediaType.PDF)
    out = output_path(doc, RedactionOptions(output_dir=tmp_path), suffix=".txt")
    assert out.name == "report.redacted.txt"
    doc = Document(path=Path("notes.txt"), media_type=MediaType.TEXT)
    assert output_path(doc, RedactionOptions(), suffix=".txt").name == "notes.redacted.txt"


# -- CLI ---------------------------------------------------------------------

def test_entities_flag_before_inputs_no_longer_swallows_files():
    args = build_parser().parse_args(["run", "-e", "EMAIL_ADDRESS", "notes.txt"])
    assert args.inputs == ["notes.txt"]
    assert _parse_entities(args.entities) == ["EMAIL_ADDRESS"]


def test_entities_flag_accepts_commas_and_repeats():
    args = build_parser().parse_args(
        ["run", "-e", "email_address,US_SSN", "-e", "PHONE_NUMBER", "notes.txt"]
    )
    assert _parse_entities(args.entities) == ["EMAIL_ADDRESS", "US_SSN", "PHONE_NUMBER"]


def test_entities_empty_means_all():
    assert _parse_entities(None) is None
    assert _parse_entities([""]) is None


def test_cli_entities_restrict_end_to_end(tmp_path, capsys):
    src = tmp_path / "a.txt"
    src.write_text("a@b.com and 123-45-6789")
    rc = main(["run", "-e", "EMAIL_ADDRESS", str(src), "-o", str(tmp_path / "o")])
    assert rc == 0
    out = (tmp_path / "o" / "a.redacted.txt").read_text()
    assert "<EMAIL_ADDRESS>" in out and "123-45-6789" in out


# -- availability caching ----------------------------------------------------

class _CountingBackend(Backend):
    name = "counting"
    supported_media_types = (MediaType.TEXT,)

    def __init__(self):
        self.probes = 0

    def missing_dependencies(self):
        self.probes += 1
        return []

    def redact(self, document, options):  # pragma: no cover
        raise NotImplementedError


def test_is_available_is_memoised():
    b = _CountingBackend()
    for _ in range(10):
        assert b.is_available()
    assert b.probes == 1
    b.refresh_availability()
    assert b.probes == 2


# -- anonymizer adapter wiring (fake CLI) ------------------------------------

@pytest.fixture
def fake_anonymizer(tmp_path, monkeypatch):
    """A stand-in for upstream's CLI that just copies input images to output."""
    script = tmp_path / "fake_anonymize"
    script.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do case \"$1\" in --input) IN=$2; shift;; --image-output) OUT=$2; shift;; *) ;; esac; shift; done\n"
        "cp \"$IN\"/* \"$OUT\"/\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.delenv("ANONYMIZER_HOME", raising=False)
    monkeypatch.setenv("ANONYMIZER_BIN", str(script))
    return script


@pytest.mark.skipif(sys.platform.startswith("win"), reason="sh script")
def test_anonymizer_image_path_with_fake_cli(tmp_path, fake_anonymizer):
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    backend = AnonymizerBackend()
    assert backend.is_available()

    res = backend.redact(
        Document(path=img, media_type=MediaType.IMAGE),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert res.output_path == tmp_path / "out" / "photo.redacted.png"
    assert res.output_path.read_bytes() == img.read_bytes()
    assert img.exists()  # original untouched


def test_anonymizer_unavailable_without_config(monkeypatch):
    monkeypatch.delenv("ANONYMIZER_HOME", raising=False)
    monkeypatch.delenv("ANONYMIZER_BIN", raising=False)
    assert AnonymizerBackend().missing_dependencies()


# Presence is not usability: a system ffmpeg whose shared libraries moved (a
# package manager bumping x265 under it) is still on PATH and still fails. Guard
# on "does it run", or this skips nothing and dies with a linker error instead.
@pytest.mark.skipif(_ffmpeg_bin() is None, reason="no usable ffmpeg")
def test_anonymizer_video_roundtrip_with_fake_cli(tmp_path, fake_anonymizer):
    import subprocess

    video = tmp_path / "clip.mp4"
    # Use the binary the suite itself resolved, not the bare name: the guard
    # above passes when only the bundled static build is usable, and that one is
    # not on PATH.
    subprocess.run(
        [_ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=red:s=64x64:d=1", "-r", "10", str(video)],
        check=True,
    )
    res = AnonymizerBackend().redact(
        Document(path=video, media_type=MediaType.VIDEO),
        RedactionOptions(output_dir=tmp_path / "out"),
    )
    assert res.success, res.message
    assert res.output_path.name == "clip.redacted.mp4"
    assert res.output_path.stat().st_size > 0


def test_anonymizer_video_reports_missing_ffmpeg(tmp_path, fake_anonymizer, monkeypatch):
    monkeypatch.setattr("redact.backends.anonymizer._ffmpeg_missing", lambda: ["ffmpeg"])
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    res = AnonymizerBackend().redact(
        Document(path=video, media_type=MediaType.VIDEO), RedactionOptions()
    )
    assert res.success is False and "ffmpeg" in res.message
