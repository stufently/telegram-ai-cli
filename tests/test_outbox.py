"""Choosing a file to send — the rule that keeps a chat tool off the disk.

`media.fetch` refuses to let a caller choose where a file lands. Sending is the
same problem pointed the other way: a caller that names any path turns a
Telegram tool into an exfiltration primitive, and the destination is a chat
somebody else reads. So a file may be sent only from a directory the operator
nominated, containment is decided after symlinks are resolved, and the size
ceiling is answered from `stat()` before a single byte is uploaded.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from telegram_ai_cli.config import (
    TELEGRAM_MAX_UPLOAD_BYTES,
    PathsConfig,
    Settings,
    UploadConfig,
)
from telegram_ai_cli.errors import (
    ArtifactTooLarge,
    ErrorCode,
    InsecurePermissions,
    InvalidInput,
    NotAllowlisted,
    NotFound,
)
from telegram_ai_cli.outbox import (
    Delivery,
    describe_delivery,
    file_preview,
    human_bytes,
    resolve_outbound,
    upload_roots,
)

CONTENT = b"a small file nobody minds sending"


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("TGAI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    root = tmp_path / "outbox"
    root.mkdir()
    return root


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    root = tmp_path / "downloads"
    root.mkdir()
    return root


def settings_for(outbox: Path, downloads: Path, **upload: object) -> Settings:
    return Settings(
        paths=PathsConfig(uploads=outbox, downloads=downloads),
        upload=UploadConfig(**upload),  # type: ignore[arg-type]
    )


def write(path: Path, content: bytes = CONTENT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# --- the happy path --------------------------------------------------------


def test_a_file_in_the_outbox_is_described_completely(outbox: Path, downloads: Path) -> None:
    """Everything the preview needs comes from one resolution, not three."""
    write(outbox / "report.pdf")
    resolved = resolve_outbound(settings_for(outbox, downloads), "report.pdf")

    assert resolved.path == (outbox / "report.pdf").resolve()
    assert resolved.name == "report.pdf"
    assert resolved.size_bytes == len(CONTENT)
    assert resolved.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert resolved.mime == "application/pdf"
    assert resolved.delivery is Delivery.DOCUMENT


def test_a_relative_name_is_read_from_the_outbox_not_the_working_directory(
    outbox: Path, downloads: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process's cwd is whatever launched the server; it is not an input."""
    write(outbox / "note.txt", b"the one that should be sent")
    decoy = write(tmp_path / "elsewhere" / "note.txt", b"the one in the working directory")
    monkeypatch.chdir(decoy.parent)

    resolved = resolve_outbound(settings_for(outbox, downloads), "note.txt")
    assert resolved.path == (outbox / "note.txt").resolve()


def test_an_absolute_path_inside_the_outbox_is_accepted(outbox: Path, downloads: Path) -> None:
    path = write(outbox / "sub" / "deep.txt")
    resolved = resolve_outbound(settings_for(outbox, downloads), str(path))
    assert resolved.path == path.resolve()


# --- containment -----------------------------------------------------------


def test_a_path_outside_every_root_is_refused(
    outbox: Path, downloads: Path, tmp_path: Path
) -> None:
    secret = write(tmp_path / "id_ed25519", b"PRIVATE KEY")
    with pytest.raises(NotAllowlisted) as refusal:
        resolve_outbound(settings_for(outbox, downloads), str(secret))
    assert refusal.value.code is ErrorCode.FORBIDDEN_BY_ALLOWLIST
    # The refusal has to say where files *may* come from, or the caller's only
    # way forward is to guess another path and try again.
    assert str(outbox) in refusal.value.message


def test_traversal_out_of_the_outbox_is_refused(
    outbox: Path, downloads: Path, tmp_path: Path
) -> None:
    write(tmp_path / "secret.txt", b"not yours")
    with pytest.raises(NotAllowlisted):
        resolve_outbound(settings_for(outbox, downloads), "../secret.txt")


def test_a_symlink_pointing_out_of_the_outbox_is_refused(
    outbox: Path, downloads: Path, tmp_path: Path
) -> None:
    """The case a plain prefix check waves through.

    The name is inside the root; the bytes are not. Containment is therefore
    decided on the resolved path, not on the string the caller typed.
    """
    secret = write(tmp_path / "secret.txt", b"not yours")
    (outbox / "innocent.txt").symlink_to(secret)

    with pytest.raises(NotAllowlisted):
        resolve_outbound(settings_for(outbox, downloads), "innocent.txt")


