from redact.cli import main


def test_cli_list(capsys):
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "builtin" in out
    assert "presidio" in out


def test_cli_detect(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    rc = main(["detect", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "text" in out and "pdf" in out


def test_cli_run_dry_run(tmp_path, capsys):
    src = tmp_path / "a.txt"
    src.write_text("mail a@b.com")
    # -b builtin, so the assertion does not depend on which backends are installed
    rc = main(["run", str(src), "--dry-run", "-b", "builtin"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "builtin" in out
    # dry-run writes nothing
    assert not (tmp_path / "a.redacted.txt").exists()


def test_cli_run_writes(tmp_path, capsys):
    src = tmp_path / "a.txt"
    src.write_text("mail a@b.com")
    rc = main(["run", str(src), "-o", str(tmp_path / "out")])
    assert rc == 0
    assert (tmp_path / "out" / "a.redacted.txt").exists()


def test_cli_version(capsys):
    rc = main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "redact-suite" in out


def test_cli_no_command_shows_help(capsys):
    rc = main([])
    assert rc == 1
