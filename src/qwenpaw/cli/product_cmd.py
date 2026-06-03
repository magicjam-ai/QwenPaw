# -*- coding: utf-8 -*-
"""Product delivery CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ..product import apply_product_bundle, load_product_bundle


@click.group("product")
def product_group() -> None:
    """Manage downstream product bundles."""


@product_group.command("apply-bundle")
@click.argument(
    "bundle_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate and print planned changes without writing files.",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Print machine-readable JSON output.",
)
def apply_bundle_cmd(
    bundle_path: Path,
    dry_run: bool,
    json_output: bool,
) -> None:
    """Apply an idempotent product bundle manifest."""
    try:
        bundle = load_product_bundle(bundle_path)
        result = apply_product_bundle(bundle, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    payload = result.to_dict()
    if json_output:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    mode = "DRY RUN" if result.dry_run else "APPLIED"
    click.echo(
        f"{mode}: {result.bundle_id}@{result.version} "
        f"({len(result.changes)} change(s))",
    )
    for change in result.changes:
        suffix = f" — {change.detail}" if change.detail else ""
        click.echo(f"  - {change.action}: {change.target}{suffix}")
    if result.state_path:
        click.echo(f"State: {result.state_path}")
