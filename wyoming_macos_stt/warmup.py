"""Pre-load yap speech assets so the first real request does not fail.

yap downloads the speech assets for a locale on first use and aborts the
request that triggered the download with a CancellationError. The download
phase is flaky for roughly a minute: attempts can succeed and then fail
again. Running the locale to two consecutive successes before the server
accepts connections keeps that instability out of real voice commands.
"""

import asyncio
import logging
import os
import shlex
import tempfile
import wave
from typing import List, Optional

_LOGGER = logging.getLogger("wyoming-macos-stt")

_ATTEMPTS = 8
_DELAY = 10  # seconds between attempts
_REQUIRED_SUCCESSES = 2
_TIMEOUT = 60  # seconds per attempt


def _write_silence(path: str, seconds: int = 1, rate: int = 16000) -> None:
    """Write a short silent WAV file to use as warm-up input."""
    with wave.open(path, "wb") as wav_file:
        wav_file.setframerate(rate)
        wav_file.setsampwidth(2)
        wav_file.setnchannels(1)
        wav_file.writeframes(b"\x00\x00" * (rate * seconds))


async def _run(cmd: List[str]) -> bool:
    """Run a yap command, returning True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    except OSError as err:
        _LOGGER.error("Failed to run %s: %s", cmd[0], err)
        return False

    if proc.returncode != 0:
        _LOGGER.debug("Warm-up attempt failed: %s", stderr.decode().strip())

    return proc.returncode == 0


async def warmup(languages: List[str], yap_args: Optional[str] = None) -> None:
    """Download speech assets for the given languages before serving."""
    if not languages:
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        wav_path = os.path.join(temp_dir, "warmup.wav")
        _write_silence(wav_path)

        for language in languages:
            base = ["yap"]
            if yap_args:
                base.extend(shlex.split(yap_args))
            base.extend(["-l", language, wav_path])

            successes = 0
            for attempt in range(1, _ATTEMPTS + 1):
                if await _run(base):
                    successes += 1
                    if successes >= _REQUIRED_SUCCESSES:
                        _LOGGER.info("Warmed up language: %s", language)
                        break
                    continue
                else:
                    successes = 0
                    _LOGGER.info(
                        "Downloading speech assets for %s (attempt %d/%d)",
                        language,
                        attempt,
                        _ATTEMPTS,
                    )

                # Only wait after a failure; assets may still be downloading.
                if attempt < _ATTEMPTS:
                    await asyncio.sleep(_DELAY)
            else:
                _LOGGER.warning(
                    "Could not warm up %s; the first requests may fail", language
                )
