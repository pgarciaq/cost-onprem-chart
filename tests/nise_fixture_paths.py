"""Resolve nise fixture template paths.

Tries the nise package first (importlib.resources), falls back to local paths
for backward compatibility during the transition period.
"""

import os
from pathlib import Path


def get_e2e_templates_dir() -> Path:
    """Return path to ROS OCP E2E test templates."""
    try:
        from importlib.resources import files

        pkg_dir = files("nise") / "examples" / "ros_ocp_e2e"
        resolved = Path(str(pkg_dir))
        if resolved.is_dir():
            return resolved
    except (ImportError, ModuleNotFoundError, TypeError):
        pass
    return Path(__file__).resolve().parent / "data" / "nise_templates"


def get_seeding_templates_dir() -> Path:
    """Return path to ROS OCP data seeding templates."""
    try:
        from importlib.resources import files

        pkg_dir = files("nise") / "examples" / "ros_ocp_seeding"
        resolved = Path(str(pkg_dir))
        if resolved.is_dir():
            return resolved
    except (ImportError, ModuleNotFoundError, TypeError):
        pass
    return Path(__file__).resolve().parent / "fixtures" / "nise_templates"
