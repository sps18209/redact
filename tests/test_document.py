from pathlib import Path

import pytest

from redact.document import detect_media_type, iter_documents, load_document
from redact.types import MediaType


def test_detect_by_extension(tmp_path):
    cases = {
        "a.txt": MediaType.TEXT,
        "a.csv": MediaType.STRUCTURED,
        "a.json": MediaType.STRUCTURED,
        "a.pdf": MediaType.PDF,
        "a.png": MediaType.IMAGE,
        "a.mp4": MediaType.VIDEO,
    }
    for name, expected in cases.items():
        p = tmp_path / name
        p.write_bytes(b"x")
        assert detect_media_type(p) is expected


def test_detect_by_magic_bytes(tmp_path):
    pdf = tmp_path / "noext"
    pdf.write_bytes(b"%PDF-1.7\n...")
    assert detect_media_type(pdf) is MediaType.PDF

    png = tmp_path / "img"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert detect_media_type(png) is MediaType.IMAGE


def test_detect_text_fallback(tmp_path):
    p = tmp_path / "weirdext.zzz"
    p.write_text("just some text")
    assert detect_media_type(p) is MediaType.TEXT


def test_load_document_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_document(tmp_path / "nope.txt")


def test_iter_documents_directory(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "b.csv").write_text("x,y")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "ignore.bin").write_bytes(b"\x00\x01\x02\x03")

    docs = list(iter_documents([str(tmp_path)], recursive=True))
    names = {d.name for d in docs}
    assert names == {"a.txt", "b.csv", "c.pdf"}  # unknown .bin skipped


def test_iter_documents_non_recursive(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("deep")
    docs = list(iter_documents([str(tmp_path)], recursive=False))
    assert {d.name for d in docs} == {"a.txt"}
