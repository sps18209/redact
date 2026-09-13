from redact import RedactionOptions, RedactionSuite


def test_suite_redacts_text_file_via_builtin(tmp_path, builtin_only_suite):
    src = tmp_path / "note.txt"
    src.write_text("contact a@b.com or 123-45-6789")
    res = builtin_only_suite.redact_path(src, RedactionOptions(output_dir=tmp_path))
    assert res.success
    assert res.backend == "builtin"
    assert res.entity_count == 2
    assert res.output_path.exists()


def test_suite_batch_mixed_folder(tmp_path):
    (tmp_path / "a.txt").write_text("a@b.com")
    (tmp_path / "b.csv").write_text("name,ssn\nBob,123-45-6789")
    (tmp_path / "c.pdf").write_bytes(b"%PDF-1.4 fake")  # no pdf backend in CI
    out = tmp_path / "out"

    suite = RedactionSuite()
    results = list(
        suite.redact_paths([str(tmp_path)], RedactionOptions(output_dir=out))
    )
    by_name = {r.source.name: r for r in results}
    assert by_name["a.txt"].success
    assert by_name["b.csv"].success
    # pdf has no available backend offline -> routing failure surfaced, not raised
    assert by_name["c.pdf"].success is False
    assert "backend" in by_name["c.pdf"].message.lower()


def test_describe_backends_has_all_defaults():
    suite = RedactionSuite()
    names = {row["name"] for row in suite.describe_backends()}
    assert {
        "builtin", "presidio", "philter", "redactai",
        "pdf-redact-tools", "anonymizer",
    } <= names


def test_describe_backends_builtin_available():
    suite = RedactionSuite()
    row = next(r for r in suite.describe_backends() if r["name"] == "builtin")
    assert row["available"] is True


def test_unknown_backend_returns_failure_result(tmp_path):
    src = tmp_path / "n.txt"
    src.write_text("a@b.com")
    suite = RedactionSuite()
    res = suite.redact_path(src, RedactionOptions(backend="does-not-exist"))
    assert res.success is False
    assert "unknown backend" in res.message.lower()
