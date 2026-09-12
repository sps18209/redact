"""Output naming and source-tree mirroring under an output directory."""

from pathlib import Path

from redact import RedactionOptions, RedactionSuite
from redact.document import Document, _glob_root, iter_documents, output_path
from redact.types import MediaType


def _doc(path, root=None):
    return Document(path=Path(path), media_type=MediaType.TEXT, root=Path(root) if root else None)


def test_flat_beside_source_without_output_dir():
    assert output_path(_doc("/data/in/x.txt"), RedactionOptions()) == Path("/data/in/x.redacted.txt")


def test_single_file_lands_in_output_root(tmp_path):
    out = output_path(_doc("/data/in/x.txt"), RedactionOptions(output_dir=tmp_path))
    assert out == tmp_path / "x.redacted.txt"


def test_relative_dir_is_mirrored(tmp_path):
    doc = _doc("inbox/a/deep/x.txt", root="inbox")
    out = output_path(doc, RedactionOptions(output_dir=tmp_path))
    assert out == tmp_path / "a" / "deep" / "x.redacted.txt"


def test_suffix_override_for_sidecars():
    doc = _doc("inbox/r.pdf", root="inbox")
    assert output_path(doc, RedactionOptions(), suffix=".txt").name == "r.redacted.txt"


def test_path_outside_root_falls_back_flat(tmp_path):
    doc = _doc("/elsewhere/x.txt", root="/inbox")
    assert output_path(doc, RedactionOptions(output_dir=tmp_path)) == tmp_path / "x.redacted.txt"


def test_glob_root_extraction():
    assert _glob_root("inbox/**/*.txt") == Path("inbox")
    assert _glob_root("/abs/dir/*.csv") == Path("/abs/dir")
    assert _glob_root("*.txt") == Path()


def test_directory_ingestion_sets_root(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x")
    docs = list(iter_documents([str(tmp_path)]))
    assert docs[0].root == tmp_path


def test_same_name_in_two_folders_does_not_collide(tmp_path):
    inbox = tmp_path / "inbox"
    for sub, mail in (("a", "one@x.io"), ("b", "two@y.io")):
        (inbox / sub).mkdir(parents=True)
        (inbox / sub / "x.txt").write_text(mail)
    out = tmp_path / "out"

    results = list(RedactionSuite().redact_paths([str(inbox)], RedactionOptions(output_dir=out)))
    assert all(r.success for r in results)
    assert (out / "a" / "x.redacted.txt").exists()
    assert (out / "b" / "x.redacted.txt").exists()
    assert {r.output_path for r in results} == {out / "a" / "x.redacted.txt", out / "b" / "x.redacted.txt"}


def test_glob_input_mirrors_from_its_fixed_prefix(tmp_path):
    inbox = tmp_path / "inbox"
    (inbox / "deep").mkdir(parents=True)
    (inbox / "deep" / "n.txt").write_text("a@b.com")
    out = tmp_path / "out"
    results = list(
        RedactionSuite().redact_paths([str(inbox / "**" / "*.txt")], RedactionOptions(output_dir=out))
    )
    assert results and results[0].output_path == out / "deep" / "n.redacted.txt"
