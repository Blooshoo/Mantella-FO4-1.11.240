"""Configuration for the optional OpenClaw companion adapter.

Design rules (see src/llm/openclaw/README.md):

* Disabled unless a config file explicitly enables it.
* Opt-in is per character name: only listed characters ever reach OpenClaw.
  Every other NPC keeps the normal direct Mantella provider.
* No secrets live in the config file. The bearer token, if any, is read at
  request time from the environment variable *named* by ``token_env``.
* The default transport is ``mock`` (offline). The ``gateway`` transport is
  loopback-only unless ``allow_non_loopback`` is set deliberately.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

ENV_CONFIG_PATH = "MANTELLA_OPENCLAW_CONFIG"
DEFAULT_CONFIG_FILENAME = "openclaw_companion.json"

# Namespaces the Gateway reserves for internal routing; application keys must avoid them
# (docs/gateway/openai-http-api.md, "x-openclaw-session-key").
RESERVED_SESSION_PREFIXES = ("subagent:", "cron:", "acp:")
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
VALID_TRANSPORTS = ("mock", "gateway")
VALID_SESSION_SCOPES = ("conversation", "character")


class OpenClawConfigError(ValueError):
    pass


@dataclass(frozen=True)
class OpenClawCompanionConfig:
    enabled: bool = False
    transport: str = "mock"
    base_url: str = "http://127.0.0.1:18789/v1"
    agent_id: str = ""
    token_env: str = "OPENCLAW_MANTELLA_TOKEN"
    characters: tuple[str, ...] = ()
    session_scope: str = "conversation"
    session_namespace: str = "mantella-fo4"
    message_channel: str = "mantella"
    allow_non_loopback: bool = False
    fallback_to_direct: bool = True
    connect_timeout_s: float = 5.0
    first_token_timeout_s: float = 45.0
    read_timeout_s: float = 60.0
    max_retries: int = 2
    retry_backoff_s: float = 0.5
    max_retry_after_s: float = 5.0
    # Owner attestation (B05): set true ONLY after verifying that the Gateway never persists a turn
    # before answering 429/503. Default false: busy retries go to a fresh session epoch
    # (conversation scope) or are not retried at all (character scope, whose key cannot rotate).
    busy_status_is_pre_acceptance: bool = False
    recap_messages: int = 12
    max_message_chars: int = 1200
    max_context_chars: int = 6000
    max_reply_chars: int = 4000
    max_tokens: int = 300
    mock_reply: str = "I'm right here with you."

    # ------------------------------------------------------------------
    def routes_character(self, name: str | None) -> bool:
        if not self.enabled or not name:
            return False
        wanted = _norm_name(name)
        return any(_norm_name(c) == wanted for c in self.characters)

    @property
    def model(self) -> str:
        return f"openclaw/{self.agent_id or 'mock'}"

    def resolve_token(self) -> str | None:
        """Read the token at call time from the named environment variable.

        The value is never stored on this object, logged, or written anywhere.
        """
        value = os.environ.get(self.token_env, "")
        return value or None

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OpenClawCompanionConfig":
        if not isinstance(raw, dict):
            raise OpenClawConfigError("config root must be an object")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - known - {"$comment", "_comment"})
        if unknown:
            raise OpenClawConfigError(f"unknown config keys: {unknown}")
        for secretish in ("token", "api_key", "password", "authorization"):
            if secretish in raw:
                raise OpenClawConfigError(
                    f"'{secretish}' must not be stored in the config file; use token_env")
        data = {k: v for k, v in raw.items() if k in known}
        if "characters" in data:
            chars = data["characters"]
            if not isinstance(chars, list) or not all(isinstance(c, str) and c.strip() for c in chars):
                raise OpenClawConfigError("characters must be a list of non-empty names")
            data["characters"] = tuple(c.strip() for c in chars)
        cfg = cls(**data)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.transport not in VALID_TRANSPORTS:
            raise OpenClawConfigError(f"transport must be one of {VALID_TRANSPORTS}")
        if self.session_scope not in VALID_SESSION_SCOPES:
            raise OpenClawConfigError(f"session_scope must be one of {VALID_SESSION_SCOPES}")
        if not _ENV_NAME_RE.match(self.token_env):
            raise OpenClawConfigError("token_env must be an environment variable NAME, not a value")
        if not re.match(r"^[a-z0-9][a-z0-9._-]{0,31}$", self.session_namespace) or \
                self.session_namespace.startswith(tuple(p.rstrip(":") for p in RESERVED_SESSION_PREFIXES)):
            raise OpenClawConfigError("session_namespace invalid or reserved")
        if self.enabled:
            if not self.characters:
                raise OpenClawConfigError("enabled requires an explicit characters allowlist")
            if self.transport == "gateway":
                if not _AGENT_ID_RE.match(self.agent_id or ""):
                    raise OpenClawConfigError(
                        "gateway transport requires a dedicated agent_id (lowercase id)")
                if self.agent_id in ("main", "default"):
                    raise OpenClawConfigError(
                        "refusing to route NPC dialogue to the main/default agent; "
                        "create a dedicated tool-restricted companion agent")
                _check_url(self.base_url, self.allow_non_loopback)
        if not isinstance(self.busy_status_is_pre_acceptance, bool):
            raise OpenClawConfigError("busy_status_is_pre_acceptance must be true or false")
        for name in ("max_retries", "recap_messages", "max_message_chars", "max_context_chars",
                     "max_reply_chars", "max_tokens"):
            if int(getattr(self, name)) < 0:
                raise OpenClawConfigError(f"{name} must be >= 0")


def _norm_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _check_url(url: str, allow_non_loopback: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise OpenClawConfigError("base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise OpenClawConfigError("base_url must not embed credentials")
    if parsed.query or parsed.fragment:
        raise OpenClawConfigError("base_url must not carry a query string or fragment")
    if allow_non_loopback:
        return
    host = parsed.hostname
    if host == "localhost":
        return
    try:
        if ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise OpenClawConfigError(
        "base_url is not loopback; set allow_non_loopback only for a deliberately scoped private ingress")


def load_config(path: str | None = None, search_dirs: list[str] | None = None) -> OpenClawCompanionConfig:
    """Load config from an explicit path, $MANTELLA_OPENCLAW_CONFIG, or search dirs.

    Missing file -> disabled config (the adapter is opt-in).
    A present-but-invalid file raises, so a typo never silently routes dialogue.
    """
    candidates: list[str] = []
    if path:
        candidates.append(path)
    elif os.environ.get(ENV_CONFIG_PATH):
        candidates.append(os.environ[ENV_CONFIG_PATH])
    else:
        for d in search_dirs or []:
            if d:
                candidates.append(os.path.join(d, DEFAULT_CONFIG_FILENAME))
    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                return OpenClawCompanionConfig.from_dict(json.load(f))
        if path or os.environ.get(ENV_CONFIG_PATH):
            raise OpenClawConfigError(f"OpenClaw companion config not found: {candidate}")
    return OpenClawCompanionConfig()
