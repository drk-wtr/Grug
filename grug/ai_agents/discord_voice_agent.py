"""
Voice Agent used for TTS and STT in Discord voice channels.

Based on the [SpeechRecognitionSink](https://github.com/imayhaveborkedit/discord-ext-voice-recv/blob/main/discord/ext/voice_recv/extras/speechrecognition.py)
from discord-ext-voice-recv.
"""

import array
import asyncio
import audioop
import time
from collections import defaultdict
from concurrent.futures import Future as CFuture
from typing import Any, Awaitable, Callable, Final, Optional, Protocol, TypedDict, TypeVar

import speech_recognition as sr  # type: ignore
from discord import User, VoiceChannel
from discord.ext.voice_recv import AudioSink, SilencePacket, VoiceData
from loguru import logger
from speech_recognition.recognizers.whisper_api import openai as sr_openai

T = TypeVar("T")


class SRStopper(Protocol):
    def __call__(self, wait: bool = True, /) -> None: ...


SRTextCB = Callable[[User, str], Any]


class _StreamData(TypedDict):
    stopper: Optional[SRStopper]
    recognizer: sr.Recognizer
    buffer: array.array[int]


class SpeechRecognitionSink(AudioSink):  # type: ignore
    _stream_data: defaultdict[int, _StreamData] = defaultdict(
        lambda: _StreamData(stopper=None, recognizer=sr.Recognizer(), buffer=array.array("B"))
    )

    def __init__(
        self,
        discord_channel: VoiceChannel,
        *,
        text_cb: Optional[SRTextCB] = None,
    ):
        super().__init__(None)
        self.discord_channel: VoiceChannel = discord_channel
        self.text_cb: Optional[SRTextCB] = text_cb

    def _await(self, coro: Awaitable[T]) -> CFuture[T]:
        assert self.client is not None
        return asyncio.run_coroutine_threadsafe(coro, self.client.loop)

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[User], data: VoiceData) -> None:
        # Ignore silence packets and packets from users we don't have data for
        if isinstance(data.packet, SilencePacket) or user is None:
            return

        sdata = self._stream_data[user.id]
        sdata["buffer"].extend(data.pcm)

        if not sdata["stopper"]:
            sdata["stopper"] = sdata["recognizer"].listen_in_background(
                source=DiscordSRAudioSource(sdata["buffer"]),
                callback=self.background_listener(user),
                phrase_time_limit=10,
            )

    def background_listener(self, user: User):
        text_cb = self.text_cb or self.get_default_text_callback()

        def callback(_recognizer: sr.Recognizer, _audio: sr.AudioData):
            # Don't process empty audio data or audio data that is too small
            if _audio.frame_data == b"" or len(bytes(_audio.frame_data)) < 10000:
                return None

            # Get the text from the audio data
            text_output = None
            try:
                text_output = sr_openai.recognize(_recognizer, _audio)
            except sr.UnknownValueError:
                logger.debug("Bad speech chunk")

            if text_output is not None:
                text_cb(user, text_output)

        return callback

    @staticmethod
    def get_default_text_callback() -> SRTextCB:
        def cb(user: Optional[User], text: Optional[str]) -> Any:
            # WEIRDEST BUG EVER: for some reason whisper keeps getting the word "you" from the recognizer, so
            #                    we'll just ignore any text segments that are just "you"
            if text.lower() != "you" if text else False:
                logger.info(f"{user.display_name if user else "Someone"} said: {text}")

        return cb

    # @AudioSink.listener()
    # def on_voice_member_disconnect(self, member: Member, ssrc: Optional[int]) -> None:
    #     self._drop(member.id)

    def cleanup(self) -> None:
        for user_id in tuple(self._stream_data.keys()):
            self._drop(user_id)

    def _drop(self, user_id: int) -> None:
        if user_id in self._stream_data:
            data = self._stream_data.pop(user_id)
            stopper = data.get("stopper")
            if stopper:
                stopper()

            buffer = data.get("buffer")
            if buffer:
                # arrays don't have a clear function
                del buffer[:]


class DiscordSRAudioSource(sr.AudioSource):
    little_endian: Final[bool] = True
    SAMPLE_RATE: Final[int] = 48_000
    SAMPLE_WIDTH: Final[int] = 2
    CHANNELS: Final[int] = 2
    CHUNK: Final[int] = 960

    # noinspection PyMissingConstructor
    def __init__(self, buffer: array.array[int], read_timeout: int = 10):
        self.read_timeout = read_timeout
        self.buffer = buffer
        self._entered: bool = False

    @property
    def stream(self):
        return self

    def __enter__(self):
        if self._entered:
            logger.warning("Already entered sr audio source")
        self._entered = True
        return self

    def __exit__(self, *exc) -> None:
        self._entered = False
        if any(exc):
            logger.exception("Error closing sr audio source")

    def read(self, size: int) -> bytes:
        for _ in range(self.read_timeout):
            if len(self.buffer) < size * self.CHANNELS:
                time.sleep(0.01)
            else:
                break
        else:
            if len(self.buffer) <= 100:
                return b""

        chunk_size = size * self.CHANNELS
        audio_chunk = self.buffer[:chunk_size].tobytes()
        del self.buffer[: min(chunk_size, len(audio_chunk))]
        audio_chunk = audioop.tomono(audio_chunk, 2, 1, 1)
        return audio_chunk

    def close(self) -> None:
        self.buffer.clear()
