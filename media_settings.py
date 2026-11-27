"""Validated options for the integrated behavior layer; defaults preserve 0.3."""

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class MediaSettings:
    preferences_enabled: bool = True
    repeat_guard_enabled: bool = True
    repeat_window_seconds: int = 1800
    recent_receipts: int = 12
    less_interval_seconds: int = 180
    similar_demotion_enabled: bool = True
    similar_distance: int = 8
    decision_records_enabled: bool = True
    record_retention_days: int = 30
    max_records: int = 5000
    character_check_enabled: bool = True
    character_check_provider: str = ""
    character_check_timeout: int = 60
    character_check_max_images: int = 2
    character_check_auto_redraw: bool = True
    make_meme_daily_limit: int = 10
    crowd_curation_threshold: int = 2

    @classmethod
    def parse(cls, raw):
        raw = raw if isinstance(raw, dict) else {}
        defaults = asdict(cls())
        bounds = {
            "repeat_window_seconds": (0, 86400),
            "recent_receipts": (1, 100),
            "less_interval_seconds": (0, 3600),
            "similar_distance": (0, 32),
            "record_retention_days": (1, 365),
            "max_records": (100, 50000),
            "character_check_timeout": (5, 120),
            "character_check_max_images": (1, 4),
            "make_meme_daily_limit": (1, 200),
            "crowd_curation_threshold": (1, 20),
        }
        for field in fields(cls):
            key = field.name
            value = raw.get(key, defaults[key])
            if isinstance(defaults[key], bool):
                if type(value) is bool:
                    defaults[key] = value
            elif key in bounds:
                if type(value) is int:
                    lower, upper = bounds[key]
                    defaults[key] = max(lower, min(upper, value))
            elif isinstance(value, str):
                defaults[key] = value.strip()[:200]
        return cls(**defaults)

    def public(self):
        return asdict(self)
