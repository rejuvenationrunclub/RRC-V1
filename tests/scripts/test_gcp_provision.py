from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.scripts.stubs import ROOT, install_stubs, stub_env

SCRIPT = ROOT / "scripts" / "gcp-provision.sh"


@dataclass
class Run:
    code: int
    out: str
    err: str
    calls: list[str]


@pytest.fixture
def provision(tmp_path: Path):  # type: ignore[no-untyped-def]
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
            timeout=60,
            check=False,
        )
        return Run(proc.returncode, proc.stdout, proc.stderr, log.read_text().splitlines())

    return run


def _call(calls: list[str], fragment: str) -> str:
    matches = [c for c in calls if fragment in c]
    assert matches, f"no gcloud call containing {fragment!r}; calls were:\n" + "\n".join(calls)
    return matches[0]


def test_fresh_project_creates_free_tier_resources(provision) -> None:  # type: ignore[no-untyped-def]
    r = provision("rrc-bot-123")
    assert r.code == 0, r.err

    vm = _call(r.calls, "compute instances create rrc-bot")
    for flag in (
        "--zone=us-west1-b",
        "--machine-type=e2-micro",
        "--boot-disk-type=pd-standard",
        "--boot-disk-size=30GB",
        "--image-family=debian-12",
        "--service-account=rrc-bot-vm@rrc-bot-123.iam.gserviceaccount.com",
        "--scopes=cloud-platform",
    ):
        assert flag in vm

    bucket = _call(r.calls, "storage buckets create gs://rrc-backups-rrc-bot-123")
    assert "--location=us-west1" in bucket
    assert "--public-access-prevention" in bucket
    assert "--lifecycle-file=deploy/gcs-lifecycle.json" in _call(r.calls, "buckets update")

    secret = _call(r.calls, "secrets create discord-token")
    assert "--locations=us-west1" in secret

    roles = {c.split("--role=")[1].split()[0] for c in r.calls if "--role=" in c}
    assert roles == {
        "roles/logging.logWriter",
        "roles/monitoring.metricWriter",
        "roles/storage.objectCreator",
        "roles/storage.objectViewer",
        "roles/secretmanager.secretAccessor",
    }
    # Least privilege: never broad roles, never deletion rights on backups.
    assert not any(
        bad in " ".join(r.calls) for bad in ("roles/editor", "roles/owner", "objectAdmin")
    )
    assert all("--project=rrc-bot-123" in c for c in r.calls)
    assert "NEW VERSION" in r.out  # tells the human to add the token


def test_rerun_is_idempotent(provision) -> None:  # type: ignore[no-untyped-def]
    r = provision("rrc-bot-123", STUB_EXISTING="sa,bucket,secret,vm", STUB_HAS_TOKEN="1")
    assert r.code == 0, r.err
    assert not [c for c in r.calls if " create " in c]
    assert "VM already exists" in r.out
    assert "value present: yes" in r.out
    assert "NEW VERSION" not in r.out


@pytest.mark.parametrize("zone", ["europe-west1-b", "us-east4-a", "us-west2-a", "asia-east1-a"])
def test_refuses_billed_regions_before_touching_gcloud(provision, zone: str) -> None:  # type: ignore[no-untyped-def]
    r = provision("rrc-bot-123", zone)
    assert r.code != 0
    assert "free-tier regions" in r.err
    assert r.calls == []


@pytest.mark.parametrize("zone", ["us-central1-a", "us-east1-c"])
def test_accepts_other_free_regions(provision, zone: str) -> None:  # type: ignore[no-untyped-def]
    r = provision("rrc-bot-123", zone)
    assert r.code == 0, r.err
    assert f"--zone={zone}" in _call(r.calls, "compute instances create")
    assert f"--location={zone.rsplit('-', 1)[0]}" in _call(r.calls, "storage buckets create")


def test_stops_when_billing_not_linked(provision) -> None:  # type: ignore[no-untyped-def]
    r = provision("rrc-bot-123", STUB_BILLING="False")
    assert r.code != 0
    assert "billing" in r.err
    assert not [c for c in r.calls if "services enable" in c or " create " in c]


def test_usage_errors(provision) -> None:  # type: ignore[no-untyped-def]
    assert provision().code == 2
    assert provision("a", "us-west1-b", "extra").code == 2


@pytest.mark.parametrize("project", ["x", "Rrc-Bot", "rrc;reboot", "rrc-bot-123-"])
def test_rejects_invalid_project_ids(provision, project: str) -> None:  # type: ignore[no-untyped-def]
    r = provision(project)
    assert r.code != 0
    assert "project ID" in r.err
    assert r.calls == []
