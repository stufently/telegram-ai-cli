"""The whole of the optional transcription image: one file, two subcommands.

This runs *inside* ``Dockerfile.transcribe`` and is not part of the installed
package — that is the point of the split. ``telegram_ai_cli`` never imports
faster-whisper, never grows a model dependency, and an installation that does
not build this image is not carrying any of it.

It talks to the caller in exactly two ways, both of which the host side depends
on and neither of which is free-form:

**JSON on stdout**, one object, on success only. Anything else is a failure —
printing a partial result would let an empty transcript read as "the speaker
said nothing", which is a lie the caller cannot detect.

**An exit status that says why.** ``3`` is "the model is not in the cache" and
``4`` is "the audio is longer than the ceiling", because those two are the ones
a person can act on and guessing them from stderr would mean parsing prose.

``transcribe`` runs with ``local_files_only``: the container has no network at
all when the host invokes it, and asking for one here would turn a missing model
into a DNS error rather than into exit code 3. Downloading is the separate
``download-model`` subcommand, which is the only invocation that ever gets a
network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

#: Not a parameter. The image *is* the model — see TranscribeConfig for why
#: making this configurable would grow a model manager.
MODEL_SIZE = "small"

#: Kept in step with telegram_ai_cli/transcribe.py.
EXIT_FAILED = 1
EXIT_MODEL_MISSING = 3
EXIT_AUDIO_TOO_LONG = 4

#: int8 on CPU: roughly four times faster than float32 and about a quarter of
#: the memory, for a difference in word error rate that does not show up on the
#: kind of audio a voice message contains.
COMPUTE_TYPE = "int8"


def fail(status: int, message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(status)


def load_model(*, offline: bool):
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(
            MODEL_SIZE,
            device="cpu",
            compute_type=COMPUTE_TYPE,
            local_files_only=offline,
        )
    except Exception as exc:  # noqa: BLE001 - every cause here means the same thing
        if offline:
            fail(
                EXIT_MODEL_MISSING,
                f"the {MODEL_SIZE} model is not in {os.environ.get('HF_HOME', '(unset HF_HOME)')}",
            )
        fail(EXIT_FAILED, f"could not load the {MODEL_SIZE} model: {type(exc).__name__}")
        raise  # pragma: no cover - fail() never returns


def cmd_download_model() -> None:
    """The only step that ever touches the network, run once, on purpose."""
    load_model(offline=False)
    print(json.dumps({"ok": True, "model": MODEL_SIZE}))


def cmd_transcribe(path: str, language: str | None, max_seconds: int) -> None:
    model = load_model(offline=True)

    try:
        segments, info = model.transcribe(
            path,
            language=language,
            beam_size=5,
            # Silence is where Whisper invents sentences, and an invented
            # sentence in a transcript is indistinguishable from something the
            # speaker actually said. The VAD asset ships inside the wheel, so
            # this needs no network.
            vad_filter=True,
        )
    except Exception as exc:  # noqa: BLE001 - reported as a status, not a traceback
        fail(EXIT_FAILED, f"could not decode the audio: {type(exc).__name__}")
        raise  # pragma: no cover

    # The host already refused on the duration Telegram reported, but that
    # figure is metadata the uploader supplied. This is the one measured from
    # the file itself, and it is checked before a single segment is decoded.
    duration = getattr(info, "duration", None)
    if duration is not None and duration > max_seconds:
        fail(EXIT_AUDIO_TOO_LONG, f"audio is {duration:.0f}s, over the {max_seconds}s ceiling")

    text = "".join(segment.text for segment in segments).strip()
    print(
        json.dumps(
            {
                "ok": True,
                "text": text,
                "language": getattr(info, "language", None),
                "language_probability": getattr(info, "language_probability", None),
                "duration": duration,
                "model": MODEL_SIZE,
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="transcriber")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("download-model", help="Fetch the model into HF_HOME. Needs a network.")

    run = sub.add_parser("transcribe", help="Transcribe one audio file. Needs no network.")
    run.add_argument("--input", required=True)
    run.add_argument("--language", default=None)
    run.add_argument("--max-seconds", type=int, required=True)

    args = parser.parse_args()
    if args.command == "download-model":
        cmd_download_model()
    else:
        cmd_transcribe(args.input, args.language, args.max_seconds)


if __name__ == "__main__":
    main()
