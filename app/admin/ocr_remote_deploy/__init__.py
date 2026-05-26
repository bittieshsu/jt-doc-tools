"""GPU OCR server remote deployment helpers.

`build_install_script()` and `build_uninstall_script()` generate self-contained
bash scripts that admins SCP to GPU hosts and run manually (path B).

Future v1.1 may add `ssh_deploy()` for path A (auto-SSH deploy via paramiko).
"""
from .builder import build_install_script, build_uninstall_script

__all__ = ["build_install_script", "build_uninstall_script"]
