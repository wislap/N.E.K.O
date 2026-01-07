from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import ormsgpack

try:
    import zmq
    import zmq.asyncio
except Exception:  # pragma: no cover
    zmq = None
    zmq_asyncio = None


def _dumps(obj: Any) -> bytes:
    return ormsgpack.packb(obj)


def _loads(data: bytes) -> Any:
    return ormsgpack.unpackb(data)


@dataclass
class ZmqIpcClient:
    plugin_id: str
    endpoint: str

    def __post_init__(self) -> None:
        if zmq is None:
            raise RuntimeError("pyzmq is not available")
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.IDENTITY, self.plugin_id.encode("utf-8"))
        sock.setsockopt(zmq.LINGER, 0)
        sock.connect(self.endpoint)
        self._sock = sock

    def request(self, request: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
        if zmq is None:
            return None
        req_id = request.get("request_id")
        if not isinstance(req_id, str) or not req_id:
            return None
        try:
            self._sock.send_multipart([req_id.encode("utf-8"), _dumps(request)], flags=0)
        except Exception:
            return None

        poller = zmq.Poller()
        poller.register(self._sock, zmq.POLLIN)
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                events = dict(poller.poll(timeout=int(remaining * 1000)))
            except Exception:
                return None
            if self._sock not in events:
                continue
            try:
                frames = self._sock.recv_multipart(flags=0)
            except Exception:
                return None
            if len(frames) < 2:
                continue
            rid = None
            try:
                rid = frames[0].decode("utf-8")
            except Exception:
                rid = None
            if rid != req_id:
                continue
            try:
                payload = _loads(frames[1])
            except Exception:
                return None
            if isinstance(payload, dict):
                return payload
            return None

    def close(self) -> None:
        try:
            self._sock.close(0)
        except Exception:
            pass


class ZmqIpcServer:
    def __init__(self, endpoint: str, request_handler):
        if zmq is None:
            raise RuntimeError("pyzmq is not available")
        self._endpoint = str(endpoint)
        self._request_handler = request_handler
        self._ctx = zmq.asyncio.Context.instance()
        self._sock = self._ctx.socket(zmq.ROUTER)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.bind(self._endpoint)
        self._running = True

    async def serve_forever(self, shutdown_event) -> None:
        while self._running and not shutdown_event.is_set():
            try:
                frames = await asyncio.wait_for(self._sock.recv_multipart(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            except Exception:
                await _async_sleep(0.01)
                continue
            if len(frames) < 3:
                continue
            ident = frames[0]
            try:
                req_id = frames[1].decode("utf-8")
            except Exception:
                continue
            try:
                request = _loads(frames[2])
            except Exception:
                continue
            if not isinstance(request, dict):
                continue

            try:
                resp = await self._request_handler(request)
            except Exception as e:
                resp = {
                    "request_id": req_id,
                    "error": str(e),
                    "result": None,
                }

            if not isinstance(resp, dict):
                resp = {"request_id": req_id, "error": "invalid response", "result": None}
            resp.setdefault("request_id", req_id)
            try:
                await self._sock.send_multipart([ident, req_id.encode("utf-8"), _dumps(resp)])
            except Exception:
                continue

    def close(self) -> None:
        self._running = False
        try:
            self._sock.close(0)
        except Exception:
            pass


async def _async_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
