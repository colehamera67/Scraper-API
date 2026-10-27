from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def walk(obj: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere inside obj."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
