"""Hook generation, installation, and the never-fail entry point."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from code_review import git, hook, paths


def read_hook(repo: Path) -> str:
    return hook.hook_path(repo).read_text(encoding="utf-8")


# -- installation ----------------------------------------------------------


def test_init_installs_the_hook(repo: Path) -> None:
    path = hook.install(repo)
    assert path.exists()
    body = read_hook(repo)
    assert hook.MARKER_BEGIN in body
    assert hook.MARKER_END in body


def test_installed_hook_is_executable(repo: Path) -> None:
    path = hook.install(repo)
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR
    assert mode & stat.S_IXGRP
    assert mode & stat.S_IXOTH


def test_hook_uses_absolute_paths(repo: Path) -> None:
    """git runs hooks with a minimal environment, so a bare name may not resolve."""
    hook.install(repo)
    body = read_hook(repo)
    assert str(Path(os.sys.executable).absolute()) in body
    assert str(paths.hook_log_path()) in body


def test_hook_interpreter_can_import_code_review(repo: Path) -> None:
    """The baked-in interpreter must actually be able to run the CLI.

    In a virtualenv, ``Path(sys.executable).resolve()`` follows the symlink to the base
    interpreter, which has no code_review installed. That mistake makes every commit log
    "No module named code_review", so the hook is checked against the real interpreter.
    """
    hook.install(repo)
    body = read_hook(repo)
    executable = next(
        part.strip('"')
        for line in body.splitlines()
        for part in line.split('"')
        if part.strip('"').endswith(("python", "python3", "python3.14"))
    )
    result = subprocess.run(
        [executable, "-c", "import code_review"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_hook_script_is_valid_shell(repo: Path) -> None:
    hook.install(repo)
    result = subprocess.run(
        ["sh", "-n", str(hook.hook_path(repo))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_install_is_idempotent(repo: Path) -> None:
    hook.install(repo)
    first = read_hook(repo)
    hook.install(repo)
    assert read_hook(repo) == first


def test_reinstall_refreshes_stale_paths(repo: Path, isolated_home: Path) -> None:
    """Re-running must rewrite a stale block, not return early.

    The block embeds absolute paths that can go stale: a changed CODEREVIEW_HOME, a moved
    interpreter, or a hook written by an older version. Early-returning would leave every
    commit logging an error forever.
    """
    hook.install(repo)
    path = hook.hook_path(repo)
    path.write_text(
        path.read_text(encoding="utf-8").replace(str(paths.hook_log_path()), "/old/path.log"),
        encoding="utf-8",
    )
    assert "/old/path.log" in read_hook(repo)

    hook.install(repo)
    body = read_hook(repo)
    assert "/old/path.log" not in body
    assert str(paths.hook_log_path()) in body


def test_reinstall_preserves_foreign_content_around_the_block(repo: Path) -> None:
    path = hook.hook_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho before\n", encoding="utf-8")
    hook.install(repo, force=True)
    hook.install(repo)  # refresh path, not force

    body = read_hook(repo)
    assert "echo before" in body
    assert body.count(hook.MARKER_BEGIN) == 1


def test_install_refuses_to_clobber_a_foreign_hook(repo: Path) -> None:
    path = hook.hook_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    with pytest.raises(hook.HookError, match="already exists"):
        hook.install(repo)
    assert "echo mine" in read_hook(repo)


def test_force_wraps_an_existing_hook(repo: Path) -> None:
    path = hook.hook_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    hook.install(repo, force=True)
    body = read_hook(repo)
    assert "echo mine" in body
    assert hook.MARKER_BEGIN in body


def test_force_creates_the_hooks_directory_when_absent(repo: Path) -> None:
    path = hook.install(repo, force=True)
    assert path.exists()


# -- uninstall -------------------------------------------------------------


def test_uninstall_removes_the_file_when_ours_alone(repo: Path) -> None:
    hook.install(repo)
    assert hook.uninstall(repo) is True
    assert not hook.hook_path(repo).exists()


def test_uninstall_preserves_foreign_content(repo: Path) -> None:
    path = hook.hook_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    hook.install(repo, force=True)

    assert hook.uninstall(repo) is True
    body = read_hook(repo)
    assert "echo mine" in body
    assert hook.MARKER_BEGIN not in body


def test_uninstall_reports_nothing_to_do(repo: Path) -> None:
    assert hook.uninstall(repo) is False


def test_uninstall_does_not_remove_a_foreign_hook(repo: Path) -> None:
    path = hook.hook_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    assert hook.uninstall(repo) is False
    assert path.exists()


def test_reinstall_after_uninstall_works(repo: Path) -> None:
    hook.install(repo)
    hook.uninstall(repo)
    hook.install(repo)
    assert hook.is_installed(repo)


# -- status ----------------------------------------------------------------


def test_is_installed_reflects_state(repo: Path) -> None:
    assert not hook.is_installed(repo)
    hook.install(repo)
    assert hook.is_installed(repo)
    hook.uninstall(repo)
    assert not hook.is_installed(repo)


# -- gitignore -------------------------------------------------------------


def test_ensure_gitignore_adds_the_state_dir(repo: Path) -> None:
    assert hook.ensure_gitignore(repo) is True
    assert ".codereview/" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_ensure_gitignore_is_idempotent(repo: Path) -> None:
    hook.ensure_gitignore(repo)
    first = (repo / ".gitignore").read_text(encoding="utf-8")
    assert hook.ensure_gitignore(repo) is False
    assert (repo / ".gitignore").read_text(encoding="utf-8") == first


def test_gitignore_is_appended_to_existing_content(repo: Path) -> None:
    gitignore = repo / ".gitignore"
    gitignore.write_text("*.pyc\n", encoding="utf-8")
    hook.ensure_gitignore(repo)
    body = gitignore.read_text(encoding="utf-8")
    assert "*.pyc" in body
    assert ".codereview/" in body


def test_gitignore_respects_a_missing_trailing_newline(repo: Path) -> None:
    gitignore = repo / ".gitignore"
    gitignore.write_text("*.pyc", encoding="utf-8")
    hook.ensure_gitignore(repo)
    lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.pyc" in lines
    assert ".codereview/" in lines


def test_state_dir_is_actually_ignored_by_git(repo: Path) -> None:
    hook.ensure_gitignore(repo)
    paths.state_dir(repo).mkdir(parents=True, exist_ok=True)
    (paths.state_dir(repo) / "reviews.db").write_text("x", encoding="utf-8")
    ignored = subprocess.run(
        ["git", "check-ignore", ".codereview/reviews.db"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0, ignored.stdout


# -- the hook entry point --------------------------------------------------


def test_main_returns_zero_even_with_no_daemon(
    repo: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a broken or absent daemon must never fail a commit.

    The enqueue is stubbed out so this stays a unit test. A real daemon is started by the
    end-to-end test instead, which is the only place that should spawn processes.
    """
    monkeypatch.chdir(repo)
    monkeypatch.setattr("code_review.daemon.client.ensure_and_enqueue", lambda *a, **k: {})
    assert hook.main([]) == 0


