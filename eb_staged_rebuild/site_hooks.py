"""An EasyBuild hook for a site prefix: stay inside it, and add a footer or an rpath.

EasyBuild runs this file only when EASYBUILD_HOOKS names it. Each check stays
off until its variable is set, so pointing the hook at this file changes nothing
by itself.
"""

from __future__ import annotations

import os


def outside_prefix(prefix: str, installdir: str) -> bool:
    """True when installdir is not prefix and not inside it."""
    if not prefix or not installdir:
        return False
    prefix = os.path.normpath(prefix)
    installdir = os.path.normpath(installdir)
    if prefix == installdir:
        return False
    try:
        return os.path.commonpath([prefix, installdir]) != prefix
    except ValueError:
        return True


def pre_install_hook(self, *args, environ=None, **kwargs):
    """Refuse an install outside EB_LOCAL_PREFIX, else EESSI_SITE_SOFTWARE_PATH.

    With neither set, this returns and the install continues.
    """
    env = os.environ if environ is None else environ
    prefix = env.get("EB_LOCAL_PREFIX") or env.get("EESSI_SITE_SOFTWARE_PATH") or ""
    installdir = getattr(self, "installdir", "") or ""
    if not prefix or not outside_prefix(prefix, installdir):
        return None
    raise RuntimeError(f"eb-staged-rebuild: install path {installdir} is outside {prefix}")


def footer_text(current: str, extra: str) -> str:
    """current plus extra on its own line, once. An empty extra leaves current."""
    extra = (extra or "").strip("\n")
    current = current or ""
    if not extra or extra in current.splitlines():
        return current
    if current and not current.endswith("\n"):
        current += "\n"
    return current + extra + "\n"


def pre_module_hook(self, *args, environ=None, **kwargs):
    """Append EB_LOCAL_MODULE_FOOTER to the module footer. Unset, this changes nothing."""
    env = os.environ if environ is None else environ
    extra = env.get("EB_LOCAL_MODULE_FOOTER", "")
    if not extra:
        return None
    cfg = getattr(self, "cfg", None)
    if isinstance(cfg, dict):
        cfg["module_footer"] = footer_text(cfg.get("module_footer") or "", extra)
    return None


def enable_rpath(options: dict, enabled: bool):
    """Set options['rpath'] when enabled. Otherwise leave options alone."""
    if not enabled:
        return None
    options["rpath"] = True
    return options


def pre_build_hook(self, *args, environ=None, **kwargs):
    """Turn toolchain rpath on when EB_LOCAL_RPATH is 1, true or on."""
    env = os.environ if environ is None else environ
    if env.get("EB_LOCAL_RPATH") not in ("1", "true", "on"):
        return None
    toolchain = getattr(self, "toolchain", None)
    options = getattr(toolchain, "options", None)
    if isinstance(options, dict):
        enable_rpath(options, True)
    return None