def test_a_symlink_that_stays_inside_the_outbox_is_fine(outbox: Path, downloads: Path) -> None:
    real = write(outbox / "real.txt")
    (outbox / "link.txt").symlink_to(real)
    resolved = resolve_outbound(settings_for(outbox, downloads), "link.txt")
    assert resolved.path == real.resolve()


def test_the_download_directory_is_not_an_outbox_by_default(outbox: Path, downloads: Path) -> None:
    """A downloaded file is a file a stranger chose.

    Re-sending one into another chat is a decision the operator takes once, in
    the configuration — not one a tool call takes on its own.
    """
    fetched = write(downloads / "deadbeef.bin")
    with pytest.raises(NotAllowlisted):
        resolve_outbound(settings_for(outbox, downloads), str(fetched))

    permitted = settings_for(outbox, downloads, allow_downloads_dir=True)
    assert resolve_outbound(permitted, str(fetched)).path == fetched.resolve()


def test_upload_roots_lists_what_the_configuration_permits(outbox: Path, downloads: Path) -> None:
    assert upload_roots(settings_for(outbox, downloads)) == (outbox.resolve(),)
    assert upload_roots(settings_for(outbox, downloads, allow_downloads_dir=True)) == (
        outbox.resolve(),
        downloads.resolve(),
    )


def test_a_missing_outbox_directory_is_explained_rather_than_crashed(
    tmp_path: Path, downloads: Path
) -> None:
    """ "No such file" would send whoever configured this looking in the wrong place."""
    absent = tmp_path / "never-created"
    with pytest.raises(NotFound) as failure:
        resolve_outbound(settings_for(absent, downloads), "whatever.txt")
    assert "paths.uploads" in failure.value.message
    assert str(absent) in failure.value.message


# --- what is on the other end of the path ----------------------------------


def test_a_missing_file_is_reported_as_missing(outbox: Path, downloads: Path) -> None:
    with pytest.raises(NotFound) as failure:
        resolve_outbound(settings_for(outbox, downloads), "absent.txt")
    assert failure.value.code is ErrorCode.NOT_FOUND


def test_a_directory_is_not_a_file_to_send(outbox: Path, downloads: Path) -> None:
    (outbox / "folder").mkdir()
    with pytest.raises(InvalidInput, match="regular file"):
        resolve_outbound(settings_for(outbox, downloads), "folder")


def test_an_empty_file_is_refused_here_rather_than_by_telegram(
    outbox: Path, downloads: Path
) -> None:
    write(outbox / "empty.txt", b"")
    with pytest.raises(InvalidInput, match="empty"):
        resolve_outbound(settings_for(outbox, downloads), "empty.txt")


@pytest.mark.parametrize("raw", ["", "   ", "with\nnewline.txt", "with\x00nul.txt"])
def test_a_path_that_is_not_a_path_is_refused(outbox: Path, downloads: Path, raw: str) -> None:
    with pytest.raises(InvalidInput):
        resolve_outbound(settings_for(outbox, downloads), raw)


# --- the ceiling -----------------------------------------------------------


def test_a_file_over_the_ceiling_is_refused_before_anything_is_uploaded(
    outbox: Path, downloads: Path
) -> None:
    """From `stat()`, not from a failed upload halfway through.

    The failure has to name both numbers: "too large" without the ceiling
    leaves the caller with nothing to decide from.
    """
    write(outbox / "big.bin", b"x" * 50)
    settings = settings_for(outbox, downloads, max_file_bytes=10)

    with pytest.raises(ArtifactTooLarge) as refusal:
        resolve_outbound(settings, "big.bin")
    assert refusal.value.code is ErrorCode.ARTIFACT_TOO_LARGE
    assert "50" in refusal.value.message
    assert "10" in refusal.value.message
    assert "upload.max_file_bytes" in (refusal.value.suggestion or "")


def test_a_file_at_the_ceiling_exactly_is_still_allowed(outbox: Path, downloads: Path) -> None:
    write(outbox / "exact.bin", b"x" * 10)
    settings = settings_for(outbox, downloads, max_file_bytes=10)
    assert resolve_outbound(settings, "exact.bin").size_bytes == 10


def test_the_configured_ceiling_cannot_exceed_telegrams_own() -> None:
    """Configuring more than Telegram accepts only moves the failure later."""
    with pytest.raises(ValidationError):
        UploadConfig(max_file_bytes=TELEGRAM_MAX_UPLOAD_BYTES + 1)


