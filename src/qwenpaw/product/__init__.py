# -*- coding: utf-8 -*-
"""Product delivery helpers for downstream QwenPaw distributions."""

from .bundle import (
    BundleApplyResult,
    BundleChange,
    ProductBundle,
    apply_product_bundle,
    load_product_bundle,
)
from .settings import get_product_settings, save_product_settings

__all__ = [
    "BundleApplyResult",
    "BundleChange",
    "ProductBundle",
    "apply_product_bundle",
    "get_product_settings",
    "load_product_bundle",
    "save_product_settings",
]
