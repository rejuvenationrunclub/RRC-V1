from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.scripts.stubs import ROOT, install_stubs, stub_env

SCRIPT = ROOT / "scripts" / "gcp-vm.sh"
GUILD = "123456789012345678"


@dataclass
class Run:
    code: int
    out: str
    err: str
    calls: list[str]


@pytest.fixture
def vm(tmp_path: Path):  # type: ignore[no-untyped-def]
    bin_dir = tmp_path / "bin"
    install_stubs(bin_dir)

    def run(*args: str, **stub: str) -> Run:
        log = tmp_path / "calls.log"
        log.write_text("")
        proc = subprocess.run(  # noqa: S603
            ["bash", str(SCRIPT), *args],
            env=stub_env(bin_dir, log, tmp_path / "state", **stub),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return Run(proc.returncode, proc.stdout, proc.stderr, log.read_text().splitlines())

    return run


def _ssh(r: Run) -> str:
    calls = [c for c in r.calls if "compute ssh" in c]
    assert len(calls) == 1, r.calls
    return calls[0]


def test_deploy_syncs_code_then_runs_vm_deploy(vm) -> None:  # type: ignore[no-untyped-def]
    r = vm("rrc-bot-123", "deploy", "--guild-id", GUILD, "--branch", "claude/feature-x")
    assert r.code == 0, r.err
    call = _ssh(r)
    assert "compute ssh rrc-bot --zone=us-west1-b --project=rrc-bot-123 --quiet" in call
    assert "git clone -q https://github.com/rejuvenationrunclub/RRC-V1.git /opt/rrc-bot" in call
    assert "checkout -q claude/feature-x" in call
    assert "pull -q --ff-only origin claude/feature-x" in call
    tail = call.split("&&")[-1].strip()
    assert tail == (
        "sudo /opt/rrc-bot/scripts/vm-deploy.sh "
        f"--guild-id {GUILD} --bucket gs://rrc-backups-rrc-bot-123"
    )


def test_update_defaults_to_main_and_keeps_guild(vm) -> None:  # type: ignore[no-untyped-def]
    r = vm("rrc-bot-123", "update")
    assert r.code == 0, r.err
    call = _ssh(r)
    assert "checkout -q main" in call
    assert call.rstrip().endswith("sudo /opt/rrc-bot/scripts/vm-deploy.sh")
    assert "--guild-id" not in call


def test_zone_override(vm) -> None:  # type: ignore[no-untyped-def]
    r = vm("rrc-bot-123", "status", ZONE="us-central1-a")
    assert r.code == 0, r.err
    assert "--zone=us-central1-a" in _ssh(r)


def test_backup_now_lists_bucket(vm) -> None:  # type: ignore[no-untyped-def]
    r = vm("rrc-bot-123", "backup-now")
    assert r.code == 0, r.err
    assert "systemctl start rrc-backup.service" in _ssh(r)
    assert any("storage ls" in c and "gs://rrc-backups-rrc-bot-123/" in c for c in r.calls)


@pytest.mark.parametrize(
    "args",
    [
        ("rrc-bot-123", "deploy"),
        ("rrc-bot-123", "deploy", "--guild-id", "my server"),
        ("rrc-bot-123", "deploy", "--guild-id", "123; rm -rf /"),
        ("rrc-bot-123", "deploy", "--guild-id", GUILD, "--branch", "main; reboot"),
        ("rrc-bot-123", "deploy", "--guild-id", GUILD, "--branch", "--upload-pack=x"),
        ("rrc-bot-123", "update", "--guild-id", GUILD),
        ("rrc-bot-123", "explode"),
        ("rrc-bot-123",),
    ],
)
def test_rejects_bad_input_without_touching_the_vm(vm, args: tuple[str, ...]) -> None:  # type: ignore[no-untyped-def]
    r = vm(*args)
    assert r.code != 0
    assert not [c for c in r.calls if "compute ssh" in c]


@pytest.mark.parametrize("project", ["x", "Rrc-Bot", "rrc;reboot", "rrc-bot-123-", "1rrc-bot"])
def test_rejects_invalid_project_ids(vm, project: str) -> None:  # type: ignore[no-untyped-def]
    r = vm(project, "status")
    assert r.code != 0
    assert "project ID" in r.err
    assert r.calls == []
