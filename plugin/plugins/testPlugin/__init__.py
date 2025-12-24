from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
import time

from plugin.sdk.base import NekoPluginBase
from plugin.sdk.decorators import lifecycle, neko_plugin, plugin_entry
from plugin.sdk import ok


@neko_plugin
class HelloPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)  # 传递 ctx 给基类
        # 启用文件日志(同时输出到文件和控制台)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger  # 使用file_logger作为主要logger
        self.plugin_id = ctx.plugin_id  # 使用 plugin_id
        self._debug_executor = ThreadPoolExecutor(max_workers=32)
        self.file_logger.info("HelloPlugin initialized with file logging enabled")

    def _start_debug_timer(self) -> None:
        # Delay to ensure the plugin command loop is running before we do sync IPC calls.
        time.sleep(0.8)
        try:
            enabled = bool(self.config.get("debug.enable", default=False))
            if not enabled:
                return

            interval_seconds = float(self.config.get("debug.interval_seconds", default=3.0))
            burst_size = int(self.config.get("debug.burst_size", default=5))
            max_count = int(self.config.get("debug.max_count", default=0))
            timer_id = str(self.config.get("debug.timer_id", default="")).strip()
            if not timer_id:
                timer_id = f"testPlugin_debug_{int(time.time())}"
                self.config.set("debug.timer_id", timer_id)

            # Keep startup non-blocking: do the update in background as well.
            loaded_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            self.config.set("debug.loaded_at", loaded_at)

            # IMPORTANT: avoid immediate callback before command loop is ready.
            # We already delayed; immediate=True is now safe and gives fast feedback.
            self.plugins.call_entry(
                "timer_service:start_timer",
                {
                    "timer_id": timer_id,
                    "interval": interval_seconds,
                    "immediate": True,
                    "callback_plugin_id": self.ctx.plugin_id,
                    "callback_entry_id": "on_debug_tick",
                    "callback_args": {
                        "timer_id": timer_id,
                        "burst_size": burst_size,
                        "max_count": max_count,
                    },
                },
                timeout=10.0,
            )
            self.file_logger.info(
                "Debug timer started: timer_id=%s interval=%s burst_size=%s max_count=%s",
                timer_id,
                interval_seconds,
                burst_size,
                max_count,
            )
        except Exception as e:
            self.file_logger.exception("Failed to start debug timer: %s", e)

    @lifecycle(id="startup")
    def startup(self, **_):
        enabled = bool(self.config.get("debug.enable", default=False))

        if not enabled:
            self.file_logger.info("Debug disabled (debug.enable=false), skipping startup debug actions")
            return ok(data={"status": "disabled", "loaded_at": None})

        threading.Thread(target=self._start_debug_timer, daemon=True, name="testPlugin-debug-timer").start()
        return ok(data={"status": "enabled"})

    @plugin_entry(id="on_debug_tick")
    def on_debug_tick(
        self,
        timer_id: str,
        burst_size: int = 5,
        current_count: int = 0,
        max_count: int = 0,
        **_,
    ):
        sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        def _send_one(i: int) -> None:
            self.ctx.push_message(
                source="testPlugin.debug",
                message_type="text",
                description="debug tick burst",
                priority=1,
                content=f"debug tick: timer_id={timer_id}, tick={current_count}, msg={i+1}/{burst_size}, at={sent_at}",
                metadata={
                    "timer_id": timer_id,
                    "tick": current_count,
                    "burst_index": i,
                    "burst_size": burst_size,
                    "sent_at": sent_at,
                },
            )

        n = max(0, int(burst_size))
        # Fire-and-forget: do not block the callback, avoid timer_service timeout.
        for i in range(n):
            self._debug_executor.submit(_send_one, i)

        return ok(
            data={
                "timer_id": timer_id,
                "tick": current_count,
                "burst_size": n,
                "max_count": max_count,
                "sent_at": sent_at,
            }
        )

    def run(self, message: str | None = None, **kwargs):
        # 简单返回一个字典结构
        self.file_logger.info(f"Running HelloPlugin with message: {message}")
        return {
            "hello": message or "world",
            "extra": kwargs,
        }
