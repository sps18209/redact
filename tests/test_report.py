"""Machine-readable output — for pipelines, and for models asked what a run found.

The governing constraint is that the report must not become the leak.
`Entity.text` holds the value that was found: the SSN, the email, the card. A
JSON log listing them is a fresh copy of exactly what the run existed to remove,
and nobody treats a log file as sensitive. So values are withheld by default and
every payload states which mode produced it.
"""

import json

import pytest

from redact.cli import main

SSN = "078-05-1120"
EMAIL = "jane.doe@example.com"


@pytest.fixture
def inbox(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.txt").write_text(f"SSN {SSN} mail {EMAIL}")
    return src


def _run_json(inbox, tmp_path, *extra):
    out = tmp_path / "report.json"
    main([
        "run", str(inbox), "-b", "builtin",
        "-o", str(tmp_path / "clean"), "--json", str(out), *extra,
    ])
    return json.loads(out.read_text()), out


# -- the report must not become the leak --------------------------------------

def test_values_are_withheld_by_default(inbox, tmp_path):
    payload, path = _run_json(inbox, tmp_path)
    raw = path.read_text()
    assert SSN not in raw, "the report reproduced the SSN it just removed"
    assert EMAIL not in raw
    assert payload["contains_pii_values"] is False
    entities = [e for d in payload["documents"] for e in d["entities"]]
    assert entities, "precondition: something was detected"
    assert all("text" not in e for e in entities)


def test_it_is_still_useful_without_the_values(inbox, tmp_path):
    """Withholding values must not make the report say nothing."""
    payload, _ = _run_json(inbox, tmp_path)
    doc = payload["documents"][0]
    assert doc["entity_counts"] == {"EMAIL_ADDRESS": 1, "US_SSN": 1}
    assert doc["entity_count"] == 2


def test_opting_in_includes_the_values_and_says_so(inbox, tmp_path, capsys):
    payload, path = _run_json(inbox, tmp_path, "--json-include-values")
    assert payload["contains_pii_values"] is True
    assert SSN in path.read_text()
    assert "as sensitive as the source documents" in capsys.readouterr().err


# -- the shape automation branches on -----------------------------------------

def test_the_payload_is_versioned(inbox, tmp_path):
    payload, _ = _run_json(inbox, tmp_path)
    assert payload["schema"] == "redact-suite/run-report"
    assert payload["schema_version"]
    assert payload["tool_version"]
    assert payload["generated_at"].endswith("+00:00")


def test_summary_carries_the_one_boolean_that_matters(inbox, tmp_path):
    payload, _ = _run_json(inbox, tmp_path)
    assert payload["summary"]["all_safe"] is True
    assert payload["summary"]["exit_code"] == 0


def test_an_incomplete_run_is_not_all_safe(tmp_path):
    """success can be True while content was knowingly left behind."""
    import base64

    src = tmp_path / "in"
    src.mkdir()
    payload_b64 = base64.b64encode(b"x").decode()
    (src / "m.eml").write_text(
        "From: a@b.com\nSubject: x\nMIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="B"\n\n'
        "--B\nContent-Type: text/plain\n\nbody\n"
        '--B\nContent-Type: application/pdf; name="r.pdf"\n'
        "Content-Transfer-Encoding: base64\n"
        'Content-Disposition: attachment; filename="r.pdf"\n\n'
        f"{payload_b64}\n--B--\n"
    )
    out = tmp_path / "r.json"
    code = main(["run", str(src), "-b", "builtin", "-o", str(tmp_path / "c"),
                 "--json", str(out)])
    payload = json.loads(out.read_text())
    doc = payload["documents"][0]
    assert code == 1
    assert doc["success"] is True
    assert doc["fully_redacted"] is False, "the distinction automation needs"
    assert doc["unredacted"]
    assert payload["summary"]["all_safe"] is False
    assert payload["summary"]["exit_code"] == 1


# -- stdout must be parseable -------------------------------------------------

def test_json_to_stdout_is_not_mixed_with_prose(inbox, tmp_path, capsys):
    """`redact run ... --json - | jq` has to work, so the prose moves to stderr."""
    main(["run", str(inbox), "-b", "builtin", "-o", str(tmp_path / "clean"), "--json", "-"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # raises if a human line leaked in
    assert payload["schema"] == "redact-suite/run-report"
    assert "[ok]" in captured.err, "the human output should still be shown"


def test_verify_emits_its_own_schema(inbox, tmp_path, capsys):
    main(["run", str(inbox), "-b", "builtin", "-o", str(tmp_path / "clean")])
    capsys.readouterr()
    main(["verify", str(tmp_path / "clean"), "--json", "-"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "redact-suite/verify-report"
    assert payload["summary"]["all_verified_clean"] is True
    assert payload["artifacts"][0]["status"] == "clean"


def test_verify_report_withholds_values_by_default(tmp_path, capsys):
    leak = tmp_path / "leak.txt"
    leak.write_text(f"ssn {SSN}")
    main(["verify", str(leak), "--json", "-"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert SSN not in out
    assert payload["summary"]["leaking"] == 1
    assert payload["artifacts"][0]["findings"][0]["type"] == "US_SSN"
    assert "text" not in payload["artifacts"][0]["findings"][0]


# -- reporting must never change the outcome ----------------------------------

def test_an_unwritable_report_does_not_fail_the_run(inbox, tmp_path, capsys):
    """The redaction already happened; a logging problem must not mask it."""
    code = main([
        "run", str(inbox), "-b", "builtin", "-o", str(tmp_path / "clean"),
        "--json", str(tmp_path / "clean" / "a.redacted.txt" / "nope.json"),
    ])
    assert code == 0, "the run succeeded; only the report could not be written"
    assert "could not write JSON report" in capsys.readouterr().err
