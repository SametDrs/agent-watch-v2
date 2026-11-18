"""One-shot subprocess runner for external CLIs."""

from __future__ import annotations

import asyncio

from agent_watch import config


async def run_oneshot(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    """Run the agy binary once with args and return (returncode, stdout, stderr)."""
    binary = config.agy_bin()
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        return (-1, "", str(exc) or type(exc).__name__)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        await _terminate_process(proc)
        return (-1, "", "timeout")
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    except Exception as exc:
        await _terminate_process(proc)
        return (-1, "", str(exc) or type(exc).__name__)

    rc = proc.returncode if proc.returncode is not None else -1
    stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
    return (rc, stdout, stderr)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess if still active and wait for it to exit."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except Exception:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except BaseException:
        pass