# --- how it will be sent ---------------------------------------------------


@pytest.mark.parametrize(
    ("name", "delivery"),
    [
        ("photo.jpg", Delivery.PHOTO),
        ("photo.JPEG", Delivery.PHOTO),
        ("shot.png", Delivery.PHOTO),
        # Telethon's is_image() knows png and jpeg only; every other picture
        # format is uploaded as a document with its bytes intact.
        ("sticker.webp", Delivery.DOCUMENT),
        ("scan.bmp", Delivery.DOCUMENT),
        ("iphone.heic", Delivery.DOCUMENT),
        ("clip.mp4", Delivery.VIDEO),
        ("clip.mov", Delivery.VIDEO),
        ("note.ogg", Delivery.VOICE),
        ("note.opus", Delivery.VOICE),
        ("song.mp3", Delivery.AUDIO),
        ("report.pdf", Delivery.DOCUMENT),
        ("archive.tar.gz", Delivery.DOCUMENT),
        ("noextension", Delivery.DOCUMENT),
    ],
)
def test_the_delivery_form_follows_the_file_type(
    outbox: Path, downloads: Path, name: str, delivery: Delivery
) -> None:
    write(outbox / name)
    resolved = resolve_outbound(settings_for(outbox, downloads), name)
    assert resolved.delivery is delivery


@pytest.mark.parametrize("name", ["photo.jpg", "clip.mp4", "note.ogg", "song.mp3"])
def test_as_document_overrides_every_guess(outbox: Path, downloads: Path, name: str) -> None:
    """The one guarantee this tool can actually make about the bytes."""
    write(outbox / name)
    resolved = resolve_outbound(settings_for(outbox, downloads), name, as_document=True)
    assert resolved.delivery is Delivery.DOCUMENT
    assert resolved.forced_document is True
    assert "unchanged" in describe_delivery(resolved)


def test_a_compressed_form_says_so_and_says_how_to_avoid_it(outbox: Path, downloads: Path) -> None:
    write(outbox / "photo.jpg")
    resolved = resolve_outbound(settings_for(outbox, downloads), "photo.jpg")
    described = describe_delivery(resolved)
    assert "compress" in described.lower()
    assert "as_document" in described


def test_the_preview_states_name_size_type_and_delivery(outbox: Path, downloads: Path) -> None:
    """The approval surface. A person reads this and decides."""
    write(outbox / "report.pdf")
    resolved = resolve_outbound(settings_for(outbox, downloads), "report.pdf")
    preview = file_preview(resolved)

    assert "report.pdf" in preview
    assert str(len(CONTENT)) in preview  # the exact byte count, not only "33 B"
    assert "application/pdf" in preview
    assert resolved.sha256 in preview
    assert "document" in preview


def test_the_preview_cannot_be_used_to_redraw_the_terminal(outbox: Path, downloads: Path) -> None:
    """A file name is caller-supplied text that a person is about to read."""
    write(outbox / "inv\x1b[2Koice.pdf")
    resolved = resolve_outbound(settings_for(outbox, downloads), "inv\x1b[2Koice.pdf")
    assert "\x1b" not in file_preview(resolved)
    assert "\x1b" not in resolved.name


@pytest.mark.parametrize(
    ("value", "rendered"),
    [(0, "0 B"), (512, "512 B"), (1024, "1.0 KiB"), (1536, "1.5 KiB"), (5 * 1024**2, "5.0 MiB")],
)
def test_human_bytes_reads_like_a_file_manager(value: int, rendered: str) -> None:
    assert human_bytes(value) == rendered


# --- the root itself --------------------------------------------------------


@pytest.mark.parametrize("configured", ["", ".", "relative/uploads"])
def test_a_relative_outbox_is_refused_by_the_configuration(configured: str) -> None:
    """`Path("")` is `Path(".")`, and `realpath(".")` is wherever this started.

    An outbox left blank would quietly make the working directory the allowlist
    — `$HOME` for a shell, and `.ssh` with it. Caught when the settings are
    built, not when a file is sent.
    """
    with pytest.raises(ValidationError, match="absolute"):
        PathsConfig(uploads=Path(configured))


