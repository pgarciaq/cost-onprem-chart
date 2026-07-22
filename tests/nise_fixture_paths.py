"""Resolve nise fixture template paths.

Tries the nise package first (importlib.resources), falls back to local paths
for backward compatibility during the transition period.
"""

import os
from pathlib import Path


def _resolve_nise_examples_subdir(subdir: str) -> "Path | None":
    """Resolve a subdirectory under nise/examples/, handling editable installs."""
    try:
        from importlib.resources import files

        pkg_root = Path(str(files("nise")))
        candidate = pkg_root / "examples" / subdir
        if candidate.is_dir():
            return candidate
        # Editable install: examples/ lives at the repo root, one level above the package
        repo_root = pkg_root.parent / "examples" / subdir
        if repo_root.is_dir():
            return repo_root
    except (ImportError, ModuleNotFoundError, TypeError):
        pass
    return None


def get_e2e_templates_dir() -> Path:
    """Return path to ROS OCP E2E test templates."""
    resolved = _resolve_nise_examples_subdir("ros_ocp_e2e")
    if resolved:
        return resolved
    return Path(__file__).resolve().parent / "data" / "nise_templates"


def get_seeding_templates_dir() -> Path:
    """Return path to ROS OCP data seeding templates."""
    resolved = _resolve_nise_examples_subdir("ros_ocp_seeding")
    if resolved:
        return resolved
    return Path(__file__).resolve().parent / "fixtures" / "nise_templates"
