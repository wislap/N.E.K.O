from __future__ import annotations

import time
from typing import Any, Dict, Optional

import ormsgpack
import zmq
from loguru import logger

from plugin.settings import MESSAGE_PLANE_INGEST_RCVHWM

from .pub_server import MessagePlanePubServer
from .stores import StoreRegistry, TopicStore


def _loads(data: bytes) -> Any:
    return ormsgpack.unpackb(data)


class MessagePlaneIngestServer:
    def __init__(
        self,
        *,
        endpoint: str,
        stores: StoreRegistry,
        pub_server: Optional[MessagePlanePubServer],
    ) -> None:
        self.endpoint = str(endpoint)
        self._stores = stores
        self._pub = pub_server

        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.PULL)
        self._sock.linger = 0
        try:
            self._sock.setsockopt(zmq.RCVHWM, int(MESSAGE_PLANE_INGEST_RCVHWM))
        except Exception:
            pass
        self._sock.bind(self.endpoint)
        self._running = False

    def stop(self) -> None:
        self._running = False

    def close(self) -> None:
        try:
            self._sock.close(linger=0)
        except Exception:
            pass

    def _resolve_store(self, name: Any) -> Optional[TopicStore]:
        if name is None:
            return None
        return self._stores.get(str(name))

    def _ingest_delta_batch(self, msg: Dict[str, Any]) -> None:
        items = msg.get("items")
        if not isinstance(items, list):
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            st = self._resolve_store(it.get("store") or it.get("bus"))
            if st is None:
                continue
            topic = it.get("topic")
            if not isinstance(topic, str) or not topic:
                continue
            payload = it.get("payload")
            if not isinstance(payload, dict):
                payload = {"value": payload}
            try:
                event = st.publish(topic, payload)
            except Exception:
                continue
            if self._pub is not None:
                try:
                    self._pub.publish(f"{st.name}.{topic}", event)
                except Exception:
                    pass

    def _ingest_snapshot(self, msg: Dict[str, Any]) -> None:
        st = self._resolve_store(msg.get("store") or msg.get("bus"))
        if st is None:
            return
        topic = msg.get("topic")
        if not isinstance(topic, str) or not topic:
            topic = "snapshot.all"
        mode = msg.get("mode")
        items = msg.get("items")
        if not isinstance(items, list):
            return
        records = [x for x in items if isinstance(x, dict)]
        if str(mode or "replace") == "append":
            for rec in records:
                try:
                    event = st.publish(topic, rec)
                except Exception:
                    continue
                if self._pub is not None:
                    try:
                        self._pub.publish(f"{st.name}.{topic}", event)
                    except Exception:
                        pass
            return

        try:
            events = st.replace_topic(topic, records)
        except Exception:
            events = []
        if self._pub is not None:
            for ev in events:
                try:
                    self._pub.publish(f"{st.name}.{topic}", ev)
                except Exception:
                    continue

    def serve_forever(self) -> None:
        self._running = True
        poller = zmq.Poller()
        poller.register(self._sock, zmq.POLLIN)
        logger.info("[message_plane] ingest server bound: {}", self.endpoint)
        while self._running:
            try:
                events = dict(poller.poll(timeout=250))
            except Exception:
                continue
            if self._sock not in events:
                continue
            try:
                raw = self._sock.recv(flags=0)
            except Exception:
                continue
            try:
                obj = _loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("kind")
            if kind == "snapshot":
                try:
                    self._ingest_snapshot(obj)
                except Exception:
                    pass
                continue
            try:
                self._ingest_delta_batch(obj)
            except Exception:
                pass
