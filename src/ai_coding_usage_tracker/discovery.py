"""Discovery of coding plan subscriptions from local tool configurations."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import paths
from .config import disabled_plans, manual_keys
from .parsing import read_json_dict


@dataclass
class DiscoveredPlan:
    """A coding plan found on this machine, with an API key when available."""

    plan_id: str
    provider: str
    name: str
    region: str | None
    auth_kind: str
    api_key: str | None = None
    api_host: str | None = None
    key_sources: list[str] = field(default_factory=list)


PLAN_LABELS: dict[str, str] = {
    "minimax-cn": "MiniMax Coding Plan (CN)",
    "minimax-intl": "MiniMax Coding Plan (Intl)",
    "glm-intl": "GLM Coding Plan (Z.ai Intl)",
    "claude-code": "Claude Code (Anthropic)",
    "chatgpt-codex": "ChatGPT Codex (OpenAI)",
}

PLAN_ORDER: list[str] = list(PLAN_LABELS)

PROVIDER_NAMES: dict[str, str] = {
    "minimax-cn": "MiniMax",
    "minimax-intl": "MiniMax",
    "glm-intl": "Z.ai",
    "claude-code": "Anthropic",
    "chatgpt-codex": "OpenAI",
}

OPENCODE_PROVIDER_TO_PLAN: dict[str, str] = {
    "zai-coding-plan": "glm-intl",
    "minimax-cn-coding-plan": "minimax-cn",
    "minimax-coding-plan": "minimax-intl",
}

MINIMAX_DEFAULT_HOSTS: dict[str, str] = {
    "minimax-cn": "https://www.minimaxi.com",
    "minimax-intl": "https://www.minimax.io",
}

MINIMAX_HOST_ALIASES: dict[str, str] = {
    "https://api.minimaxi.com": "https://www.minimaxi.com",
    "https://api.minimax.io": "https://www.minimax.io",
}

MINIMAX_TRUSTED_HOSTNAMES: tuple[str, ...] = ("minimaxi.com", "minimax.io")


def _is_trusted_minimax_url(value: str) -> bool:
    """True when value is an https URL whose hostname is owned by MiniMax."""
    try:
        parts = urlsplit(value)
        hostname = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme != "https" or not hostname:
        return False
    return hostname in MINIMAX_TRUSTED_HOSTNAMES or hostname.endswith(
        (".minimaxi.com", ".minimax.io")
    )


def sanitize_minimax_host(value: str, default: str) -> str:
    """Constrain a host from user/env configuration to MiniMax-owned URLs.

    Hosts reach us from files an attacker-influenced process may write
    (~/.codex/config.toml, ~/.claude/settings*.json, plantrack config), so
    anything that is not a known alias or an https URL on a MiniMax domain
    is replaced with `default` before an API key is ever sent to it.
    """
    if value in MINIMAX_HOST_ALIASES:
        return MINIMAX_HOST_ALIASES[value]
    if _is_trusted_minimax_url(value):
        return value
    return default


ZAI_TRUSTED_HOSTNAMES: tuple[str, ...] = ("z.ai",)


def is_zai_base_url(value: str) -> bool:
    """True when an ANTHROPIC_BASE_URL points at a Z.ai-owned https endpoint."""
    try:
        parts = urlsplit(value)
        hostname = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme != "https" or not hostname:
        return False
    return hostname in ZAI_TRUSTED_HOSTNAMES or hostname.endswith(".z.ai")


def _read_toml(path: Path) -> dict | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _find_minimax_intl_key(home: Path, plan: DiscoveredPlan) -> None:
    config = _read_toml(paths.codex_dir(home) / "config.toml")
    if not config:
        return
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        env = server.get("env")
        if not isinstance(env, dict):
            continue
        key = env.get("MINIMAX_API_KEY")
        host = env.get("MINIMAX_API_HOST")
        if not isinstance(key, str) or not key:
            continue
        host = sanitize_minimax_host(str(host), MINIMAX_DEFAULT_HOSTS["minimax-intl"])
        plan.api_key = key
        plan.api_host = host
        plan.key_sources.append(f"~/.codex/config.toml [{name}]")
        return


def _find_claude_settings_key(
    home: Path,
    filename: str,
    plan: DiscoveredPlan,
    default_host: str | None = None,
    trusted_base_url: Callable[[str], bool] | None = None,
) -> None:
    """Attach a key from a Claude settings file.

    `default_host` only applies to plans whose quota API host can be derived
    from the base URL (MiniMax). `trusted_base_url` guards the plans whose
    quota endpoint is instead a fixed URL (GLM): the filename alone would
    otherwise decide where the token is sent, so a settings file that declares
    a base URL for some *other* Anthropic-compatible provider holds that
    provider's key, and it is skipped rather than handed to Z.ai. A file with
    no base URL declared is the common case and still matches on filename.
    """
    settings = read_json_dict(paths.claude_dir(home) / filename)
    if not settings:
        return
    env = settings.get("env")
    if not isinstance(env, dict):
        return
    key = env.get("ANTHROPIC_AUTH_TOKEN")
    base = env.get("ANTHROPIC_BASE_URL")
    if not isinstance(key, str) or not key:
        return
    declared = base.strip() if isinstance(base, str) else ""
    if trusted_base_url is not None and declared and not trusted_base_url(declared):
        return
    plan.api_key = key
    if isinstance(base, str) and default_host is not None:
        plan.api_host = sanitize_minimax_host(base, default_host)
    plan.key_sources.append(f"~/.claude/{filename}")


def discover_plans(home: Path | None = None) -> list[DiscoveredPlan]:
    """Scan local tool configs and return every coding plan that can be tracked."""
    home = home or paths.default_home()
    discovered: dict[str, DiscoveredPlan] = {}

    def ensure(plan_id: str, auth_kind: str) -> DiscoveredPlan:
        if plan_id not in discovered:
            discovered[plan_id] = DiscoveredPlan(
                plan_id=plan_id,
                provider=PROVIDER_NAMES[plan_id],
                name=PLAN_LABELS[plan_id],
                region="CN" if plan_id.endswith("-cn") else ("Intl" if "intl" in plan_id else None),
                auth_kind=auth_kind,
            )
        return discovered[plan_id]

    # Manually added keys win over anything discovered on disk: the user
    # registered them explicitly via `plantrack plan add`.
    for plan_id, entry in manual_keys(home).items():
        if plan_id not in PLAN_LABELS or not isinstance(entry, dict):
            continue
        key = entry.get("api_key")
        if not isinstance(key, str) or not key:
            continue
        plan = ensure(plan_id, "api-key")
        if plan.api_key:
            continue
        plan.api_key = key
        host = entry.get("api_host")
        default_host = MINIMAX_DEFAULT_HOSTS.get(plan_id)
        if isinstance(host, str) and host and default_host is not None:
            # Only MiniMax plans use a configurable quota host; a hostile or
            # non-MiniMax value falls back to the plan's default host.
            plan.api_host = sanitize_minimax_host(host, default_host)
        plan.key_sources.append("plantrack config")

    auth = read_json_dict(paths.opencode_auth_file(home))
    if auth:
        for provider_id, plan_id in OPENCODE_PROVIDER_TO_PLAN.items():
            entry = auth.get(provider_id)
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                plan = ensure(plan_id, "api-key")
                if not plan.api_key:
                    plan.api_key = entry["key"]
                    plan.key_sources.append("~/.local/share/opencode/auth.json")

    minimax_cn = ensure("minimax-cn", "api-key")
    minimax_cn.api_host = minimax_cn.api_host or MINIMAX_DEFAULT_HOSTS["minimax-cn"]
    if not minimax_cn.key_sources:
        _find_claude_settings_key(
            home, "settings-mx-cn.json", minimax_cn, MINIMAX_DEFAULT_HOSTS["minimax-cn"]
        )

    minimax_intl = ensure("minimax-intl", "api-key")
    minimax_intl.api_host = minimax_intl.api_host or MINIMAX_DEFAULT_HOSTS["minimax-intl"]
    if not minimax_intl.key_sources:
        _find_minimax_intl_key(home, minimax_intl)

    glm = ensure("glm-intl", "api-key")
    if not glm.key_sources:
        # GLM's quota API lives at a fixed Z.ai URL, so the base URL declared in
        # the settings file cannot redirect the request - it can only tell us
        # the token in that file is not a Z.ai one, in which case we skip it.
        _find_claude_settings_key(
            home, "settings-glm.json", glm, trusted_base_url=is_zai_base_url
        )

    claude_plan = ensure("claude-code", "oauth")
    credentials = read_json_dict(paths.claude_dir(home) / ".credentials.json")
    if credentials and isinstance(credentials.get("claudeAiOauth"), dict):
        claude_plan.key_sources.append("~/.claude/.credentials.json")

    codex_plan = ensure("chatgpt-codex", "oauth")
    codex_auth = read_json_dict(paths.codex_dir(home) / "auth.json")
    if codex_auth and codex_auth.get("auth_mode") == "chatgpt":
        codex_plan.key_sources.append("~/.codex/auth.json")

    disabled = disabled_plans(home)
    return [
        discovered[plan_id]
        for plan_id in PLAN_ORDER
        if plan_id not in disabled and discovered[plan_id].key_sources
    ]
