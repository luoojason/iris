"""Static supply-chain advisory check: flag known-compromised dependency versions.

Model-free and network-free, like the rest of ``iris audit``. A small frozen
catalog of known-bad releases is matched against what is actually installed (pip,
via importlib.metadata) and against the pinned ``npx``/``uvx`` packages the owner
wired as MCP servers (``iris mcp add``). Silent unless a compromised version is
genuinely present, so it is safe to run on every audit / CI tripwire. A live
OSV.dev network scan is intentionally a separate opt-in (owner decision), not this.

Hermes ships a security-advisories surface; Iris had none despite loading pip deps
and letting the owner point the agent at arbitrary pinned npx/uvx MCP servers. This
closes that gap with a stdlib-only, deterministic check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Advisory:
    id: str
    ecosystem: str  # "pip" | "npm"
    package: str
    bad_versions: tuple  # exact version strings known to be compromised
    severity: str  # "critical" | "high" | ...
    note: str


@dataclass(frozen=True)
class Component:
    ecosystem: str  # "pip" | "npm"
    package: str
    version: Optional[str]


# Seeded with real 2026 supply-chain incidents; extend as new advisories land.
# Versions are exact: an attack publishes a specific malicious release, and the
# fix is to move OFF that version, so matching the exact string avoids guessing a
# range and never false-flags a patched install.
ADVISORIES: tuple = (
    Advisory("shai-hulud-2026-05", "npm", "@ctrl/tinycolor", ("4.1.1", "4.1.2"),
             "critical", "shai-hulud self-propagating npm worm (2026-05): steals env/credentials"),
    Advisory("shai-hulud-2026-05-ngx", "npm", "ngx-bootstrap", ("18.1.4", "19.0.3", "20.0.4"),
             "critical", "shai-hulud-compromised release"),
    Advisory("mistralai-2.4.6", "pip", "mistralai", ("2.4.6",),
             "critical", "compromised release pulled by upstream"),
)


def installed_pip_versions() -> dict:
    """Map of installed pip distribution name (lowercased) -> version string."""
    try:
        import importlib.metadata as md
    except Exception:  # pragma: no cover - importlib.metadata is stdlib on 3.10+
        return {}
    out: dict = {}
    for dist in md.distributions():
        try:
            name = (dist.metadata["Name"] or "").strip()
            if name:
                out[name.lower()] = dist.version
        except Exception:
            continue
    return out


def _split_npm(spec: str) -> tuple:
    """('@scope/name', '1.0.0') etc. Returns (package, version|None)."""
    if spec.startswith("@"):  # scoped package: the version '@' is the SECOND one
        idx = spec.find("@", 1)
        if idx == -1:
            return spec, None
        return spec[:idx], (spec[idx + 1:] or None)
    pkg, sep, ver = spec.partition("@")
    return pkg, (ver or None) if sep else None


def parse_mcp_component(command: str, args) -> Optional[Component]:
    """Turn an MCP server launch into a pinned Component, or None if not npx/uvx.

    Recognizes ``npx [-y] pkg[@ver]`` (npm) and ``uvx pkg[==ver]`` (pip). Flags
    (leading ``-``) are skipped; the first non-flag token is the package spec.
    """
    argv = ([command] if command else []) + [str(a) for a in (args or [])]
    if not argv:
        return None
    exe = os.path.basename(argv[0])
    rest = [a for a in argv[1:] if not a.startswith("-")]
    if not rest:
        return None
    spec = rest[0]
    if exe == "npx":
        pkg, ver = _split_npm(spec)
        return Component("npm", pkg, ver)
    if exe == "uvx":
        pkg, sep, ver = spec.partition("==")
        return Component("pip", pkg, (ver or None) if sep else None)
    return None


def scan(pip_versions: dict, components, advisories=ADVISORIES) -> list:
    """Return [(Advisory, found_version)] for every compromised version present.

    Matches installed pip versions and pinned MCP components against the catalog.
    An unpinned component (no version) is skipped: ``npx pkg`` pulls latest, which
    we cannot resolve offline, so we never guess.
    """
    matches: list = []
    pip_versions = {k.lower(): v for k, v in (pip_versions or {}).items()}
    for adv in advisories:
        if adv.ecosystem == "pip":
            have = pip_versions.get(adv.package.lower())
            if have is not None and have in adv.bad_versions:
                matches.append((adv, have))
        for comp in (components or ()):
            if (comp is not None and comp.ecosystem == adv.ecosystem
                    and comp.package == adv.package and comp.version is not None
                    and comp.version in adv.bad_versions):
                matches.append((adv, comp.version))
    return matches
