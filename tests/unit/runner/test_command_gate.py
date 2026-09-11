"""Three-tier command gate: the four SAFE conditions and the two fallbacks."""

from __future__ import annotations

from pathlib import Path

import pytest

from session_runner.security import CommandPolicy, CommandTier, classify_command


@pytest.fixture
def roots(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skills = tmp_path / "agent-skills" / "run-1"
    workspace.mkdir()
    skills.mkdir(parents=True)
    (workspace / "report.txt").write_text("data", encoding="utf-8")
    (skills / "review").mkdir()
    (skills / "review" / "SKILL.md").write_text("body", encoding="utf-8")
    return workspace, skills


def _classify(command: str, roots, policy: CommandPolicy = CommandPolicy()):
    workspace, skills = roots
    return classify_command(
        command,
        allowed_prefixes=[str(workspace), str(skills)],
        cwd=workspace,
        policy=policy,
    )


def test_reads_inside_allowed_prefixes_are_safe(roots):
    workspace, skills = roots
    assert _classify(f"cat {skills / 'review' / 'SKILL.md'}", roots).tier == CommandTier.SAFE
    assert _classify("cat report.txt", roots).tier == CommandTier.SAFE
    assert _classify("grep -n title report.txt", roots).tier == CommandTier.SAFE
    assert _classify("ls -la", roots).tier == CommandTier.SAFE


def test_path_outside_allowed_prefixes_is_risky(roots):
    verdict = _classify("cat /etc/passwd", roots)
    assert verdict.tier == CommandTier.RISKY
    assert "outside allowed prefixes" in verdict.reason


def test_relative_escape_is_risky(roots):
    assert _classify("cat ../../secrets.env", roots).tier == CommandTier.RISKY


def test_composition_is_never_safe(roots):
    for command in (
        "cat report.txt | head -5",
        "cat report.txt > copy.txt",
        "cat report.txt >> copy.txt",
        "cat $(pwd)/report.txt",
        "echo `whoami`",
        "cat report.txt && rm report.txt",
        "cat report.txt; ls",
    ):
        assert _classify(command, roots).tier == CommandTier.RISKY, command


def test_denied_executables_are_blocked(roots):
    for command in ("rm -rf /workspace", "sh -c 'cat x'", "python3 -c 'print(1)'", "sudo cat x"):
        assert _classify(command, roots).tier == CommandTier.BLOCKED, command


def test_untrusted_absolute_executable_is_risky(roots):
    verdict = _classify("/tmp/cat report.txt", roots)
    assert verdict.tier == CommandTier.RISKY
    assert "not trusted" in verdict.reason


def test_trusted_absolute_executable_is_safe(roots):
    assert _classify("/bin/cat report.txt", roots).tier == CommandTier.SAFE


def test_write_flag_demotes_to_risky(roots):
    assert _classify("sort -o out.txt report.txt", roots).tier == CommandTier.RISKY
    assert _classify("sort --output=out.txt report.txt", roots).tier == CommandTier.RISKY
    assert _classify("sort report.txt", roots).tier == CommandTier.SAFE


def test_unknown_executable_is_risky(roots):
    assert _classify("find . -name '*.md'", roots).tier == CommandTier.RISKY
    assert _classify("git status", roots).tier == CommandTier.RISKY


def test_empty_command_is_blocked(roots):
    assert _classify("   ", roots).tier == CommandTier.BLOCKED


def test_policy_can_only_tighten(roots):
    tightened = CommandPolicy().tightened(disable_safe=["cat"], deny_more=["grep"])
    assert _classify("cat report.txt", roots, tightened).tier == CommandTier.RISKY
    assert _classify("grep x report.txt", roots, tightened).tier == CommandTier.BLOCKED
