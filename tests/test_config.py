"""Config loading.

``review_agent`` and ``review_agent_args`` and ``review_agent_env`` decide what process gets
spawned, so both the parsing and its effect on the agent are covered: a key that parses
correctly but never reaches the agent would look fine and break every review.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review.config import Config, ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    (tmp_path / ".codereview.toml").write_text(text, encoding="utf-8")
    return tmp_path


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert config.agent is None
    assert config.review_guidelines == ""
    assert config.max_diff_bytes > 0
    assert config.deny_unknown_commands is True


def test_full_config_is_parsed(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            'review_agent = ["ac", "--flag"]\n'
            'review_agent_env = { A = "1" }\n'
            'review_guidelines = "Be strict."\n'
            "max_diff_bytes = 1234\n"
            "review_timeout_seconds = 42\n"
            "deny_unknown_commands = false\n",
        )
    )
    assert config.agent is not None
    assert config.agent.command == "ac"
    assert config.agent.args == ("--flag",)
    assert config.agent.env == {"A": "1"}
    assert config.review_guidelines == "Be strict."
    assert config.max_diff_bytes == 1234
    assert config.review_timeout_seconds == 42
    assert config.deny_unknown_commands is False


def test_review_agent_args_are_appended_after_the_command(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            """
review_agent = ["ac", "--base"]
review_agent_args = ["--extra", "value"]
""",
        )
    )
    assert config.agent is not None
    assert config.agent.argv == ["ac", "--base", "--extra", "value"]


def test_env_without_args_is_kept(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, 'review_agent = ["ac"]\nreview_agent_env = { A = "1" }\n'))
    assert config.agent is not None
    assert config.agent.args == ()
    assert config.agent.env == {"A": "1"}


def test_args_without_env_are_kept(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, 'review_agent = ["ac"]\nreview_agent_args = ["--x"]\n'))
    assert config.agent is not None
    assert config.agent.args == ("--x",)
    assert config.agent.env == {}


def test_env_and_args_without_a_command_are_ignored(tmp_path: Path) -> None:
    """Config that names no agent is not an agent; require_agent() reports it."""
    config = load_config(write(tmp_path, 'review_agent_env = { A = "1" }\n'))
    assert config.agent is None


def test_empty_args_list_is_not_an_error(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, 'review_agent = ["ac"]\nreview_agent_args = []\n'))
    assert config.agent is not None
    assert config.agent.args == ()


def test_agent_argv_property_is_complete(tmp_path: Path) -> None:
    config = load_config(
        write(tmp_path, 'review_agent = ["ac", "-a"]\nreview_agent_args = ["-b"]\n')
    )
    assert config.require_agent().argv == ["ac", "-a", "-b"]


def test_label_is_the_command(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, 'review_agent = ["my-agent", "-x"]\n'))
    assert config.require_agent().label == "my-agent"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("review_agent = []\n", "non-empty array"),
        ('review_agent = "ac"\n', "non-empty array"),
        ("review_agent = [1]\n", "must all be strings"),
        ('review_agent = ["ac"]\nreview_agent_args = "x"\n', "array of strings"),
        ('review_agent = ["ac"]\nreview_agent_args = [1]\n', "array of strings"),
        ('review_agent = ["ac"]\nreview_agent_env = "x"\n', "table of string"),
        ('review_agent = ["ac"]\nreview_agent_env = { A = 1 }\n', "must be a string"),
        ('review_agent = ["ac"]\nmax_diff_bytes = 0\n', "positive integer"),
        ('review_agent = ["ac"]\nmax_diff_bytes = "big"\n', "positive integer"),
        ('review_agent = ["ac"]\nreview_timeout_seconds = -1\n', "positive integer"),
        ('review_agent = ["ac"]\nreview_timeout_seconds = true\n', "positive integer"),
        ('review_agent = ["ac"]\ndeny_unknown_commands = "yes"\n', "true or false"),
        ("review_guidelines = 5\n", "must be a string"),
        ("this is not toml =\n", "not valid TOML"),
    ],
)
def test_malformed_config_is_rejected_with_a_useful_message(
    tmp_path: Path, text: str, expected: str
) -> None:
    with pytest.raises(ConfigError, match=expected):
        load_config(write(tmp_path, text))


def test_require_agent_names_the_config_file() -> None:
    config = Config(agent=None, source_path=Path("/tmp/x/.codereview.toml"))
    with pytest.raises(ConfigError, match=r"/tmp/x/\.codereview\.toml"):
        config.require_agent()


def test_require_agent_error_shows_an_example() -> None:
    with pytest.raises(ConfigError, match=r'review_agent = \["claude-agent-acp"\]'):
        Config(agent=None).require_agent()


def test_guidelines_are_stripped(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, 'review_guidelines = "\\n\\n  Be strict.  \\n\\n"\n'))
    assert config.review_guidelines == "Be strict."


def test_multiline_guidelines_from_a_real_toml_block(tmp_path: Path) -> None:
    """The documented triple-quoted TOML form must survive intact."""
    config = load_config(
        write(
            tmp_path,
            'review_guidelines = """\nFocus on error handling.\nAnd backwards compatibility.\n"""\n',
        )
    )
    assert "error handling" in config.review_guidelines
    assert "backwards compatibility" in config.review_guidelines
    assert not config.review_guidelines.startswith("\n")
