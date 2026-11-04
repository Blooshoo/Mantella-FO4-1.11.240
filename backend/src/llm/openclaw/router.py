"""Decides, per character, whether a turn goes to OpenClaw or stays on Mantella's provider.

Only characters on the explicit allowlist are ever routed. Clients live for one
Mantella conversation (``begin_conversation`` is called whenever ChatManager
clears its per-character client cache at conversation start), which gives every
conversation its own OpenClaw session key in the default ``conversation`` scope.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from src.llm.openclaw.client import OpenClawCompanionClient
from src.llm.openclaw.config import OpenClawCompanionConfig, load_config
from src.llm.openclaw.session import new_conversation_id
from src.llm.openclaw.transport import GatewayChatCompletionsTransport, MockTransport, Transport

logger = logging.getLogger("mantella.openclaw")


def default_transport_factory(cfg: OpenClawCompanionConfig) -> Transport:
    if cfg.transport == "gateway":
        return GatewayChatCompletionsTransport(cfg)
    return MockTransport(reply_chunks=[cfg.mock_reply], token_supplier=cfg.resolve_token)


class OpenClawRouter:
    def __init__(self, cfg: OpenClawCompanionConfig,
                 transport_factory: Callable[[OpenClawCompanionConfig], Transport] = default_transport_factory):
        self.cfg = cfg
        self._factory = transport_factory
        self._transport: Transport | None = None
        self._clients: dict[str, OpenClawCompanionClient] = {}
        self.conversation_id = new_conversation_id()
        if cfg.enabled:
            logger.info("OpenClaw companion route enabled for %s via %s transport (agent '%s')",
                        list(cfg.characters), cfg.transport, cfg.agent_id or "mock")

    @classmethod
    def from_search_dirs(cls, dirs: list[str]) -> "OpenClawRouter":
        return cls(load_config(search_dirs=dirs))

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def begin_conversation(self) -> None:
        self._clients.clear()
        self.conversation_id = new_conversation_id()

    def client_for(self, character: Any, fallback: Any) -> Any | None:
        """Return an OpenClaw client for an allowlisted character, else None."""
        name = getattr(character, "name", None)
        if not self.cfg.routes_character(name):
            return None
        ref = str(getattr(character, "ref_id", "") or "")
        key = f"{name}|{ref}"
        client = self._clients.get(key)
        if client is None:
            if self._transport is None:
                self._transport = self._factory(self.cfg)
            client = OpenClawCompanionClient(self.cfg, self._transport, fallback, name, ref,
                                             self.conversation_id)
            self._clients[key] = client
        return client
