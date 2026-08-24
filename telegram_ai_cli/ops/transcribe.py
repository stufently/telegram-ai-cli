"""``telegram_media_transcribe`` — turn one voice message into words, locally.

Everything about the *download* is ``media.fetch``'s: the same resolve, the same
refusals, the same server-chosen path, the same ceilings and the same quota. This
module adds three things to it, and each one is a decision.

**It is audio or it is a refusal.** The message must actually carry a
``DocumentAttributeAudio`` — a voice note or an audio file. A video, a photo or a
document is a caller error rather than a slow failure inside a container, and
saying so costs nothing while transcribing a 200 MB video would cost minutes.

**The length is checked twice, and the first check is not trusted.** Telegram
reports a duration, which is metadata the *uploader* supplied; believing it is
worth doing because it saves the whole transfer when the answer is "too long",
but it is not evidence. The container measures the file it actually decodes and
refuses on its own — so the ceiling holds even for an attachment whose declared
duration is a lie.

**The transcript is somebody else's sentence.** It goes into a field named
``transcript``, which ``untrusted.py`` wraps and defangs like a message body,
because an injection can simply be *spoken*: "ignore your instructions" is as
easy to say as to type, and it arrives as a JSON string that looks exactly like
one this project wrote. That is the whole reason this operation assembles its
result through ``telegram_result`` rather than returning a dict of its own.

Effect is ``LOCAL_WRITE``, not ``READ``: it downloads the audio, and the audio
stays — under the media quota, at a path nobody chose, so that the recording the
transcript claims to describe can still be listened to. ``media.fetch`` set that
precedent and the reasoning is unchanged.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .. import transcribe as engine
from ..context import OperationContext
from ..envelope import Envelope
from ..errors import ArtifactTooLarge, InvalidInput
from ..opspec import REGISTRY, Effect, Operation
from ..safety import Capability
from ._client import open_account
from ._common import ReadInput, telegram_result
from ._serialize import peer_summary
from .media import fetch_message_media, resolve_message_for_media


class TranscribeInput(ReadInput):
    chat: str = Field(
        description=(
            "Chat id, @username, or t.me link. A link that names the message "
            "supplies message_id on its own."
        )
    )
    message_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Id of the voice message or audio file to transcribe. Omit only when the "
            "chat argument is a t.me link to that message."
        ),
    )
    language: str | None = Field(
        default=None,
        pattern=engine.LANGUAGE_PATTERN,
        description=(
            "ISO 639-1 code to transcribe as, e.g. 'ru'. Omit to let Whisper detect "
            "the language; the detected one is reported either way."
        ),
    )


def audio_attribute(message: Any) -> Any | None:
    """The message's audio track, or nothing at all.

    Found by class *name*, the way :mod:`telegram_ai_cli.ops._serialize` reads
    every other Telethon type: the generated type set differs between library
    versions, and an import that fails on one of them would break the operation
    rather than the branch. ``mime_type`` is deliberately not the test — it is a
    string the uploader chose, so ``audio/ogg`` on an executable is a free pass.
    """
    document = getattr(getattr(message, "media", None), "document", None)
    for attribute in getattr(document, "attributes", None) or ():
        if type(attribute).__name__ == "DocumentAttributeAudio":
            return attribute
    return None


async def handle_media_transcribe(ctx: OperationContext, params: TranscribeInput) -> Envelope:
    config = ctx.settings.transcribe

    async with open_account(ctx, params.account) as account:
        addressed = await resolve_message_for_media(
            ctx, account.client, params.chat, params.message_id, action="media.transcribe"
        )

        audio = audio_attribute(addressed.message)
        if audio is None:
            raise InvalidInput(
                f"message {addressed.message_id} carries no audio track, so there is "
                "nothing to transcribe",
                suggestion="Transcription accepts voice messages and audio files only.",
            )

        declared = getattr(audio, "duration", None)
        if declared is not None and declared > config.max_audio_seconds:
            # Checked before the download because it saves the whole transfer.
            # Not *trusted*, though: the container measures the real file too.
            raise ArtifactTooLarge(
                f"the audio is {int(declared)}s, over transcribe.max_audio_seconds "
                f"({config.max_audio_seconds}s)",
                suggestion="Raise transcribe.max_audio_seconds if audio this long is wanted.",
            )

        fetched = await fetch_message_media(
            ctx, account.client, addressed, account_label=account.label, action="media.transcribe"
        )

    # Outside the account context on purpose: transcription takes minutes, and
    # holding an MTProto connection (and its session lock) open through a
    # subprocess that is not talking to Telegram is how a read blocks every
    # other operation on the account.
    event = ctx.audit.attempt(
        action="media.transcribe",
        account=account.label,
        actor=ctx.actor,
        peer_id=addressed.ref.peer_id,
        extra={"message_id": addressed.message_id, "artifact_id": fetched.artifact_id},
    )
    try:
        transcript = await engine.transcribe_file_async(
            config, audio=fetched.path, language=params.language
        )
    except BaseException as exc:
        # The container's own words land here and nowhere else. Failure payloads
        # are now redacted and defanged too, but arbitrary stderr still must not
        # be interpolated into prose this project presents as its own.
        ctx.audit.outcome(
            event,
            status="failed",
            error_code=type(exc).__name__,
            detail=engine.container_detail(exc),
        )
        raise
    ctx.audit.outcome(event, status="applied", detail=f"{len(transcript.text)} characters")

    data: dict[str, Any] = {
        "transcript": transcript.text,
        "language": transcript.language,
        "language_detected": params.language is None,
        "language_probability": transcript.language_probability,
        "duration_seconds": transcript.duration_seconds,
        "model": transcript.model,
        "artifact_id": fetched.artifact_id,
        "chat": peer_summary(addressed.entity),
        "message_id": addressed.message_id,
    }
    if ctx.actor == "cli":
        data["path"] = str(fetched.path)

    return telegram_result(ctx, data, account=account.label, returned=1, total=1)


MEDIA_TRANSCRIBE = REGISTRY.register(
    Operation(
        name="media.transcribe",
        cli=("media", "transcribe"),
        mcp_tool="telegram_media_transcribe",
        summary="Transcribe one voice message or audio file, locally and offline.",
        description=(
            "Downloads the audio like `media fetch` does, then runs Whisper (model "
            "'small') in a separate optional Docker image with no network at all, so "
            "the recording never leaves this host. The transcript is a stranger's "
            "words and is marked as untrusted content. Requires the optional image: "
            "build it with `make transcribe-image` and fetch the model once with "
            "`make transcribe-model`; without it the operation refuses and says so."
        ),
        input_model=TranscribeInput,
        effect=Effect.LOCAL_WRITE,
        # The audio, and only the audio. The container mounts the model cache
        # read-only and writes nothing itself, so `paths.downloads` is the whole
        # of what this operation puts on disk — which is what a client's roots
        # have to be checked against.
        local_path="downloads",
        capability=Capability.READ_MEDIA,
        handler=handle_media_transcribe,  # type: ignore[arg-type]
        tags=("read", "media"),
    )
)
