import time
import threading
from typing import Any, Dict, Optional

from plugin.sdk.base import NekoPluginBase
from plugin.sdk.decorators import neko_plugin, plugin_entry, lifecycle
from plugin.sdk import ok


@neko_plugin
class LoadTestPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self.plugin_id = ctx.plugin_id

    def _bench_loop(self, duration_seconds: float, fn, *args, **kwargs) -> Dict[str, Any]:
        start = time.perf_counter()
        end_time = start + float(duration_seconds)
        count = 0
        errors = 0
        while True:
            now = time.perf_counter()
            if now >= end_time:
                break
            try:
                fn(*args, **kwargs)
                count += 1
            except Exception as e:  # pragma: no cover - defensive
                errors += 1
                try:
                    self.logger.warning("[load_tester] bench iteration failed: {}", e)
                except Exception:
                    pass
        elapsed = time.perf_counter() - start
        qps = float(count) / elapsed if elapsed > 0 else 0.0
        return {
            "iterations": count,
            "errors": errors,
            "elapsed_seconds": elapsed,
            "qps": qps,
        }

    def _bench_loop_concurrent(self, duration_seconds: float, workers: int, fn, *args, **kwargs) -> Dict[str, Any]:
        """Run benchmark with multiple worker threads.

        workers <= 1 时退化为单线程调用者应当已经处理, 这里假设 workers >= 1.
        """
        start = time.perf_counter()
        end_time = start + float(duration_seconds)
        count = 0
        errors = 0
        lock = threading.Lock()

        def _worker() -> None:
            nonlocal count, errors
            while True:
                now = time.perf_counter()
                if now >= end_time:
                    break
                try:
                    fn(*args, **kwargs)
                    with lock:
                        count += 1
                except Exception as e:  # pragma: no cover - defensive
                    with lock:
                        errors += 1
                    try:
                        self.logger.warning("[load_tester] bench iteration failed (concurrent): {}", e)
                    except Exception:
                        pass

        threads = []
        worker_count = max(1, int(workers))
        for _ in range(worker_count):
            t = threading.Thread(target=_worker, daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            try:
                t.join()
            except Exception:
                # 避免 join 异常中断整个压测
                pass

        elapsed = time.perf_counter() - start
        qps = float(count) / elapsed if elapsed > 0 else 0.0
        return {
            "iterations": count,
            "errors": errors,
            "elapsed_seconds": elapsed,
            "qps": qps,
            "workers": worker_count,
        }

    def _get_load_test_section(self, section: Optional[str] = None) -> Dict[str, Any]:
        """Read load_test config section from plugin.toml via PluginConfig.

        - section is None: return [load_test]
        - section = "push_messages": return [load_test.push_messages]
        """
        path = "load_test" if not section else f"load_test.{section}"
        try:
            return self.config.get_section(path)
        except Exception:
            # 配置缺失或格式不对时，按空配置处理，避免影响插件可用性
            return {}

    def _get_global_bench_config(self, root_cfg: Optional[Dict[str, Any]]) -> tuple[int, bool]:
        """Read global worker_threads and log_summary from [load_test] section.

        Returns (workers, log_summary).
        """
        base_cfg: Dict[str, Any] = root_cfg or {}
        log_summary = bool(base_cfg.get("log_summary", True))
        workers_raw = base_cfg.get("worker_threads", 1)
        try:
            workers_int = int(workers_raw)
        except Exception:
            workers_int = 1
        workers = max(1, workers_int)
        return workers, log_summary

    @plugin_entry(
        id="bench_push_messages",
        name="Bench Push Messages",
        description="Measure QPS of ctx.push_message (message bus write)",
        input_schema={
            "type": "object",
            "properties": {
                "duration_seconds": {
                    "type": "number",
                    "description": "Benchmark duration in seconds",
                    "default": 5.0,
                },
            },
        },
    )
    def bench_push_messages(self, duration_seconds: float = 5.0, **_: Any):
        root_cfg = self._get_load_test_section(None)
        sec_cfg = self._get_load_test_section("push_messages")
        global_enabled = bool(root_cfg.get("enable", True)) if root_cfg else True
        enabled = bool(sec_cfg.get("enable", global_enabled)) if sec_cfg else global_enabled
        if not enabled:
            try:
                self.logger.info("[load_tester] bench_push_messages disabled by config")
            except Exception:
                pass
            return ok(data={"test": "bench_push_messages", "enabled": False, "skipped": True})

        workers, log_summary = self._get_global_bench_config(root_cfg)

        dur_cfg = sec_cfg.get("duration_seconds") if sec_cfg else None
        if dur_cfg is None and root_cfg:
            dur_cfg = root_cfg.get("duration_seconds")
        try:
            duration = float(dur_cfg) if dur_cfg is not None else duration_seconds
        except Exception:
            duration = duration_seconds

        def _op() -> None:
            self.ctx.push_message(
                source="load_tester.push_messages",
                message_type="text",
                description="load test message",
                priority=1,
                content="load_test",
            )

        if workers > 1:
            stats = self._bench_loop_concurrent(duration, workers, _op)
        else:
            stats = self._bench_loop(duration, _op)

        if log_summary:
            try:
                self.logger.info(
                    "[load_tester] bench_push_messages duration={}s iterations={} qps={} errors={} workers={}",
                    duration,
                    stats["iterations"],
                    stats["qps"],
                    stats["errors"],
                    stats.get("workers", workers),
                )
            except Exception:
                pass
        return ok(data={"test": "bench_push_messages", **stats})

    @plugin_entry(
        id="bench_bus_messages_get",
        name="Bench Bus Messages Get",
        description="Measure QPS of bus.messages.get() (message bus read)",
        input_schema={
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "number", "default": 5.0},
                "max_count": {"type": "integer", "default": 50},
                "plugin_id": {"type": "string", "default": "*"},
                "timeout": {"type": "number", "default": 0.1},
            },
        },
    )
    def bench_bus_messages_get(
        self,
        duration_seconds: float = 5.0,
        max_count: int = 50,
        plugin_id: str = "*",
        timeout: float = 0.1,
        **_: Any,
    ):
        root_cfg = self._get_load_test_section(None)
        sec_cfg = self._get_load_test_section("bus_messages_get")
        global_enabled = bool(root_cfg.get("enable", True)) if root_cfg else True
        enabled = bool(sec_cfg.get("enable", global_enabled)) if sec_cfg else global_enabled
        if not enabled:
            try:
                self.logger.info("[load_tester] bench_bus_messages_get disabled by config")
            except Exception:
                pass
            return ok(data={"test": "bench_bus_messages_get", "enabled": False, "skipped": True})

        workers, log_summary = self._get_global_bench_config(root_cfg)

        dur_cfg = sec_cfg.get("duration_seconds") if sec_cfg else None
        if dur_cfg is None and root_cfg:
            dur_cfg = root_cfg.get("duration_seconds")
        try:
            duration = float(dur_cfg) if dur_cfg is not None else duration_seconds
        except Exception:
            duration = duration_seconds

        pid_norm = None if not plugin_id or plugin_id.strip() == "*" else plugin_id.strip()

        def _op() -> None:
            _ = self.ctx.bus.messages.get(
                plugin_id=pid_norm,
                max_count=int(max_count),
                timeout=float(timeout),
            )

        if workers > 1:
            stats = self._bench_loop_concurrent(duration, workers, _op)
        else:
            stats = self._bench_loop(duration, _op)

        if log_summary:
            try:
                self.logger.info(
                    "[load_tester] bench_bus_messages_get duration={}s iterations={} qps={} errors={} max_count={} plugin_id={} timeout={} workers={}",
                    duration,
                    stats["iterations"],
                    stats["qps"],
                    stats["errors"],
                    max_count,
                    plugin_id,
                    timeout,
                    stats.get("workers", workers),
                )
            except Exception:
                pass
        return ok(data={"test": "bench_bus_messages_get", **stats})

    @plugin_entry(
        id="bench_bus_events_get",
        name="Bench Bus Events Get",
        description="Measure QPS of bus.events.get() (event bus read)",
        input_schema={
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "number", "default": 5.0},
                "max_count": {"type": "integer", "default": 50},
                "plugin_id": {"type": "string", "default": "*"},
                "timeout": {"type": "number", "default": 0.1},
            },
        },
    )
    def bench_bus_events_get(
        self,
        duration_seconds: float = 5.0,
        max_count: int = 50,
        plugin_id: str = "*",
        timeout: float = 0.1,
        **_: Any,
    ):
        root_cfg = self._get_load_test_section(None)
        sec_cfg = self._get_load_test_section("bus_events_get")
        global_enabled = bool(root_cfg.get("enable", True)) if root_cfg else True
        enabled = bool(sec_cfg.get("enable", global_enabled)) if sec_cfg else global_enabled
        if not enabled:
            try:
                self.logger.info("[load_tester] bench_bus_events_get disabled by config")
            except Exception:
                pass
            return ok(data={"test": "bench_bus_events_get", "enabled": False, "skipped": True})

        workers, log_summary = self._get_global_bench_config(root_cfg)

        dur_cfg = sec_cfg.get("duration_seconds") if sec_cfg else None
        if dur_cfg is None and root_cfg:
            dur_cfg = root_cfg.get("duration_seconds")
        try:
            duration = float(dur_cfg) if dur_cfg is not None else duration_seconds
        except Exception:
            duration = duration_seconds

        pid_norm = None if not plugin_id or plugin_id.strip() == "*" else plugin_id.strip()

        def _op() -> None:
            _ = self.ctx.bus.events.get(
                plugin_id=pid_norm,
                max_count=int(max_count),
                timeout=float(timeout),
            )

        if workers > 1:
            stats = self._bench_loop_concurrent(duration, workers, _op)
        else:
            stats = self._bench_loop(duration, _op)

        if log_summary:
            try:
                self.logger.info(
                    "[load_tester] bench_bus_events_get duration={}s iterations={} qps={} errors={} max_count={} plugin_id={} timeout={} workers={}",
                    duration,
                    stats["iterations"],
                    stats["qps"],
                    stats["errors"],
                    max_count,
                    plugin_id,
                    timeout,
                    stats.get("workers", workers),
                )
            except Exception:
                pass
        return ok(data={"test": "bench_bus_events_get", **stats})

    @plugin_entry(
        id="bench_bus_lifecycle_get",
        name="Bench Bus Lifecycle Get",
        description="Measure QPS of bus.lifecycle.get() (lifecycle bus read)",
        input_schema={
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "number", "default": 5.0},
                "max_count": {"type": "integer", "default": 50},
                "plugin_id": {"type": "string", "default": "*"},
                "timeout": {"type": "number", "default": 0.1},
            },
        },
    )
    def bench_bus_lifecycle_get(
        self,
        duration_seconds: float = 5.0,
        max_count: int = 50,
        plugin_id: str = "*",
        timeout: float = 0.1,
        **_: Any,
    ):
        root_cfg = self._get_load_test_section(None)
        sec_cfg = self._get_load_test_section("bus_lifecycle_get")
        global_enabled = bool(root_cfg.get("enable", True)) if root_cfg else True
        enabled = bool(sec_cfg.get("enable", global_enabled)) if sec_cfg else global_enabled
        if not enabled:
            try:
                self.logger.info("[load_tester] bench_bus_lifecycle_get disabled by config")
            except Exception:
                pass
            return ok(data={"test": "bench_bus_lifecycle_get", "enabled": False, "skipped": True})

        workers, log_summary = self._get_global_bench_config(root_cfg)

        dur_cfg = sec_cfg.get("duration_seconds") if sec_cfg else None
        if dur_cfg is None and root_cfg:
            dur_cfg = root_cfg.get("duration_seconds")
        try:
            duration = float(dur_cfg) if dur_cfg is not None else duration_seconds
        except Exception:
            duration = duration_seconds

        pid_norm = None if not plugin_id or plugin_id.strip() == "*" else plugin_id.strip()

        def _op() -> None:
            _ = self.ctx.bus.lifecycle.get(
                plugin_id=pid_norm,
                max_count=int(max_count),
                timeout=float(timeout),
            )

        if workers > 1:
            stats = self._bench_loop_concurrent(duration, workers, _op)
        else:
            stats = self._bench_loop(duration, _op)

        if log_summary:
            try:
                self.logger.info(
                    "[load_tester] bench_bus_lifecycle_get duration={}s iterations={} qps={} errors={} max_count={} plugin_id={} timeout={} workers={}",
                    duration,
                    stats["iterations"],
                    stats["qps"],
                    stats["errors"],
                    max_count,
                    plugin_id,
                    timeout,
                    stats.get("workers", workers),
                )
            except Exception:
                pass
        return ok(data={"test": "bench_bus_lifecycle_get", **stats})

    @plugin_entry(
        id="bench_buslist_filter",
        name="Bench BusList Filter",
        description="Measure QPS of BusList.filter() on a preloaded message list",
        input_schema={
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "number", "default": 5.0},
                "max_count": {"type": "integer", "default": 500},
                "timeout": {"type": "number", "default": 1.0},
                "source": {"type": "string", "default": ""},
            },
        },
    )
    def bench_buslist_filter(
        self,
        duration_seconds: float = 5.0,
        max_count: int = 500,
        timeout: float = 1.0,
        source: str = "",
        **_: Any,
    ):
        root_cfg = self._get_load_test_section(None)
        sec_cfg = self._get_load_test_section("buslist_filter")
        global_enabled = bool(root_cfg.get("enable", True)) if root_cfg else True
        enabled = bool(sec_cfg.get("enable", global_enabled)) if sec_cfg else global_enabled
        if not enabled:
            try:
                self.logger.info("[load_tester] bench_buslist_filter disabled by config")
            except Exception:
                pass
            return ok(data={"test": "bench_buslist_filter", "enabled": False, "skipped": True})

        workers, log_summary = self._get_global_bench_config(root_cfg)

        dur_cfg = sec_cfg.get("duration_seconds") if sec_cfg else None
        if dur_cfg is None and root_cfg:
            dur_cfg = root_cfg.get("duration_seconds")
        try:
            duration = float(dur_cfg) if dur_cfg is not None else duration_seconds
        except Exception:
            duration = duration_seconds

        base_list = self.ctx.bus.messages.get(
            plugin_id=None,
            max_count=int(max_count),
            timeout=float(timeout),
        )

        if len(base_list) == 0:
            try:
                self.logger.info("[load_tester] bench_buslist_filter: no messages available, pushing seed messages")
            except Exception:
                pass
            for _ in range(10):
                self.ctx.push_message(
                    source="load_tester.seed",
                    message_type="text",
                    description="seed message for buslist benchmark",
                    priority=1,
                    content="seed",
                )
            base_list = self.ctx.bus.messages.get(
                plugin_id=None,
                max_count=int(max_count),
                timeout=float(timeout),
            )

        flt_kwargs: Dict[str, Any] = {}
        if source:
            flt_kwargs["source"] = source
        else:
            flt_kwargs["source"] = "load_tester"

        def _op() -> None:
            _ = base_list.filter(strict=False, **flt_kwargs)

        if workers > 1:
            stats = self._bench_loop_concurrent(duration, workers, _op)
        else:
            stats = self._bench_loop(duration, _op)

        if log_summary:
            try:
                self.logger.info(
                    "[load_tester] bench_buslist_filter duration={}s iterations={} qps={} errors={} base_size={} filter={} workers={}",
                    duration,
                    stats["iterations"],
                    stats["qps"],
                    stats["errors"],
                    len(base_list),
                    flt_kwargs,
                    stats.get("workers", workers),
                )
            except Exception:
                pass
        return ok(data={"test": "bench_buslist_filter", "base_size": len(base_list), **stats})

    @plugin_entry(
        id="run_all_benchmarks",
        name="Run All Benchmarks",
        description="Run a suite of QPS benchmarks for core subsystems",
        input_schema={
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "number", "default": 5.0},
            },
        },
    )
    def run_all_benchmarks(self, duration_seconds: float = 5.0, **_: Any):
        root_cfg = self._get_load_test_section(None)
        if root_cfg and not bool(root_cfg.get("enable", True)):
            try:
                self.logger.info("[load_tester] run_all_benchmarks disabled by config.load_test.enable=false")
            except Exception:
                pass
            return ok(data={"tests": {}, "enabled": False, "skipped": True})

        # 如果配置中设置了全局 duration_seconds，则覆盖参数作为默认时长
        if root_cfg:
            dur_cfg = root_cfg.get("duration_seconds")
            try:
                if dur_cfg is not None:
                    duration_seconds = float(dur_cfg)
            except Exception:
                pass

        results = {}
        tests = [
            ("bench_push_messages", self.bench_push_messages),
            ("bench_bus_messages_get", self.bench_bus_messages_get),
            ("bench_bus_events_get", self.bench_bus_events_get),
            ("bench_bus_lifecycle_get", self.bench_bus_lifecycle_get),
            ("bench_buslist_filter", self.bench_buslist_filter),
        ]
        for name, fn in tests:
            try:
                res = fn(duration_seconds=duration_seconds)
                results[name] = getattr(res, "data", None) or getattr(res, "get", lambda *_a, **_k: None)("data") or None
            except Exception as e:
                results[name] = {"error": str(e)}
                try:
                    self.logger.warning("[load_tester] benchmark {} failed: {}", name, e)
                except Exception:
                    pass
        try:
            self.logger.info("[load_tester] run_all_benchmarks finished: {}", results)
        except Exception:
            pass
        return ok(data={"tests": results})

    @lifecycle(id="startup")
    def startup(self, **_: Any):
        """Optional auto-start hook to run benchmarks on plugin startup.

        Controlled by plugin.toml:

        [load_test]
        auto_start = true/false
        """
        root_cfg = self._get_load_test_section(None)
        auto_start = bool(root_cfg.get("auto_start", False)) if root_cfg else False
        if not auto_start:
            try:
                self.logger.info("[load_tester] startup: auto_start disabled, skipping benchmarks")
            except Exception:
                pass
            return ok(data={"status": "startup_skipped", "auto_start": False})

        # 重用 run_all_benchmarks 的逻辑和配置检查
        res = self.run_all_benchmarks()
        result_data: Any = None
        try:
            result_data = getattr(res, "data", None)
        except Exception:
            try:
                # 兼容直接返回 dict 的情况
                if isinstance(res, dict):
                    result_data = res.get("data")
            except Exception:
                result_data = None

        return ok(data={"status": "startup_ran", "auto_start": True, "result": result_data})
