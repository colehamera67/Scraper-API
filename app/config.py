import os
from dataclasses import dataclass, field


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "SCRAPER_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        )
    )
    # Optional outbound proxy, e.g. http://user:pass@host:port. Standard HTTP(S)_PROXY env vars also work.
    proxy_url: str | None = field(default_factory=lambda: os.environ.get("SCRAPER_PROXY_URL") or None)
    timeout: float = field(default_factory=lambda: _float("SCRAPER_TIMEOUT", 20))
    # Minimum seconds between requests to the same host.
    min_interval: float = field(default_factory=lambda: _float("SCRAPER_MIN_INTERVAL", 2))
    cache_ttl: float = field(default_factory=lambda: _float("SCRAPER_CACHE_TTL", 900))
    # Lowe's prices are per store; set a store number to get that store's pricing.
    lowes_store_id: str | None = field(default_factory=lambda: os.environ.get("LOWES_STORE_ID") or None)
    # Price history database. Every scraped, parsed or imported product is recorded here.
    db_path: str = field(default_factory=lambda: os.environ.get("SCRAPER_DB", "data/prices.db"))
    record_history: bool = field(
        default_factory=lambda: os.environ.get("SCRAPER_RECORD_HISTORY", "1").lower() not in ("0", "false", "no")
    )
    # An observation identical to the previous one is skipped if it's newer than this many seconds.
    min_record_interval: float = field(default_factory=lambda: _float("SCRAPER_MIN_RECORD_INTERVAL", 1800))


settings = Settings()