def test_main_survives_an_unreachable_daemon(
    repo: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No daemon and no way to start one: still exit 0."""
    monkeypatch.chdir(repo)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise OSError("could not connect to socket")

    monkeypatch.setattr("code_review.daemon.client.ensure_and_enqueue", unavailable)
    assert hook.main([]) == 0


def test_main_returns_zero_in_an_empty_repo(tmp_path: Path, isolated_home: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=empty, check=True)
    cwd = Path.cwd()
    os.chdir(empty)
    try:
        assert hook.main([]) == 0
    finally:
        os.chdir(cwd)


def test_main_returns_zero_outside_a_repository(tmp_path: Path, isolated_home: Path) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    cwd = Path.cwd()
    os.chdir(outside)
    try:
        assert hook.main([]) == 0
    finally:
        os.chdir(cwd)


def test_main_logs_failures_without_raising(
    repo: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated daemon failure")

    monkeypatch.chdir(repo)
    monkeypatch.setattr("code_review.daemon.client.ensure_and_enqueue", boom)
    assert hook.main([]) == 0
    assert paths.hook_log_path().exists()
    assert "simulated daemon failure" in paths.hook_log_path().read_text(encoding="utf-8")


def test_main_accepts_an_explicit_sha(
    repo: Path,
    isolated_home: Path,
    second_commit: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr("code_review.daemon.client.ensure_and_enqueue", lambda *a, **k: {})
    assert hook.main([second_commit]) == 0


def test_main_resolves_head_from_cwd_not_the_invocation_dir(
    repo: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git runs hooks from the worktree root, so cwd is the only trustworthy signal."""
    monkeypatch.chdir(repo)
    captured: list[str] = []

    def record(sha: str, *args: object, **kwargs: object) -> None:
        captured.append(sha)

    monkeypatch.setattr("code_review.daemon.client.ensure_and_enqueue", record)
    hook.main([])
    assert captured == [git.resolve_sha(repo, "HEAD")]
