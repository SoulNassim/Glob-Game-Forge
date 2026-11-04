"""Coding-agent CLIs as Glob chat models, run against fake CLIs on PATH."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from forge.agent import cli_models, llm

FAKE_CLAUDE = """
import json, sys
args = sys.argv[1:]
msg = json.loads(sys.stdin.readline())
text = msg["message"]["content"][-1]["text"]
assert args[args.index("--tools") + 1] == ""
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "result", "is_error": False, "result": "echo:" + text + ":" + args[args.index("--model") + 1]}))
"""

FAKE_CODEX_LOGGED_OUT = """
import sys
sys.stdin.read()
sys.stderr.write("Error: not logged in. Run codex login.\\n")
sys.exit(1)
"""


def _install(bin_dir: Path, name: str, script: str) -> None:
    # A shell wrapper, since a shebang can't hold the interpreter path when it contains spaces.
    (bin_dir / f"{name}.py").write_text(script)
    path = bin_dir / name
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / name}.py" "$@"\n')
    path.chmod(0o755)


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if sys.platform == "win32":
        pytest.skip("fake CLIs are shebang scripts")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return bin_dir


def test_installed_clis_become_chat_models(fake_bin: Path) -> None:
    _install(fake_bin, "claude", FAKE_CLAUDE)
    models = cli_models.list_models()
    assert {m["id"] for m in models} == {f"cli:claude/{mid}" for mid, _ in cli_models.CLI_MODELS["claude"]}
    assert all(m["ready"] and m["label"].startswith("Claude Code") for m in models)
    assert llm.resolve_model("cli:claude/opus") == "cli:claude/opus"


def test_claude_cli_answers_through_the_router(fake_bin: Path) -> None:
    _install(fake_bin, "claude", FAKE_CLAUDE)
    result = asyncio.run(llm.call_agent_model(prompt="hi", system_prompt="be Glob", model_selector="cli:claude/sonnet", effort="high"))
    assert result == {"text": "echo:hi:sonnet", "model": "Claude Code · sonnet", "provider": "cli"}


def test_logged_out_cli_explains_how_to_log_in(fake_bin: Path) -> None:
    _install(fake_bin, "codex", FAKE_CODEX_LOGGED_OUT)
    with pytest.raises(RuntimeError, match="codex login"):
        asyncio.run(cli_models.call("cli:codex/", "hi", "be Glob", [], "medium"))


def test_missing_cli_names_the_install_command(fake_bin: Path) -> None:
    with pytest.raises(RuntimeError, match="npm install -g @google/gemini-cli"):
        asyncio.run(cli_models.call("cli:gemini/", "hi", "be Glob", [], "medium"))


def test_gemini_and_opencode_output_is_parsed(tmp_path: Path) -> None:
    assert cli_models.parse_output("gemini", json.dumps({"response": " done "}), tmp_path) == ("done", "")
    lines = "\n".join(json.dumps(e) for e in [{"type": "step_start"}, {"type": "text", "part": {"text": "a"}}, {"type": "text", "part": {"text": "b"}}])
    assert cli_models.parse_output("opencode", lines, tmp_path) == ("ab", "")


def test_claude_installs_without_npm() -> None:
    for windows in (True, False):
        script = cli_models.install_script("claude", windows)
        assert "npm" not in script and "claude.ai/install" in script


def test_npm_agents_install_node_first_when_npm_is_missing() -> None:
    assert "winget install" in cli_models.install_script("codex", True)
    assert cli_models.install_script("codex", True).rstrip().endswith("call npm install -g @openai/codex")
    assert "command -v npm" in cli_models.install_script("codex", False)


def test_logout_runs_the_cli_and_marks_it_logged_out(fake_bin: Path) -> None:
    _install(fake_bin, "claude", "#!/bin/sh\nexit 0\n")
    cli_models._login["claude"] = "ok"
    assert cli_models.logout("claude")["status"] == "login"
    assert cli_models.login_status("claude") == "login"


def test_gemini_logout_removes_its_login_files(fake_bin: Path, tmp_path: Path) -> None:
    _install(fake_bin, "gemini", "#!/bin/sh\nexit 0\n")
    creds = tmp_path / "home" / ".gemini" / "oauth_creds.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}")
    cli_models.logout("gemini")
    assert not creds.exists()
