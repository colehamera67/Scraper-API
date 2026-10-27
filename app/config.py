import json
import os
from dataclasses import dataclass, field


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() not in ("0", "false", "no")


def _json_dict(name: str) -> dict[str, str]:
    raw = os.environ.get(name)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{name} must be a JSON object, e.g. '{{\"X-Api-Key\": \"...\"}}': {e}") from e
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return {str(k): str(v) for k, v in value.items()}


@dataclass(frozen=True)
class RetailerAccess:
    """How to reach one retailer: whatever the retailer gave you for approved access, plus speed limits."""

    # Extra request headers, e.g. a partner token: {"X-Partner-Token": "..."}
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    # Fixed outbound proxy for this retailer (e.g. the static IP the retailer allowlisted).
    proxy_url: str | None = None
    # Seconds between request starts. None uses the global SCRAPER_MIN_INTERVAL.
    min_interval: float | None = None
    # Requests allowed in flight at once.
    concurrency: int = 1
    # Sitemap URLs for catalog discovery. Empty means read them from robots.txt.
    sitemaps: tuple[str, ...] = ()


def retailer_access_from_env(prefix: str) -> RetailerAccess:
    """Read <PREFIX>_HEADERS, _COOKIES, _PROXY_URL, _MIN_INTERVAL, _CONCURRENCY and _SITEMAPS."""
    interval = os.environ.get(f"{prefix}_MIN_INTERVAL")
    concurrency = int(os.environ.get(f"{prefix}_CONCURRENCY", 1))
    if concurrency < 1:
        raise ValueError(f"{prefix}_CONCURRENCY must be at least 1")
    sitemaps = os.environ.get(f"{prefix}_SITEMAPS", "")
    return RetailerAccess(
        headers=_json_dict(f"{prefix}_HEADERS"),
        cookies=_json_dict(f"{prefix}_COOKIES"),
        proxy_url=os.environ.get(f"{prefix}_PROXY_URL") or None,
        min_interval=float(interval) if interval else None,
        concurrency=concurrency,
        sitemaps=tuple(s.strip() for s in sitemaps.split(",") if s.strip()),
    )


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
    record_history: bool = field(default_factory=lambda: _bool("SCRAPER_RECORD_HISTORY", True))
    # An observation identical to the previous one is skipped if it's newer than this many seconds.
    min_record_interval: float = field(default_factory=lambda: _float("SCRAPER_MIN_RECORD_INTERVAL", 1800))
    # Per-retailer access, keyed by domain.
    access: dict[str, RetailerAccess] = field(
        default_factory=lambda: {
            "walmart.com": retailer_access_from_env("WALMART"),
            "lowes.com": retailer_access_from_env("LOWES"),
        }
    )
    # Skip pages robots.txt disallows for our user agent.
    respect_robots: bool = field(default_factory=lambda: _bool("SCRAPER_RESPECT_ROBOTS", True))
    # The crawler stops a run after this many blocked responses in a row.
    crawl_block_limit: int = field(default_factory=lambda: int(os.environ.get("SCRAPER_CRAWL_BLOCK_LIMIT", 20)))


settings = Settings()
