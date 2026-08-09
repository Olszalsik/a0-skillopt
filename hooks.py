"""
SkillOpt plugin - lifecycle hooks.

Runs inside the Agent Zero framework runtime (NOT the agent execution
environment). The plugin installer calls install() after placing the
plugin in usr/plugins/. The updater calls pre_update() before pulling
new code. The uninstaller calls uninstall() before deleting the
plugin directory.

The A0 framework runs on its own venv at /opt/venv-a0 (Python 3.12.4)
in the Docker container. On Windows the path is .venv\\Scripts\\python.exe
inside the user's project directory. This hook MUST install into that
venv - not the shell's default Python (which may be a different venv
with no A0 packages).

v1.1.0 changes:
- Cross-platform A0 venv detection (Windows + Linux/Docker).
- Skillopt 0.2.0 ships `skillopt_sleep` as a separate top-level
  package alongside the `skillopt` library. Probe for it via the
  `skillopt_sleep` module name (NOT the merged `skillopt.sleep` form
  suggested in the original analysis - that form does not exist in
  0.2.0 and using it would break the installation probe).
"""

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

PLUGIN_NAME = "skillopt"
PLUGIN_VERSION = "1.1.0"

# Base `skillopt` (no extras). The [webui] extra pulls gradio +
# huggingface_hub >= 1.2 + msal + azure-identity, which conflicts with
# A0's pinned transformers (<1.0 huggingface_hub). We don't need any
# of that - A0 has its own WebUI.
REQUIRED_PACKAGE = "skillopt"


def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _a0_python() -> str:
    """Return the Python that should be used for `pip install`.

    Cross-platform detection:
    - Linux/Docker: /opt/venv-a0/bin/python (the framework venv)
    - Windows: .venv\\Scripts\\python.exe inside the current working
      directory (where the A0 dev setup expects it), with a fallback
      to sys.executable.

    Prefers the A0 venv if it exists (it's the framework's runtime).
    Falls back to `sys.executable` only if no candidate is found -
    in that case we just hope for the best and let pip target the
    current interpreter's site-packages.
    """
    candidates = []
    if sys.platform == "win32":
        # Windows: A0 dev setup puts the venv in the project root as
        # .venv\\Scripts\\python.exe. Also accept the legacy ProgramData
        # layout used by the bundled Docker image's Windows variant.
        cwd = os.getcwd()
        candidates.append(os.path.join(cwd, ".venv", "Scripts", "python.exe"))
        candidates.append(os.path.join(cwd, "venv", "Scripts", "python.exe"))
    else:
        # Linux/Docker: A0 framework venv at /opt/venv-a0.
        candidates.append("/opt/venv-a0/bin/python")
        # Allow override via env (e.g. CI / alt installs).
        env_py = os.environ.get("A0_VENV_PYTHON")
        if env_py:
            candidates.insert(0, env_py)

    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    log.warning(
        "[%s] no A0 venv found (tried %s) - falling back to %s",
        PLUGIN_NAME, candidates, sys.executable,
    )
    return sys.executable