def test_upload_roots_refuses_a_relative_root_it_is_handed(
    outbox: Path, downloads: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second lock, for a Settings built some other way than validation."""
    settings = settings_for(outbox, downloads)
    monkeypatch.setattr(settings.paths, "uploads", Path("relative"), raising=False)
    with pytest.raises(InvalidInput, match="absolute"):
        upload_roots(settings)


def test_an_outbox_other_users_can_write_into_is_refused(outbox: Path, downloads: Path) -> None:
    """The rule assumes the files here were put there by the operator.

    World-writable is refused rather than repaired: no default umask produces
    it, so it is somebody's deliberate `chmod` and silently overruling that is
    not this tool's call.
    """
    write(outbox / "report.pdf")
    outbox.chmod(0o777)
    try:
        with pytest.raises(InsecurePermissions) as refusal:
            resolve_outbound(settings_for(outbox, downloads), "report.pdf")
        assert refusal.value.code is ErrorCode.INSECURE_PERMISSIONS
        assert "chmod" in (refusal.value.suggestion or "")
        # Refused, not quietly re-permissioned.
        assert stat.S_IMODE(outbox.stat().st_mode) == 0o777
    finally:
        outbox.chmod(0o755)


def test_an_outbox_made_under_the_default_umask_is_usable(outbox: Path, downloads: Path) -> None:
    """`umask 002` makes 0775 directories, and it is the common Linux default.

    Wherever user private groups are in use — Ubuntu out of the box, Debian and
    the RHEL family through `USERGROUPS_ENAB` — refusing those would mean the
    tool does not work at all, on a group that has exactly one member. The group
    write bit is taken off instead of being judged — see `_require_private_root`
    for why "is this group safe?" cannot be answered honestly from a process.
    """
    write(outbox / "report.pdf")
    outbox.chmod(0o775)

    resolved = resolve_outbound(settings_for(outbox, downloads), "report.pdf")

    assert resolved.name == "report.pdf"
    # Repaired, and only the write bit: the owner's own r/x and the group's
    # read access are left exactly as they were found.
    assert stat.S_IMODE(outbox.stat().st_mode) == 0o755


def test_a_group_writable_outbox_that_cannot_be_repaired_is_refused(
    outbox: Path, downloads: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrow, *or refuse* — a failed chmod must never fall through to a send.

    Standing in for an outbox this process does not own: `chmod` is the owner's
    privilege, so there the repair is an `EPERM` and the original refusal is
    still the right answer.
    """
    write(outbox / "report.pdf")
    outbox.chmod(0o775)

    def refuse_to_chmod(self: Path, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "chmod", refuse_to_chmod)

    with pytest.raises(InsecurePermissions) as refusal:
        resolve_outbound(settings_for(outbox, downloads), "report.pdf")
    assert refusal.value.code is ErrorCode.INSECURE_PERMISSIONS
    assert "chmod" in (refusal.value.suggestion or "")


def test_a_chmod_that_silently_does_nothing_is_still_a_refusal(
    outbox: Path, downloads: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is read back, not inferred from `chmod` returning.

    Filesystems mounted with fixed permissions — many FUSE and SMB mounts, and
    anything with `mode=`/`dmask=` — accept the call and change nothing. Trusting
    the return value would make this check strict-looking and open, which is the
    worst of both. Raised by review, 2026-08-23.
    """
    write(outbox / "report.pdf")
    outbox.chmod(0o775)

    def chmod_that_does_nothing(self: Path, mode: int) -> None:
        return None

    monkeypatch.setattr(Path, "chmod", chmod_that_does_nothing)

    with pytest.raises(InsecurePermissions, match="still writable"):
        resolve_outbound(settings_for(outbox, downloads), "report.pdf")
    assert stat.S_IMODE(outbox.stat().st_mode) == 0o775


# --- what is opened, and how ------------------------------------------------


def test_a_fifo_is_refused_and_does_not_block(outbox: Path, downloads: Path) -> None:
    """A named pipe would hang `open()` for ever, before any timeout is armed.

    Which is why the descriptor is taken with O_NONBLOCK and the type comes
    from `fstat` on it. If this test hangs, that flag is gone.
    """
    os.mkfifo(outbox / "pipe.bin")
    with pytest.raises(InvalidInput, match="regular file"):
        resolve_outbound(settings_for(outbox, downloads), "pipe.bin")


def test_the_size_is_measured_on_the_descriptor_that_is_hashed(
    outbox: Path, downloads: Path
) -> None:
    """Both facts describe one object, so they cannot disagree about it."""
    write(outbox / "report.pdf")
    resolved = resolve_outbound(settings_for(outbox, downloads), "report.pdf")
    assert resolved.size_bytes == (outbox / "report.pdf").stat().st_size
    assert resolved.sha256 == hashlib.sha256(CONTENT).hexdigest()
