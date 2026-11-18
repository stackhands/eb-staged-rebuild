"""Guard removals with ``eb-keep-logs used`` when a usage log is provided."""

import subprocess

from eb_staged_rebuild.errors import Refused


def check_prefix(prefix, usage_log, eb_keep_logs):
    """Return whether ``usage_log`` reports hits under ``prefix``.

    ``eb-keep-logs used --require-none`` exits 0 when no matching hit was found,
    and 2 when one or more paths under the prefix were seen.
    """
    if not usage_log:
        return False, ""
    cmd = [eb_keep_logs, "used", "--prefix", prefix, "--require-none", usage_log]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise Refused(f"cannot run {eb_keep_logs} used: {exc}") from exc
    if completed.returncode == 0:
        return False, ""
    detail = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode == 2:
        return True, detail
    tail = detail[-500:] if detail else ""
    raise Refused(f"{eb_keep_logs} used exited {completed.returncode}: {tail}")
