# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_PATH = REPO_ROOT / "product" / "bundles" / "enterprise-default.json"


def _run_bundle(tmp_path: Path, *extra_args: str) -> dict:
    env = os.environ.copy()
    env["QWENPAW_WORKING_DIR"] = str(tmp_path / "home")
    env["QWENPAW_SECRET_DIR"] = str(tmp_path / "secret")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "apply_product_bundle.py"),
            str(BUNDLE_PATH),
            *extra_args,
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_enterprise_bundle_dry_run_reports_expected_boundaries(
    tmp_path: Path,
) -> None:
    payload = _run_bundle(tmp_path, "--dry-run")

    assert payload["dry_run"] is True
    assert payload["changed"] is True
    targets = {change["target"] for change in payload["changes"]}
    assert "product.skillhub" in targets
    assert "agents.enterprise" in targets
    assert "skills.pool.enterprise-search" in targets
    assert "agents.enterprise.skills.enterprise-search" in targets


def test_enterprise_bundle_apply_is_idempotent(tmp_path: Path) -> None:
    first = _run_bundle(tmp_path)
    second = _run_bundle(tmp_path)

    home = tmp_path / "home"
    assert first["changed"] is True
    assert second["changed"] is False
    assert (home / "product" / "settings.json").is_file()
    assert (home / "workspaces" / "enterprise" / "agent.json").is_file()
    assert (
        home
        / "workspaces"
        / "enterprise"
        / "skills"
        / "enterprise-search"
        / "SKILL.md"
    ).is_file()