def install() -> None:
    """Called after the plugin is placed in usr/plugins/.

    Ensures the `skillopt` Python package is available in the A0 venv.
    Base install only - no [webui], [alfworld], [claude], [qwen] extras;
    those break A0's dependency graph or pull hundreds of MB we don't
    need. Users can install extras manually if they need a specific
    benchmark or backend.
    """
    log.info("[%s] install() called (v%s)", PLUGIN_NAME, PLUGIN_VERSION)

    py = _a0_python()

    # Probe: is the skillopt_sleep engine importable from the target
    # Python? In v0.2.0+ this is a separate top-level package that ships
    # alongside the `skillopt` library.
    try:
        out = subprocess.check_output(
            [py, "-c", "import skillopt_sleep; print(skillopt_sleep.__file__)"],
            stderr=subprocess.STDOUT, text=True, timeout=15,
        )
        log.info(
            "[%s] skillopt_sleep already present in %s at %s",
            PLUGIN_NAME, py, out.strip(),
        )
        return
    except subprocess.CalledProcessError:
        pass # not installed, fall through to install
    except subprocess.TimeoutExpired:
        log.warning("[%s] import probe timed out - proceeding to install", PLUGIN_NAME)
    except Exception as e:
        log.warning("[%s] import probe error: %s - proceeding to install", PLUGIN_NAME, e)

    log.info("[%s] installing %s via %s ...", PLUGIN_NAME, REQUIRED_PACKAGE, py)
    try:
        subprocess.check_call(
            [py, "-m", "pip", "install", "--quiet", REQUIRED_PACKAGE],
        )
        log.info("[%s] pip install succeeded", PLUGIN_NAME)
    except subprocess.CalledProcessError as e:
        log.error("[%s] pip install failed: %s", PLUGIN_NAME, e)
        log.error(
            "[%s] you can install manually: %s -m pip install %s",
            PLUGIN_NAME, py, REQUIRED_PACKAGE,
        )


def pre_update() -> None:
    """Called immediately before the updater pulls new plugin code.

    We snapshot the staging directory (in case an in-flight Sleep cycle
    has a half-written proposal) and the run logs. Nothing else needs
    preservation - the plugin is stateless except for those two dirs.
    """
    log.info("[%s] pre_update() called - snapshotting staging/logs", PLUGIN_NAME)
    backup_root = os.path.join(_here(), "_update_backup")
    for sub in ("staging", "logs"):
        src = os.path.join(_here(), sub)
        if os.path.isdir(src):
            dst = os.path.join(backup_root, sub)
            try:
                shutil.copytree(src, dst, dirs_exist_ok=True)
            except Exception as e:
                log.warning("[%s] could not back up %s: %s", PLUGIN_NAME, sub, e)


def uninstall() -> None:
    """Called before the plugin directory is deleted.

    We do NOT pip-uninstall the `skillopt` package - other code in the
    container may depend on it. We DO remove the per-plugin staging
    area and (if the user enabled it) the agent skills that were
    adopted via this plugin. The latter is opt-in via env var so we
    never delete user data silently.
    """
    log.info("[%s] uninstall() called", PLUGIN_NAME)
    if os.environ.get("SKILLOPT_PURGE_ON_UNINSTALL", "").lower() in ("1", "true", "yes"):
        log.info("[%s] SKILLOPT_PURGE_ON_UNINSTALL=1, removing staging and logs", PLUGIN_NAME)
        for sub in ("staging", "logs"):
            p = os.path.join(_here(), sub)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
    else:
        log.info(
            "[%s] leaving staging/logs in place (set SKILLOPT_PURGE_ON_UNINSTALL=1 to remove)",
            PLUGIN_NAME,
        )


def _self_check() -> dict:
    """Optional helper used by execute.py to verify the plugin is healthy."""
    py = _a0_python()
    info = {
        "plugin_dir": _here(),
        "a0_python": py,
        "a0_venv_exists": os.path.isfile(py),
        "package_present": False,
        "package_version": None,
        "package_path": None,
        "staging_dir": os.path.isdir(os.path.join(_here(), "staging")),
        "logs_dir": os.path.isdir(os.path.join(_here(), "logs")),
    }
    try:
        out = subprocess.check_output(
            [py, "-c",
             "import skillopt_sleep; "
             "print('OK', getattr(skillopt_sleep, '__version__', 'unknown'), "
             "skillopt_sleep.__file__)"],
            stderr=subprocess.STDOUT, text=True, timeout=15,
        )
        parts = out.strip().split(None, 2)
        info["package_present"] = True
        info["package_version"] = parts[1] if len(parts) > 1 else "unknown"
        info["package_path"] = parts[2] if len(parts) > 2 else None
    except Exception as e:
        info["import_error"] = str(e)
    return info
