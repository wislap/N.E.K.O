from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict


SendResponse = Callable[[str, str, Any, str | None], None]
Request = Dict[str, Any]
RequestHandler = Callable[[Request, Callable[..., None]], Awaitable[None]]
