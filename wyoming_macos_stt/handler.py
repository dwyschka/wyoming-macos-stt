"""Event handler for clients of the server."""

import argparse
import asyncio
import logging
import os
import shlex
import tempfile
import time
import uuid
import wave
from typing import List, Optional, Tuple

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStop
from wyoming.event import Event
from wyoming.info import Describe
from wyoming.server import AsyncEventHandler

from .info import get_wyoming_info

_LOGGER = logging.getLogger("wyoming-macos-stt")

_SUBPROCESS_TIMEOUT = 30  # seconds


class MacosSTTEventHandler(AsyncEventHandler):
    """Event handler for clients."""

    _shared_wav_dir: Optional[tempfile.TemporaryDirectory] = None

    def __init__(
        self,
        cli_args: argparse.Namespace,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.cli_args = cli_args
        self.wyoming_info_event = get_wyoming_info(self.cli_args.service_name).event()

        if MacosSTTEventHandler._shared_wav_dir is None:
            MacosSTTEventHandler._shared_wav_dir = tempfile.TemporaryDirectory()

        self._wav_path = os.path.join(
            MacosSTTEventHandler._shared_wav_dir.name, f"speech_{uuid.uuid4().hex}.wav"
        )
        self._audio_chunks: List[bytes] = []
        self._audio_params: Optional[Tuple[int, int, int]] = None  # rate, width, channels
        self._language = None

    async def handle_event(self, event: Event) -> bool:
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)

            if self._audio_params is None:
                self._audio_params = (chunk.rate, chunk.width, chunk.channels)

            self._audio_chunks.append(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            if self._audio_params is None or not self._audio_chunks:
                _LOGGER.error("AudioStop received without audio data")
                await self.write_event(Transcript(text="").event())
                return False

            rate, width, channels = self._audio_params
            with wave.open(self._wav_path, "wb") as wf:
                wf.setframerate(rate)
                wf.setsampwidth(width)
                wf.setnchannels(channels)
                wf.writeframes(b"".join(self._audio_chunks))

            self._audio_chunks = []
            self._audio_params = None

            cmd = ["yap"]
            if self.cli_args.yap_args:
                cmd.extend(shlex.split(self.cli_args.yap_args))
            if self._language:
                cmd.extend(["-l", self._language])
            cmd.append(self._wav_path)

            _LOGGER.debug("Running command: %s", cmd)
            start_time = time.time()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_SUBPROCESS_TIMEOUT
                )
            except asyncio.TimeoutError:
                _LOGGER.error("Command timed out after %ds", _SUBPROCESS_TIMEOUT)
                proc.kill()
                await self.write_event(Transcript(text="").event())
                return False
            finally:
                _LOGGER.debug("Command execution duration: %.3fs", time.time() - start_time)

            if proc.returncode == 0:
                text = stdout.decode().strip()
                _LOGGER.debug("Transcribed text: %s", text)
                await self.write_event(Transcript(text=text).event())
            else:
                _LOGGER.error("Command failed with return code %d", proc.returncode)
                _LOGGER.error(stderr.decode())
                await self.write_event(Transcript(text="").event())

            return False

        if Transcribe.is_type(event.type):
            transcribe = Transcribe.from_event(event)
            if transcribe.language:
                self._language = transcribe.language
                _LOGGER.debug("Language set to %s", transcribe.language)
            return True

        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            _LOGGER.debug("Sent info")
            return True

        return True
