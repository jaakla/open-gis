"""Source pin classes and credential hygiene for ``sources.*``.

A source is reproducible only if the bytes it contributed can be obtained
again. ``version.identifier`` answers that for a published release or a
STAC item; it does not for a warehouse table, whose "version" is a moving
target unless something freezes it. This module recognises two pin classes
that do freeze it, and reports honestly when neither holds.

.. code-block:: yaml

    sources:
      parcels:
        access:
          method: postgis
          connection: {ref: "env:PARCELS_DSN"}      # credentials by reference only
        warehouse:
          backend: postgis                          # duckdb | postgis (pilot)
          account: geo-prod                         # host/project identity, no secrets
          database: gis
          schema: cadastre
          table: parcels
          query_sha256: "sha256:..."                # digest of the exact SELECT
          schema_sha256: "sha256:..."               # digest of the discovered columns
        pin:
          class: local_snapshot                     # (1) user-approved local copy
          path: data/source/parcels.parquet
          sha256: "sha256:..."
          captured_at: "2026-08-30T10:00:00Z"
        # -- or --
        pin:
          class: backend_snapshot                   # (2) backend time travel / snapshot
          identifier: "pg_export_snapshot:00000003-000001A8-1"
          captured_at: "2026-08-30T10:00:00Z"
          retention_until: "2026-12-31T00:00:00Z"   # when the backend may drop it
          verification: {at: "2026-08-30T10:05:00Z", status: accessible}

``assess_pin`` returns one of:

- ``pinned``: the pin class validates (local bytes match, or the backend
  snapshot is identified, unexpired, and not known to be inaccessible);
- ``not_reproducible``: a pin is declared but cannot deliver the bytes again
  (missing or changed snapshot file, expired or inaccessible backend snapshot);
- ``unpinned``: no pin and no usable version identity, or a mutable alias
  such as ``latest``;
- ``invalid``: the pin block is malformed.

A source without a ``pin`` block keeps the original rule: a non-``latest``
``version.identifier`` or ``published_at`` counts as pinned. That rule is
adequate for immutable published releases and is all a file download needs.

Secrets never belong in ``project.yaml``. ``find_inline_credentials`` walks
a source block for password fragments, credentialed URLs, and well-known key
shapes, and ``connection_reference_error`` requires ``access.connection`` to
be a reference (``env:``, ``file:``, ``service:``, ``keyring:``) rather than
a DSN.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .integrity import normalize_digest, sha256_file
from .project import get_in, project_path

PIN_CLASSES = ("local_snapshot", "backend_snapshot")
MUTABLE_ALIASES = {"latest", "current", "head", "now", "master", "main", "live", "today"}
CONNECTION_REFERENCE_SCHEMES = ("env", "file", "service", "keyring")

_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("password fragment", re.compile(r"(?i)(?:^|[\s;&?,{\"'])(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token)\s*[=:]\s*\S")),
    ("credentialed URL", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]{16,}=*")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


@dataclass
class PinAssessment:
    status: str  # pinned | not_reproducible | unpinned | invalid
    pin_class: str  # local_snapshot | backend_snapshot | version_identity | none
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_mutable_alias(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in MUTABLE_ALIASES


def assess_pin(root: Path, source: dict[str, Any], *, now: datetime | None = None) -> PinAssessment:
    """Classify how (and whether) one source is pinned. Never raises."""
    now = now or datetime.now(timezone.utc)
    pin = source.get("pin")
    identifier = get_in(source, "version", "identifier")
    published_at = get_in(source, "version", "published_at")
    # A mutable alias is a lie about identity whatever else is declared: a
    # snapshot of "latest" still cannot say *which* latest it froze.
    if _is_mutable_alias(identifier) or (identifier in (None, "") and _is_mutable_alias(published_at)):
        return PinAssessment("unpinned", "version_identity", f"version.identifier {identifier!r} is a mutable alias")
    if pin is None:
        if identifier in (None, "") and published_at in (None, ""):
            return PinAssessment("unpinned", "none", "no pin block and no version.identifier/published_at")
        return PinAssessment("pinned", "version_identity", "version identity is recorded and is not a mutable alias")

    if not isinstance(pin, dict):
        return PinAssessment("invalid", "none", "pin must be a mapping")
    pin_class = pin.get("class")
    if pin_class not in PIN_CLASSES:
        return PinAssessment("invalid", "none", f"pin.class must be one of {list(PIN_CLASSES)}, got {pin_class!r}")

    if pin_class == "local_snapshot":
        relative = pin.get("path")
        target = project_path(root, relative)
        if target is None:
            return PinAssessment("invalid", pin_class, "pin.path must be a safe project-relative path")
        if not str(relative).replace("\\", "/").startswith("data/source/"):
            return PinAssessment("invalid", pin_class, "a local snapshot must live under data/source/")
        expected = normalize_digest(pin.get("sha256"))
        if expected is None:
            return PinAssessment("invalid", pin_class, "pin.sha256 must be a sha256 digest of the snapshot")
        if _parse_timestamp(pin.get("captured_at")) is None:
            return PinAssessment("invalid", pin_class, "pin.captured_at must be an ISO-8601 timestamp")
        if not target.is_file():
            return PinAssessment(
                "not_reproducible", pin_class, f"snapshot file is missing: {relative}", {"cause": "snapshot_missing"}
            )
        actual = sha256_file(target)
        if actual != expected:
            return PinAssessment(
                "not_reproducible",
                pin_class,
                f"snapshot {relative} does not match pin.sha256",
                {"cause": "snapshot_hash_mismatch", "expected": expected, "actual": actual},
            )
        return PinAssessment("pinned", pin_class, f"local snapshot {relative} matches its content hash")

    identifier = pin.get("identifier")
    if not isinstance(identifier, str) or not identifier.strip():
        return PinAssessment("invalid", pin_class, "pin.identifier must name the backend snapshot")
    if _is_mutable_alias(identifier):
        return PinAssessment("unpinned", pin_class, f"pin.identifier {identifier!r} is a mutable alias, not a snapshot")
    if _parse_timestamp(pin.get("captured_at")) is None:
        return PinAssessment("invalid", pin_class, "pin.captured_at must be an ISO-8601 timestamp")
    retention = _parse_timestamp(pin.get("retention_until"))
    if retention is None:
        return PinAssessment(
            "invalid", pin_class, "pin.retention_until must record when the backend may drop the snapshot"
        )
    if retention <= now:
        return PinAssessment(
            "not_reproducible",
            pin_class,
            f"backend snapshot {identifier!r} retention expired at {pin.get('retention_until')}",
            {"cause": "snapshot_expired", "retention_until": str(pin.get("retention_until"))},
        )
    verification = pin.get("verification")
    if isinstance(verification, dict) and verification.get("status") == "inaccessible":
        return PinAssessment(
            "not_reproducible",
            pin_class,
            f"backend snapshot {identifier!r} was last verified inaccessible",
            {"cause": "snapshot_inaccessible", "verified_at": verification.get("at")},
        )
    return PinAssessment("pinned", pin_class, f"backend snapshot {identifier!r} is identified and retained until {pin.get('retention_until')}")


def _walk_strings(value: Any, path: str):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")


def find_inline_credentials(value: Any, path: str = "") -> list[dict[str, str]]:
    """Return every string in ``value`` that looks like an embedded secret.

    Reports the manifest path and the pattern name, never the matched text,
    so the finding itself cannot leak what it found.
    """
    findings: list[dict[str, str]] = []
    for where, text in _walk_strings(value, path):
        for name, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(text):
                findings.append({"path": where, "pattern": name})
                break
    return findings


def connection_reference_error(root: Path, connection: object) -> str | None:
    """``access.connection`` must be a reference, never a connection string."""
    if connection is None:
        return None
    reference = connection.get("ref") if isinstance(connection, dict) else connection
    if not isinstance(reference, str) or not reference.strip():
        return "access.connection must be a string reference or {ref: ...}"
    scheme, separator, remainder = reference.partition(":")
    if not separator or scheme not in CONNECTION_REFERENCE_SCHEMES or not remainder.strip():
        return (
            "access.connection must reference credentials indirectly "
            f"({', '.join(f'{item}:' for item in CONNECTION_REFERENCE_SCHEMES)}), not embed a connection string"
        )
    if scheme == "file":
        # The same rule the connector applies, so preflight cannot accept a
        # reference every source operation will refuse.
        candidate = Path(remainder.strip()).expanduser()
        if not candidate.is_absolute():
            return "access.connection file references must be absolute paths outside the project"
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return None
        return "access.connection file references must point outside the project directory"
    return None


def source_pin_summary(root: Path, sources: dict[str, Any], *, now: datetime | None = None) -> dict[str, PinAssessment]:
    return {
        str(key): assess_pin(root, source, now=now) if isinstance(source, dict) else PinAssessment("invalid", "none", "source must be a mapping")
        for key, source in sources.items()
    }


def redact(text: str) -> str:
    """Mask credential-like fragments in free text before it is recorded."""
    redacted = re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+):[^/\s@]+@", r"\1:***@", text)
    redacted = re.sub(
        r"(?i)((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token)\s*[=:]\s*)\S+",
        r"\1***",
        redacted,
    )
    redacted = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "AKIA****************", redacted)
    return redacted
