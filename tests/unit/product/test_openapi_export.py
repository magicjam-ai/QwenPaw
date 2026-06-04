# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_export_openapi_contains_frontend_contract_paths(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env["QWENPAW_WORKING_DIR"] = str(tmp_path / "home")
    env["QWENPAW_SECRET_DIR"] = str(tmp_path / "secret")
    output_path = tmp_path / "openapi.json"

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "export_openapi.py"),
            str(output_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )

    spec = json.loads(output_path.read_text(encoding="utf-8"))
    paths = spec["paths"]
    for path in [
        "/api/market/providers",
        "/api/market/search",
        "/api/models",
        "/api/models/active",
        "/api/agents",
        "/api/skills",
        "/api/console/chat",
        "/api/workspace/download",
    ]:
        assert path in paths
