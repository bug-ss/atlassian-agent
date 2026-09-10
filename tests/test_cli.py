import pytest

from atlassian_agent.cli import build_parser, explain, resolve_system_prompt, settings_from_args
from atlassian_agent.config import DEFAULT_MCP_URL
from atlassian_agent.oauth import OAuthCallbackError


def parse(*argv):
    return build_parser().parse_args(list(argv))


def test_cli_flags_override_the_environment(monkeypatch):
    monkeypatch.setenv("ATLASSIAN_AGENT_MODEL", "anthropic:claude-sonnet-5")
    settings = settings_from_args(parse("--model", "anthropic:claude-opus-5"))
    assert settings.model == "anthropic:claude-opus-5"
    assert settings.mcp_url == DEFAULT_MCP_URL


def test_scopes_flag_accepts_commas():
    settings = settings_from_args(parse("--scopes", "read:me,offline_access"))
    assert settings.scopes == ("read:me", "offline_access")


def test_system_prompt_file_wins_over_inline(tmp_path):
    path = tmp_path / "prompt.txt"
    path.write_text("From the file.", encoding="utf-8")
    args = parse("--system-prompt", "inline", "--system-prompt-file", str(path))
    assert resolve_system_prompt(args) == "From the file."


def test_default_system_prompt_is_used_when_unset():
    from atlassian_agent.agent import DEFAULT_SYSTEM_PROMPT

    assert resolve_system_prompt(parse()) == DEFAULT_SYSTEM_PROMPT


def test_query_words_are_joined():
    assert parse("what", "is", "ENG-1").query == ["what", "is", "ENG-1"]


# -- error reporting --------------------------------------------------------


def test_explain_digs_the_real_cause_out_of_a_task_group():
    """anyio wraps MCP session failures in an ExceptionGroup whose own message
    ("unhandled errors in a TaskGroup") is useless to the user."""
    inner = OAuthCallbackError("Timed out after 8s waiting for the callback.")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])

    assert explain(group) == "Timed out after 8s waiting for the callback."


def test_explain_handles_nested_groups():
    inner = RuntimeError("ANTHROPIC_API_KEY is not set")
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    assert explain(nested) == "ANTHROPIC_API_KEY is not set"


def test_explain_falls_back_to_the_type_name():
    group = ExceptionGroup("boom", [ConnectionResetError()])
    assert explain(group).startswith("ConnectionResetError")


def test_explain_passes_through_a_plain_exception():
    assert explain(ValueError("bad port")) == "bad port"


@pytest.mark.parametrize("argv", [["--login"], ["--logout"], ["--status"], ["--list-tools"]])
def test_credential_actions_parse(argv):
    assert build_parser().parse_args(argv)
