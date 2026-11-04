"""Stable identity, per-character/per-conversation routing and bounded state.

OpenClaw sessions are stateful (the Gateway keeps the agent transcript per
session key), while Mantella re-sends its whole message thread every turn.
To integrate organically we send only the *new* part of the thread when the
previous turn's transcript is a prefix of the current one, and a bounded recap
(scene context + last N lines) whenever we cannot prove continuity: first turn,
after an error/cancel, or after Mantella rewrote/summarised its thread.
"""
from __future__ import annotations

import hashlib
import re
import threading
import uuid
from dataclasses import dataclass, field

from src.llm.openclaw.config import OpenClawCompanionConfig, RESERVED_SESSION_PREFIXES
from src.llm.openclaw.trust import TranscriptLine

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(value: str, limit: int = 32) -> str:
    s = _SLUG_RE.sub("-", (value or "").casefold()).strip("-")
    return (s or "unknown")[:limit]


def build_session_key(cfg: OpenClawCompanionConfig, character_name: str, character_ref: str | None,
                      conversation_id: str) -> str:
    """Application-owned session key.

    Format: ``<namespace>:<agent>:<character-slug>-<ref-hash>[:<conversation>]``.
    ``character`` scope keeps one durable session per character; ``conversation``
    scope (default) starts a fresh session each Mantella conversation.
    """
    ref_hash = hashlib.sha256(f"{character_name}|{character_ref or ''}".encode()).hexdigest()[:10]
    parts = [cfg.session_namespace, slug(cfg.agent_id or "mock"), f"{slug(character_name)}-{ref_hash}"]
    if cfg.session_scope == "conversation":
        parts.append(slug(conversation_id, 40))
    key = ":".join(parts)
    if key.startswith(RESERVED_SESSION_PREFIXES):  # defensive; validate() already blocks this
        raise ValueError("session key uses a reserved namespace")
    return key


def _line_digest(line: TranscriptLine) -> str:
    return hashlib.sha256(f"{line.speaker}\x00{line.text}".encode()).hexdigest()[:16]


@dataclass
class TurnPlan:
    resync: bool
    lines: list[TranscriptLine]
    include_context: bool
    sent_digests: list[str]


@dataclass
class ConversationState:
    """Bounded per-conversation bookkeeping. Holds digests, never raw dialogue."""
    session_key: str
    conversation_id: str
    max_digests: int = 512
    acknowledged: list[str] = field(default_factory=list)
    context_digest: str | None = None
    needs_resync: bool = True
    turns: int = 0
    epoch: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self._base_key = self.session_key

    def rotate_epoch(self) -> str:
        """Move to a fresh remote session after an ambiguous failure.

        The Gateway may already hold the failed turn in the old transcript; nothing can be
        rolled back there, and replaying into it would duplicate the player's turn. The next
        turn therefore goes to ``<key>:r<n>`` with a bounded recap (needs_resync)."""
        with self._lock:
            self.epoch += 1
            self.session_key = f"{self._base_key}:r{self.epoch}"
            self.needs_resync = True
            self.acknowledged = []
            self.context_digest = None
            return self.session_key

    def plan(self, scene_context: str, lines: list[TranscriptLine], recap_messages: int,
             self_speaker: str) -> TurnPlan:
        """Decide what to send for this turn.

        Continuity holds when every line acknowledged last turn is still a prefix of the
        thread. Lines spoken by the routed character directly after that prefix are the
        agent's own previous reply as Mantella recorded it (possibly trimmed by Mantella's
        sentence limits); the OpenClaw session already has them, so they are skipped.
        """
        digests = [_line_digest(l) for l in lines]
        ctx_digest = hashlib.sha256(scene_context.encode()).hexdigest()[:16] if scene_context else None
        me = " ".join(self_speaker.casefold().split())
        with self._lock:
            ack = self.acknowledged
            if not self.needs_resync and len(ack) <= len(digests) and digests[: len(ack)] == ack:
                i = len(ack)
                while i < len(lines) and " ".join(lines[i].speaker.casefold().split()) == me:
                    i += 1
                new_lines = lines[i:]
                if new_lines:
                    return TurnPlan(False, new_lines, ctx_digest != self.context_digest, digests)
            tail = lines[-recap_messages:] if recap_messages else lines[-1:]
            return TurnPlan(True, tail, True, digests)

    def commit(self, plan: TurnPlan, scene_context: str) -> None:
        """Record a successful (fully streamed) turn."""
        with self._lock:
            digests = list(plan.sent_digests)
            # Window overflow: continuity can no longer be proven cheaply -> resync next turn.
            self.needs_resync = len(digests) > self.max_digests
            self.acknowledged = digests[-self.max_digests:]
            self.context_digest = (hashlib.sha256(scene_context.encode()).hexdigest()[:16]
                                   if scene_context else None)
            self.turns += 1

    def invalidate(self) -> None:
        with self._lock:
            self.needs_resync = True


def new_conversation_id() -> str:
    return uuid.uuid4().hex[:16]
