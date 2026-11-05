"""Staged, all-or-nothing replacement of a text file, and retry-safe identification of a write that already landed.

Conversation histories and summary files used to be rewritten in place (open(path, 'w') then write). A failure
during the write left a truncated file (earlier memory destroyed; a retry then read and extended the fragment),
and a rollover whose write failed left an empty newest summary file (which every later prompt then read as the
NPC's whole memory). Here the complete new content is written to a temporary file next to the target, flushed and
fsynced, and only then moved over the target with os.replace: a reader sees either the old file or the complete
new one, never a fragment, whatever fails in write / flush / fsync / close / rename.

os.replace itself can fail ambiguously (it raised, but the rename did happen), and an exception after a
successful replace looks the same to the caller. PublicationAttempt records the digest of the exact content an
attempt tried to publish, so a retry can tell from the file itself whether that content is already there,
instead of trusting an in-memory "written" flag that is only set when no exception occurred.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Callable

TEMP_SUFFIX = ".tmp"


def text_digest(text: str, encoding: str = "utf-8") -> str:
    """sha256 of text with newlines normalised: the file is written in text mode (\\r\\n on Windows) and read
    back with universal newlines, so only the newline-normalised content identifies it on every platform."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode(encoding, errors="surrogatepass")).hexdigest()


def temp_path_for(path: str) -> str:
    """A unique sibling of path. It starts with '.', keeps the target's name (so it is recognisable) and ends in
    .tmp, so it never matches the '*.txt' scan for the latest summary file nor a '<name>.json' history path."""
    directory, name = os.path.split(path)
    return os.path.join(directory, f".{name}.{uuid.uuid4().hex[:12]}{TEMP_SUFFIX}")


def write_text_atomically(path: str, text: str, open_fn: Callable = open, encoding: str = "utf-8") -> None:
    """Replace path with exactly text, or leave it as it was and raise.

    open_fn is the caller module's open, so that module's file handling (and test fault injection on it) applies.
    The temporary file is removed on failure (best effort: a leftover .tmp is never read)."""
    temp_path = temp_path_for(path)
    try:
        f = open_fn(temp_path, "w", encoding=encoding)
        try:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        finally:
            f.close()
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def file_digest(path: str, open_fn: Callable = open, encoding: str = "utf-8") -> str | None:
    """text_digest of path's content, or None when it does not exist / cannot be read or decoded."""
    try:
        with open_fn(path, "r", encoding=encoding) as f:
            return text_digest(f.read(), encoding)
    except (OSError, UnicodeDecodeError):
        return None


class PublicationAttempt:
    """One record of a final save that may be retried (a conversation's history entry, a summary, a share).

    registry (FinalSaveProgress.publications) outlives a failed attempt: before the staged file replaces the
    target, the digest of the full new content is stored under key. A retry asks is_committed(path): True means the
    earlier attempt's content is the file's current content - the replace landed even though the attempt raised -
    so the record must not be written again."""

    def __init__(self, registry: dict, key: object, encoding: str = "utf-8") -> None:
        self.__registry = registry
        self.__key = key
        self.__encoding = encoding

    def is_committed(self, path: str, open_fn: Callable = open) -> bool:
        expected = self.__registry.get(self.__key)
        return expected is not None and file_digest(path, open_fn, self.__encoding) == expected

    def publish(self, path: str, text: str, open_fn: Callable = open) -> None:
        self.__registry[self.__key] = text_digest(text, self.__encoding)
        write_text_atomically(path, text, open_fn, self.__encoding)
