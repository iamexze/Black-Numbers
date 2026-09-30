"""macOS on-device speech recognition via Apple's Speech framework (PyObjC).

This is the voice ear when `BN_STT=macos`. It uses SFSpeechRecognizer with an
audio-engine tap, which runs on-device on modern macOS — free, private, and
low-latency. It requires the pyobjc Speech and AVFoundation bridges and the
microphone / speech-recognition permissions, which macOS prompts for on first
run.

If any of that is missing, `available()` returns False and the app falls back to
typed input, so nothing here can prevent the assistant from starting.
"""

from __future__ import annotations

import queue
import threading
from typing import Iterator

from .base import STT


class MacSpeech(STT):
    name = "macos"

    def __init__(self, locale: str = "en-US"):
        self.locale = locale
        self._ok = False
        try:
            import AVFoundation  # noqa: F401
            import Speech  # noqa: F401

            self._Speech = Speech
            self._AV = AVFoundation
            self._ok = True
        except Exception:
            self._Speech = None
            self._AV = None

    def available(self) -> bool:
        return self._ok

    def listen(self) -> Iterator[str]:
        """Continuous recognition. Yields an utterance each time the recognizer
        reports a stable final result, then restarts for the next phrase.

        Implementation note: the Speech framework is callback-driven and runs on
        a Cocoa run loop. We bridge it to a simple generator with a queue so the
        agent loop can stay a plain `for utterance in stt.listen()`.
        """
        if not self._ok:
            return

        Speech = self._Speech
        AV = self._AV
        out: queue.Queue[str] = queue.Queue()

        recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(
            self._AV.NSLocale.localeWithLocaleIdentifier_(self.locale)
            if hasattr(self._AV, "NSLocale")
            else Speech.NSLocale.localeWithLocaleIdentifier_(self.locale)
        )

        engine = AV.AVAudioEngine.alloc().init()

        def start_request():
            request = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
            request.setShouldReportPartialResults_(False)
            node = engine.inputNode()
            fmt = node.outputFormatForBus_(0)

            def tap(buf, when):
                request.appendAudioPCMBuffer_(buf)

            node.installTapOnBus_bufferSize_format_block_(0, 1024, fmt, tap)
            engine.prepare()
            engine.startAndReturnError_(None)

            def handler(result, error):
                if result is not None and result.isFinal():
                    text = str(result.bestTranscription().formattedString())
                    if text.strip():
                        out.put(text)
                    # restart for the next phrase
                    node.removeTapOnBus_(0)
                    engine.stop()
                    threading.Timer(0.05, start_request).start()

            recognizer.recognitionTaskWithRequest_resultHandler_(request, handler)

        # Ask for authorization, then start. This blocks briefly on first run
        # while macOS shows its permission dialog.
        auth_done = threading.Event()

        def auth_cb(status):
            auth_done.set()

        Speech.SFSpeechRecognizer.requestAuthorization_(auth_cb)
        auth_done.wait(timeout=30)
        start_request()

        while True:
            try:
                yield out.get(timeout=3600)
            except queue.Empty:
                continue
