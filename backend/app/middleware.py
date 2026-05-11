"""Request ID + access logging middleware."""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("beatbridge.access")


class RequestIdMiddleware(BaseHTTPMiddleware):
    header_name = "x-request-id"

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get(self.header_name) or uuid.uuid4().hex[:12]
        start = time.perf_counter()
        # Stash on state so handlers can include it in logs.
        request.state.request_id = rid
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000
            logger.exception(
                "request %s %s failed after %.1fms (rid=%s)",
                request.method,
                request.url.path,
                elapsed,
                rid,
            )
            raise
        elapsed = (time.perf_counter() - start) * 1000
        response.headers[self.header_name] = rid
        logger.info(
            "%s %s -> %d in %.1fms (rid=%s)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
            rid,
        )
        return response
