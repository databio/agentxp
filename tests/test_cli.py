"""The CLI with no model: validate and golden against the example server (loopback only)."""

from agentxp.cli import main
from conftest import EXAMPLE


def test_validate_ok_and_bad(tmp_path, capsys):
    assert main(["validate", str(EXAMPLE / "mcp_tasks.yaml"), str(EXAMPLE / "mcp_tasks_stdio.yaml")]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nserver: {name: x}\ntasks: []\n")
    assert main(["validate", str(bad)]) == 1
    assert "error" in capsys.readouterr().out


def test_golden_http_fixture(capsys):
    assert main(["golden", str(EXAMPLE / "mcp_tasks.yaml")]) == 0
    out = capsys.readouterr().out
    assert "mark-done: PASS" in out and "observer-cannot-edit: PASS" in out


def test_golden_writes_outputs(tmp_path, capsys):
    assert main(["golden", str(EXAMPLE / "mcp_tasks.yaml"), "--only", "freezer-count", "--out", str(tmp_path)]) == 0
    assert "42 boxes" in (tmp_path / "golden.jsonl").read_text()


def test_golden_stdio(capsys):
    assert main(["golden", str(EXAMPLE / "mcp_tasks_stdio.yaml")]) == 0
    assert "freezer-contact: PASS calls=1" in capsys.readouterr().out
