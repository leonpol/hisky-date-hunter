"""Persistent deduplication state so monthly runs only alert on new/restocked finds."""

from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from .matcher import MatchFinding, normalize_url


class SeenStateStore:
    """Manages state/seen_matches.json across monthly GitHub Actions runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.seen: dict[str, dict[str, str]] = {}
        self.last_run_utc: str = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.seen = dict(raw.get("seen", {}))
                self.last_run_utc = str(raw.get("last_run_utc", ""))
        except (OSError, json.JSONDecodeError):
            self.seen = {}

    def filter_new(
        self,
        findings: Iterable[MatchFinding],
        *,
        active_direct_urls: set[str] | None = None,
        active_target_dates: set[str] | None = None,
    ) -> list[MatchFinding]:
        """
        Return findings not yet alerted, and prune watched direct URLs that went out of stock
        (or entries from another user's target_dates after a fork) so restocks re-alert cleanly.
        """
        if active_target_dates is not None:
            stale_dates = [
                u for u, meta in self.seen.items()
                if meta.get("target_date") and meta.get("target_date") not in active_target_dates
            ]
            for u in stale_dates:
                self.seen.pop(u, None)

        if active_direct_urls is not None:
            # Any previously-seen direct URL that was checked and is NO LONGER in stock gets pruned
            to_prune = [
                u for u, meta in self.seen.items()
                if meta.get("source_layer") == "Direct Check" and u not in active_direct_urls
            ]
            for u in to_prune:
                self.seen.pop(u, None)

        new_items: list[MatchFinding] = []
        seen_in_batch: set[str] = set()
        for item in findings:
            norm = normalize_url(item.url)
            if norm in seen_in_batch:
                continue
            seen_in_batch.add(norm)
            if norm not in self.seen:
                new_items.append(item)
        return new_items

    def mark_seen(self, findings: Iterable[MatchFinding]) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.last_run_utc = now
        for item in findings:
            norm = normalize_url(item.url)
            self.seen[norm] = {
                "title": item.title,
                "target_date": item.target_date,
                "price": item.price,
                "source_layer": item.source_layer,
                "first_seen_utc": self.seen.get(norm, {}).get("first_seen_utc", now),
                "last_seen_utc": now,
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_run_utc": self.last_run_utc
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seen": dict(sorted(self.seen.items())),
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
