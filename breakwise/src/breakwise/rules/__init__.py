"""Built-in migration rules for big upgrades whose changelogs just say "see the migration guide".

Each rule is a synthetic changelog entry with a known fix. Rules are applied when an upgrade
crosses the rule's version, e.g. pydantic 1.10 -> 2.x crosses 2.0.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

from .. import versions
from ..models import ChangeEntry, ChangeKind, Ecosystem


@lru_cache(maxsize=None)
def _load(ecosystem: str) -> list[dict]:
    try:
        text = resources.files(__package__).joinpath(f"{ecosystem}.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return json.loads(text)


def entries_for(package: str, ecosystem: Ecosystem, current: str, target: str) -> list[ChangeEntry]:
    out = []
    for rule in _load(ecosystem.value):
        if rule["package"].lower() != package.lower():
            continue
        if not versions.in_range(rule["version"], current, target, ecosystem):
            continue
        out.append(
            ChangeEntry(
                version=rule["version"],
                text=rule["text"],
                kind=ChangeKind(rule["kind"]),
                section="Migration guide",
                symbols=list(rule["symbols"]),
                fix=rule["fix"],
                source="rules",
            )
        )
    return out
