# -*- coding: utf-8 -*-
"""Bounded, observable acquisition for model-selected context resources.

The cognitive model chooses *which* resources could change an answer.  This
broker performs only the choices that survived code-owned policy, isolates
provider failures, gathers independent evidence concurrently, and reports
metadata without logging private payloads.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
import time
from typing import Callable, Iterable, Mapping, Tuple

from resource_pool import ResourceRequest


@dataclass(frozen=True)
class EvidenceItem:
    name: str
    required: bool
    ok: bool
    text: str = ""
    error: str = ""
    elapsed_ms: int = 0


@dataclass(frozen=True)
class EvidenceBundle:
    items: Tuple[EvidenceItem, ...]

    @property
    def missing_required(self) -> Tuple[EvidenceItem, ...]:
        return tuple(item for item in self.items if item.required and not item.ok)

    def render(self) -> str:
        blocks = [item.text.strip() for item in self.items
                  if item.ok and item.text.strip()]
        missing = self.missing_required
        if missing:
            names = ", ".join(item.name for item in missing)
            blocks.append(
                "--- REQUIRED RESOURCE UNAVAILABLE ---\n"
                f"Could not acquire: {names}. Do not claim an evidence-based "
                "result from those resources. Use a valid alternative if one "
                "exists; otherwise report the concrete blocker.")
        return "\n\n".join(blocks)

    def audit_line(self) -> str:
        statuses = [
            f"{item.name}={'ok' if item.ok else 'failed'}:{item.elapsed_ms}ms"
            for item in self.items
        ]
        return "[Resource Broker] " + (", ".join(statuses) or "no providers")


def collect(requests: Iterable[ResourceRequest],
            providers: Mapping[str, Callable[[], str]], *,
            timeout_sec: float = 3.0) -> EvidenceBundle:
    """Acquire independent resources concurrently with a bounded wait.

    The result order follows ``requests`` rather than completion order so the
    prompt remains deterministic.  No provider payload is printed here.
    """
    selected = tuple(request for request in (requests or ())
                     if request.name in providers)
    if not selected:
        return EvidenceBundle(())

    started = {}
    executor = ThreadPoolExecutor(
        max_workers=min(6, len(selected)), thread_name_prefix="vanta-resource")
    futures = {}
    for request in selected:
        started[request.name] = time.monotonic()
        futures[request.name] = executor.submit(providers[request.name])

    items = []
    deadline = time.monotonic() + max(0.1, float(timeout_sec))
    try:
        for request in selected:
            future = futures[request.name]
            remaining = max(0.0, deadline - time.monotonic())
            try:
                value = future.result(timeout=remaining)
                text = str(value or "").strip()
                if request.required and not text:
                    items.append(EvidenceItem(
                        request.name, True, False,
                        error="provider returned no evidence",
                        elapsed_ms=int((time.monotonic() - started[request.name]) * 1000)))
                else:
                    items.append(EvidenceItem(
                        request.name, request.required, True, text=text,
                        elapsed_ms=int((time.monotonic() - started[request.name]) * 1000)))
            except TimeoutError:
                future.cancel()
                items.append(EvidenceItem(
                    request.name, request.required, False,
                    error="provider timeout",
                    elapsed_ms=int((time.monotonic() - started[request.name]) * 1000)))
            except Exception as exc:
                items.append(EvidenceItem(
                    request.name, request.required, False,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.monotonic() - started[request.name]) * 1000)))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return EvidenceBundle(tuple(items))
