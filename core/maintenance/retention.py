"""Library ownership and deterministic weighted capacity eviction."""

import math
import os
from collections.abc import Collection

from ..util.normalization import canonicalize_path

DEFAULT_MAX_DISK_BYTES = 256 * 1024 * 1024  # 256 MB
DEFAULT_MAX_PENDING_DISK_BYTES = 128 * 1024 * 1024  # 128 MB
PROTECTED_PENDING_STATUSES = frozenset({"queued", "processing", "approved"})


def library_group(meta: dict) -> str:
    """Favorites take precedence without changing the stored character."""
    if meta.get("is_favorite"):
        return "favorites"
    if str(meta.get("character") or "").strip():
        return "characters"
    return "general"


def is_auto_evictable(meta: object) -> bool:
    return (
        isinstance(meta, dict)
        and library_group(meta) == "general"
        and str(meta.get("retention_class") or "native").strip()
        not in {"external", "pinned"}
    )


def library_counts(index: dict) -> dict[str, int]:
    counts = {"general": 0, "favorites": 0, "characters": 0, "automatic": 0}
    for meta in index.values():
        if isinstance(meta, dict):
            counts[library_group(meta)] += 1
            counts["automatic"] += int(is_auto_evictable(meta))
    return counts


def nonnegative_number(value: object, default: float = 0) -> float:
    try:
        number = float(value)
        return max(0, number) if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


def entry_file_size(path: str, meta: dict) -> int:
    """Resolve an entry's byte size from metadata or disk stat (0 if unavailable)."""
    for key in ("file_size", "size", "size_bytes"):
        raw = meta.get(key)
        if raw is not None and not isinstance(raw, bool):
            try:
                val = int(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    try:
        return max(0, int(os.path.getsize(path)))
    except OSError:
        return 0


def automatic_disk_bytes(index: dict) -> int:
    """Return total disk bytes occupied by auto-evictable entries in ``index``."""
    total = 0
    for path, meta in index.items():
        if is_auto_evictable(meta):
            total += entry_file_size(str(path), meta)
    return total


def eviction_candidates(
    index: dict,
    limit: int,
    usage_weight: float = 0.7,
    *,
    max_disk_bytes: int | None = None,
) -> list[tuple[str, int]]:
    """Score low usage and old creation time on [0, 1]; largest score first.

    Only eligible entries contribute to either normalization or the quota.
    Equal scores prefer lower usage, then older creation, then stable path.
    When ``max_disk_bytes`` is positive, additional lowest-priority entries are
    selected until remaining auto-evictable disk usage is at or below the cap.
    """
    byte_cap = int(max_disk_bytes) if max_disk_bytes is not None and max_disk_bytes > 0 else 0
    if limit <= 0 and byte_cap <= 0:
        return []
    items = [
        (
            path,
            nonnegative_number(meta.get("use_count")),
            int(nonnegative_number(meta.get("created_at"))),
            entry_file_size(str(path), meta) if byte_cap > 0 else 0,
        )
        for path, meta in index.items()
        if is_auto_evictable(meta)
    ]
    if not items:
        return []
    count_overflow = max(0, len(items) - limit) if limit > 0 else 0
    total_bytes = sum(item[3] for item in items) if byte_cap > 0 else 0
    byte_overflow = max(0, total_bytes - byte_cap) if byte_cap > 0 else 0
    if count_overflow <= 0 and byte_overflow <= 0:
        return []
    weight = min(1, nonnegative_number(usage_weight, 0.7))
    min_usage = min(item[1] for item in items)
    usage_span = max(item[1] for item in items) - min_usage
    min_created = min(item[2] for item in items)
    age_span = max(item[2] for item in items) - min_created

    def sort_key(item):
        path, usage, created, _size = item
        low_usage = 1 - (usage - min_usage) / usage_span if usage_span else 0
        old_age = 1 - (created - min_created) / age_span if age_span else 0
        score = weight * low_usage + (1 - weight) * old_age
        return -score, usage, created, path

    items.sort(key=sort_key)
    selected = list(items[:count_overflow])
    if byte_cap > 0:
        remaining_bytes = total_bytes - sum(item[3] for item in selected)
        if remaining_bytes > byte_cap:
            for item in items[count_overflow:]:
                selected.append(item)
                remaining_bytes -= item[3]
                if remaining_bytes <= byte_cap:
                    break
    return [(path, created) for path, _, created, _ in selected]


def is_pending_evictable(
    row: object,
    *,
    protected_paths: Collection[str] | None = None,
) -> bool:
    """Return whether a pending row may be evicted by disk quota cleanup.

    Strictly protects in-flight VLM jobs (``queued`` / ``processing``),
    already-approved entries (``approved``), and any path still referenced
    by the live library.
    """
    if not isinstance(row, dict):
        return False
    path = str(row.get("path") or "").strip()
    if not path:
        return False
    status = str(row.get("review_status") or "pending").strip().lower()
    if status in PROTECTED_PENDING_STATUSES:
        return False
    if protected_paths and canonicalize_path(path) in protected_paths:
        return False
    return True


def pending_eviction_candidates(
    pending_rows: list[dict],
    max_disk_bytes: int | None = DEFAULT_MAX_PENDING_DISK_BYTES,
    *,
    protected_paths: Collection[str] | None = None,
) -> list[dict]:
    """Select oldest evictable pending entries when pending/ disk usage exceeds ``max_disk_bytes``."""
    byte_cap = int(max_disk_bytes) if max_disk_bytes is not None and max_disk_bytes > 0 else 0
    if byte_cap <= 0 or not pending_rows:
        return []
    normalized_protected = (
        {canonicalize_path(p) for p in protected_paths if p}
        if protected_paths
        else None
    )
    sized: list[tuple[int, int, str, dict, int]] = []
    total_bytes = 0
    for row in pending_rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "")
        if not path:
            continue
        size = entry_file_size(path, row)
        total_bytes += size
        if not is_pending_evictable(row, protected_paths=normalized_protected):
            continue
        created_raw = row.get("created_at")
        try:
            created = int(nonnegative_number(created_raw, 0))
        except Exception:
            created = 0
        if created <= 0:
            try:
                created = int(os.path.getmtime(path))
            except OSError:
                created = 0
        try:
            row_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            row_id = 0
        sized.append((created, row_id, path, row, size))
    if total_bytes <= byte_cap:
        return []
    sized.sort(key=lambda item: (item[0], item[1], item[2]))
    evicted: list[dict] = []
    remaining = total_bytes
    for _created, _row_id, _path, row, size in sized:
        if remaining <= byte_cap:
            break
        evicted.append(row)
        remaining -= size
    return evicted
