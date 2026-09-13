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


# -- usability: the difference between "typo" and "nothing here" --------------

def test_a_nonexistent_input_is_named(tmp_path, capsys):
    """A typo must not read the same as an empty folder."""
    rc = main(["run", str(tmp_path / "typo.txt")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no such file, directory, or glob match" in err
    assert "typo.txt" in err


def test_an_empty_directory_does_not_claim_a_missing_path(tmp_path, capsys):
    (tmp_path / "sub").mkdir()
    rc = main(["run", str(tmp_path / "sub")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no such file" not in err          # the path exists; it is just empty
    assert "no matching documents found" in err


def test_unrecognised_files_are_reported_not_silently_dropped(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("a@b.com")
    (tmp_path / "mystery.bin").write_bytes(b"\x00\x01\x02\x03")
    rc = main(["run", str(tmp_path), "-b", "builtin", "-o", str(tmp_path / "o")])
    err = capsys.readouterr().err
    assert rc == 0
    assert "skipped 1 file(s) of unrecognised type" in err
    assert "mystery.bin" in err
    assert "--include-unknown" in err


def test_list_shows_how_to_install_a_missing_backend(capsys):
    main(["list"])
    out = capsys.readouterr().out
    # every unavailable backend should offer a next step, not just a complaint
    for line in out.splitlines():
        if "needs:" in line:
            assert "├─" in line, "a 'needs' line must be followed by a hint"
    assert "pip install" in out
