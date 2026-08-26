"""The one place that turns an audio file into words, and it is a container.

Transcription is the only feature in this project that needs half a gigabyte of
model weights, so it is not in the package and it is not in the main image. It
is a **separate, optional** image that an installation builds on purpose. Not
building it costs nothing: no dependency, no behaviour, and — this is the part
that has to be designed rather than hoped for — no cryptic failure. "The image
is not there" is the normal case, not the exceptional one, so it produces a
sentence naming the image and the command that builds it.

**The audio does not leave this host.** That is a decision, not a default, and
it is enforced by ``--network none`` on the transcribing container rather than
asserted in a README. A process with no network cannot upload anything, whatever
its code says and whatever a future dependency of it decides to do. The model is
therefore fetched by a *separate, explicit* step (``make transcribe-model``),
which is the only invocation that ever has a network at all.

What the container is given, and nothing more:

- **one file**, bind-mounted read-only at a fixed path. Not the download
  directory — that holds every attachment this account ever fetched, from every
  chat, and a transcription of one voice message has no business being able to
  read the rest;
- **the model cache**, mounted separately and writable, so the weights are
  downloaded once and reused;
- **this user's uid:gid**. Never root — and the refusal lives in
  :func:`resolve_run_as`, which *every* path goes through, because writing it in
  the default branch left ``run_as: "0:0"`` a way to turn it off from a config
  file;
- **a memory ceiling**, which is not defence in depth. faster-whisper decodes
  the whole file before it can report a duration, so the length check *inside*
  the container happens after the allocation it is meant to bound. The limit is
  what makes a fabricated duration on a many-hour file an exit status instead of
  an out-of-memory host.

**A timeout kills the container, not just the client.** ``--rm`` removes a
container that has *exited*; killing ``docker run`` leaves this one holding the
CPU and memory the timeout existed to reclaim. So the run is named, and the
timeout path removes it by that name.

**Nothing the container prints reaches the caller as an error message.**
The failure envelope redacts and defangs what it serialises, but arbitrary
container output still does not belong in project-authored diagnostic prose.
It is attached to the exception in an attribute the envelope never serialises,
read only by :func:`container_detail` on its way to the audit log, where a
person debugging a broken image can find it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess  # noqa: S404 - the whole point of the module is to run one container
from dataclasses import dataclass
from pathlib import Path

from .config import TranscribeConfig
from .errors import ArtifactTooLarge, TranscriberUnavailable, TranscriptionFailed

#: Baked into the image, deliberately not configurable. See ``TranscribeConfig``.
MODEL_SIZE = "small"

#: Repeated from the default so a message can name the image without a Settings
#: object in hand — the "you have not built it" path has one before it has one.
DEFAULT_IMAGE = TranscribeConfig().image

#: Fixed mount points. The container never learns a host path.
CONTAINER_AUDIO_DIR = "/audio"
CONTAINER_MODEL_DIR = "/models"

#: Exit statuses the entrypoint uses to say *why*, so the mapping below is not a
#: guess at the meaning of stderr. Kept in step with docker/transcribe/entrypoint.py.
EXIT_MODEL_MISSING = 3
EXIT_AUDIO_TOO_LONG = 4

#: An ISO 639-1 code and nothing else. The value becomes an element of an argv
#: that also contains ``--privileged``, and "it is only ever two letters" is a
#: property worth enforcing rather than assuming.
LANGUAGE_PATTERN = r"^[a-z]{2}$"

_LANGUAGE_RE = re.compile(LANGUAGE_PATTERN)
_RUN_AS_RE = re.compile(r"^(\d+):(\d+)$")

#: A ceiling on what the container may allocate, and it is not belt-and-braces.
#: faster-whisper decodes the *whole* file into memory before it reports a
#: duration, so the length check inside the container happens after the
#: allocation it is meant to bound. A 100 MiB opus file — which
#: ``download.max_file_bytes`` permits by default — is many hours of audio and
#: several gigabytes of float32 once decoded. With a limit the kernel kills the
#: container and the operation reports a failure; without one it is the host
#: that runs out of memory.
MEMORY_LIMIT = "2g"

#: Whisper is one process with a handful of threads; a fork bomb in a decoder
#: parsing a stranger's file is not something to leave unbounded.
PIDS_LIMIT = "256"


@dataclass(frozen=True, slots=True)
class Transcript:
    """What the container heard, plus what it thinks it heard it in."""

    text: str
    language: str | None
    language_probability: float | None
    duration_seconds: float | None
    model: str


def default_run_as() -> str:
    """This process's own uid:gid.

    The Makefile takes the same position for the same reason (``UID ?= $(shell
    id -u)``): a container that writes to a bind mount as root leaves files
    behind that the host user cannot read or delete. Running the transcriber as
    root would also mean an audio decoder parsing a stranger's file with more
    privilege than the tool that fetched it.
    """
    return f"{os.getuid()}:{os.getgid()}"


def resolve_run_as(config: TranscribeConfig) -> str:
    """Decide ``--user``, and refuse root however it was arrived at.

    The refusal lives *here*, not in :func:`default_run_as`, because it was
    written there first and that was the bug: a configured ``run_as: "0:0"``
    never reached the check, so a guarantee stated in three files could be
    turned off by a config key. Every path that produces a ``--user`` value now
    goes through one function.
    """
    # A blank string is how "unset" arrives from YAML, and it means the same as
    # `None` — this process's own ids. Every *other* value is checked.
    value = (config.run_as or "").strip() or default_run_as()
    match = _RUN_AS_RE.match(value)
    if match is None:
        raise TranscriptionFailed(
            "transcribe.run_as must be numeric 'uid:gid'",
            suggestion="Use the numeric ids, e.g. '1000:1000'; names are not resolved here.",
        )
    if match.group(1) == "0" or match.group(2) == "0":
        raise TranscriptionFailed(
            "refusing to run the transcriber as root",
            suggestion=(
                "Run tg-ai as an ordinary user, or set transcribe.run_as to a non-root uid:gid."
            ),
        )
    return value


def _mount(source: Path | str, target: str, *, read_only: bool = False) -> str:
    """One ``-v`` value, refusing a path Docker would misread.

    ``-v`` splits on colons. A source path containing one does not fail — it
    silently becomes a different mount, with the tail read as a target or as an
    option list. Refusing is the only safe reading.
    """
    text = str(source)
    if ":" in text:
        raise TranscriptionFailed(
            "a path containing ':' cannot be passed to docker -v",
            suggestion="Point paths.downloads and transcribe.model_cache at colon-free paths.",
        )
    if not text.startswith("/"):
        # A relative source is not a bind mount at all — Docker reads it as a
        # *named volume*, so the container would silently get an empty directory
        # under a name it invented, and the operation would write outside every
        # path it declares.
        raise TranscriptionFailed(
            f"{text!r} is not an absolute path, and docker -v would read it as a volume name",
            suggestion="Use absolute paths for paths.downloads and transcribe.model_cache.",
        )
    return f"{text}:{target}:ro" if read_only else f"{text}:{target}"


def container_name() -> str:
    """A name for this run, so a timeout has something to kill.

    ``--rm`` only removes a container once it *exits*; killing the ``docker run``
    client leaves the container running, holding the CPU and the memory the
    timeout was there to reclaim. A name is how the timeout path finds it.
    """
    return f"tgai-transcribe-{secrets.token_hex(8)}"


def build_run_command(
    config: TranscribeConfig,
    *,
    audio: Path,
    language: str | None,
    run_as: str,
    name: str | None = None,
) -> list[str]:
    """The exact argv, built in one place so the flags can be asserted.

    Kept pure and separate from running it because the security-relevant part of
    this feature *is* the argument list: ``--network none`` and a single
    read-only file are the claim, and a claim in a string that a test can read is
    a claim that survives the next refactor.
    """
    if language is not None and not _LANGUAGE_RE.match(language):
        raise TranscriptionFailed(f"{language!r} is not an ISO 639-1 language code")
    if not config.image or config.image.startswith("-"):
        raise TranscriptionFailed("transcribe.image is empty or looks like a flag")

    suffix = audio.suffix if re.match(r"^\.[A-Za-z0-9]{1,8}$", audio.suffix) else ".bin"
    command = [
        config.docker_binary,
        "run",
        "--rm",
        # The claim, as an argument. A process with no network interface cannot
        # send the audio anywhere, whatever it or its dependencies want.
        "--network",
        "none",
        "--user",
        run_as,
        "--memory",
        MEMORY_LIMIT,
        # Without this the limit above is advice: the kernel would swap rather
        # than refuse, and the container would take the host down slowly.
        "--memory-swap",
        MEMORY_LIMIT,
        "--pids-limit",
        PIDS_LIMIT,
    ]
    if name is not None:
        command += ["--name", name]
    command += [
        "-v",
        _mount(audio, f"{CONTAINER_AUDIO_DIR}/input{suffix}", read_only=True),
        "-v",
        # Read-only too, and that is not merely tidiness: it means the
        # transcribing container writes *nothing at all*, anywhere. The cache is
        # filled by `make transcribe-model`, which is a separate command a person
        # runs, so the only thing this operation puts on disk is the audio it
        # downloaded — which is what `local_path="downloads"` declares.
        _mount(config.model_cache, CONTAINER_MODEL_DIR, read_only=True),
        "-e",
        f"HF_HOME={CONTAINER_MODEL_DIR}",
        config.image,
        "transcribe",
        "--input",
        f"{CONTAINER_AUDIO_DIR}/input{suffix}",
        "--max-seconds",
        str(config.max_audio_seconds),
    ]
    if language is not None:
        command += ["--language", language]
    return command


def build_inspect_command(config: TranscribeConfig) -> list[str]:
    """Ask Docker whether the optional image exists, without pulling anything."""
    return [config.docker_binary, "image", "inspect", config.image]


def _image_present(config: TranscribeConfig) -> bool:
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell, fixed binary
            build_inspect_command(config),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TranscriberUnavailable(
            f"transcription needs Docker, and {config.docker_binary!r} is not on PATH",
            suggestion=(
                "Install Docker, or set transcribe.docker_binary to the client on this host."
            ),
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TranscriberUnavailable(
            "the Docker daemon did not answer within 30s",
            suggestion="Check that the daemon is running and reachable by this user.",
        ) from exc
    return completed.returncode == 0


def _missing_image_error(config: TranscribeConfig) -> TranscriberUnavailable:
    return TranscriberUnavailable(
        f"transcription is optional and its image, {config.image}, is not built on this host",
        suggestion=(
            "Build it with `make transcribe-image`, then download the model once with "
            "`make transcribe-model`. Nothing else in this tool needs either."
        ),
    )


def transcribe_file(
    config: TranscribeConfig,
    *,
    audio: Path,
    language: str | None,
) -> Transcript:
    """Run the container over one file and parse what it says.

    Synchronous and blocking: it is a subprocess that takes minutes, and the
    caller runs it off the event loop. Returning a partial result on failure was
    never an option — an empty transcript reads as "the person said nothing",
    which is a lie a model has no way to detect.
    """
    if not config.model_cache.is_dir():
        # `docker -v` *creates* a missing absolute source as an empty directory
        # rather than failing, so without this the container would start, find no
        # model, and the operation would write a directory it never declared.
        raise TranscriberUnavailable(
            f"the model cache {config.model_cache} does not exist",
            suggestion=(
                "Download the model once with `make transcribe-model`. It is a separate "
                "step because transcription itself runs with no network."
            ),
        )
    if not _image_present(config):
        raise _missing_image_error(config)

    name = container_name()
    command = build_run_command(
        config, audio=audio, language=language, run_as=resolve_run_as(config), name=name
    )

    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell, fixed binary
            command,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - inspect would have caught it
        raise TranscriberUnavailable(
            f"transcription needs Docker, and {config.docker_binary!r} is not on PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        # The timeout killed the *client*, not the container: `--rm` removes one
        # that has exited, and this one has not. Left alone it would keep the CPU
        # and the memory the timeout exists to reclaim.
        force_remove(config, name)
        raise TranscriptionFailed(
            "the transcriber exceeded transcribe.timeout_seconds "
            f"({config.timeout_seconds}s) and was stopped",
            suggestion="Retry, or raise transcribe.timeout_seconds for audio this long.",
            retry_after=1,
        ) from exc

    return _result_from_process(
        config,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


async def transcribe_file_async(
    config: TranscribeConfig,
    *,
    audio: Path,
    language: str | None,
) -> Transcript:
    """Cancellation-safe asynchronous variant used by MCP and the CLI.

    Image inspection stays in a worker thread because it never creates a
    container and is independently bounded to 30 seconds. The long-running
    ``docker run`` process is owned by the event loop: on timeout or caller
    cancellation we stop that client, wait for it to settle, and only then
    remove the named container. Waiting before ``rm`` closes the creation race
    where cleanup could run just before Docker registered the container.
    """
    if not config.model_cache.is_dir():
        raise TranscriberUnavailable(
            f"the model cache {config.model_cache} does not exist",
            suggestion=(
                "Download the model once with `make transcribe-model`. It is a separate "
                "step because transcription itself runs with no network."
            ),
        )
    if not await asyncio.to_thread(_image_present, config):
        raise _missing_image_error(config)

    name = container_name()
    command = build_run_command(
        config, audio=audio, language=language, run_as=resolve_run_as(config), name=name
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # pragma: no cover - inspect normally catches it
        raise TranscriberUnavailable(
            f"transcription needs Docker, and {config.docker_binary!r} is not on PATH"
        ) from exc

    try:
        async with asyncio.timeout(config.timeout_seconds):
            stdout_bytes, stderr_bytes = await process.communicate()
    except asyncio.CancelledError:
        await _stop_process(process)
        await force_remove_async(config, name)
        raise
    except TimeoutError as exc:
        await _stop_process(process)
        await force_remove_async(config, name)
        raise TranscriptionFailed(
            "the transcriber exceeded transcribe.timeout_seconds "
            f"({config.timeout_seconds}s) and was stopped",
            suggestion="Retry, or raise transcribe.timeout_seconds for audio this long.",
            retry_after=1,
        ) from exc

    return _result_from_process(
        config,
        returncode=int(process.returncode or 0),
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )


def _result_from_process(
    config: TranscribeConfig, *, returncode: int, stdout: str, stderr: str
) -> Transcript:
    """Map either subprocess implementation onto the public result/errors."""
    if returncode == EXIT_MODEL_MISSING:
        raise TranscriberUnavailable(
            f"the {MODEL_SIZE} Whisper model is not in transcribe.model_cache",
            suggestion=(
                "Download it once with `make transcribe-model`. It is a separate step "
                "because transcription itself runs with no network."
            ),
        )
    if returncode == EXIT_AUDIO_TOO_LONG:
        # Not `TranscriptionFailed`: that class is retryable, and the same file
        # will be too long every time — a retry would only download it again.
        # This is the host-side ceiling's own error, reached by the check that
        # measured the file rather than believing its metadata.
        raise ArtifactTooLarge(
            f"the audio is longer than transcribe.max_audio_seconds ({config.max_audio_seconds}s)",
            suggestion="Raise transcribe.max_audio_seconds if audio this long is genuinely wanted.",
        )
    if returncode != 0:
        # No stderr in the *message*: see the module docstring. It is attached to
        # the exception instead, where only `container_detail` — the audit path —
        # reads it.
        failure = TranscriptionFailed(
            f"the transcriber exited with status {returncode}",
            suggestion="The container's own output is in the audit log for this operation.",
        )
        failure.container_output = stderr  # type: ignore[attr-defined]
        raise failure

    return _parse(stdout)


def force_remove(config: TranscribeConfig, name: str) -> None:
    """Best-effort ``docker rm -f`` for a container this module started.

    Best-effort on purpose: the caller is already raising, and a daemon that
    cannot be reached to clean up is not a second error worth replacing the
    first one with. It is bounded so that cleanup cannot itself hang.
    """
    try:
        subprocess.run(  # noqa: S603 - argv list, no shell, fixed binary
            [config.docker_binary, "rm", "-f", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """Stop a Docker client and wait until it can no longer create a container."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def force_remove_async(config: TranscribeConfig, name: str) -> None:
    """Asynchronous best-effort counterpart to :func:`force_remove`."""
    try:
        cleanup = await asyncio.create_subprocess_exec(
            config.docker_binary,
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=30)
        except TimeoutError:
            cleanup.kill()
            await cleanup.wait()
    except (OSError, subprocess.SubprocessError):
        return


def container_detail(exc: BaseException) -> str:
    """A short, marker-free note for the audit log — never for a response.

    Prefers the container's own output where there is any, because that is the
    text a person debugging a broken image actually needs and the response
    deliberately does not carry it.

    Defanged either way. The log is read by a person, but it is also read *back*
    by tooling, and a delimiter that survives into any document this project
    frames elsewhere is a delimiter that can close a frame it did not open.
    """
    from .untrusted import neutralize

    output = getattr(exc, "container_output", None)
    detail = f"{exc}: {output}" if output else str(exc)
    return neutralize(detail)[:200]


def _parse(stdout: str) -> Transcript:
    """Read the container's JSON, treating a malformed answer as a failure.

    A transcriber that printed something unparseable has not transcribed
    anything, and coercing that into an empty string would report silence.
    """
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise TranscriptionFailed("the transcriber did not return a JSON result") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise TranscriptionFailed("the transcriber returned a result with no transcript in it")

    return Transcript(
        text=payload["text"],
        language=payload.get("language"),
        language_probability=payload.get("language_probability"),
        duration_seconds=payload.get("duration"),
        model=str(payload.get("model") or MODEL_SIZE),
    )
