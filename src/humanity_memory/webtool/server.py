"""本地 HTTP 后端：ThreadingHTTPServer + JSON 路由。

只监听 127.0.0.1，全程只读。每个请求现读现算，无内存缓存（设计文档 §8）。
路由表与取数函数一一对应；取数失败统一转 {"ok":false,code,message}。
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .datasource import SourceError, WebSource

_STATIC_DIR = Path(__file__).parent / "static"

# 心跳默认值（秒）。可见页 3s 发一次、10s 无心跳就退出；后台标签页被浏览器
# 节流到分钟级，放宽到 60s；启动宽限 60s 等浏览器打开，避免刚启动就被杀。
DEFAULT_HEARTBEAT_TIMEOUT = 10.0
DEFAULT_HIDDEN_GRACE = 60.0
DEFAULT_STARTUP_GRACE = 60.0
_HEARTBEAT_INTERVAL_MS = 3000


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Handler(BaseHTTPRequestHandler):
    """无状态请求处理：所有数据从 self.server.source 现取。"""

    server_version = "HMWebtool/1.0"

    @property
    def source(self) -> WebSource:
        return self.server.source  # type: ignore[attr-defined]

    # ---- 基础响应 ----

    def _send(self, status: int, payload: Any, *, content_type: str = "application/json; charset=utf-8") -> None:
        if content_type.startswith("application/json"):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, payload)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, SourceError):
            self._json(exc.to_dict(), 503)
        elif isinstance(exc, APIError):
            self._json({"ok": False, "code": exc.code, "message": exc.message}, exc.status)
        else:
            self._json(
                {"ok": False, "code": "internal", "message": f"{type(exc).__name__}: {exc}"},
                500,
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        # 精简访问日志：4s 签名轮询 + 3s 心跳不该刷屏
        if "/api/status" in (self.path or "") or "/api/heartbeat" in (self.path or ""):
            return
        super().log_message(fmt, *args)

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path in ("/", "/index.html"):
                self._serve_index()
                return
            if path in ("/starmap.html", "/recall.html"):
                # 老工具的完整渲染逻辑，启动时从 API 取实时数据（设计见 webtool/live.py）
                from . import live

                html = live.starmap_live_html() if path == "/starmap.html" else live.retrieval_live_html()
                self._send(200, html.encode("utf-8"), content_type="text/html; charset=utf-8")
                return
            if path in ("/views.js", "/style.css"):
                target = _STATIC_DIR / path.lstrip("/")
                if target.is_file():
                    ctype = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                    self._send(200, target.read_bytes(), content_type=ctype)
                    return
            routes: dict[str, Callable[[dict[str, list[str]]], Any]] = {
                "/api/session": lambda q: self.source.session(),
                "/api/status": lambda q: {**self.source.signatures(), "now": int(time.time() * 1000)},
                # 心跳只 touch 时间戳，不碰库、不算签名 —— 每 3s 调用也要足够轻
                "/api/heartbeat": lambda q: self._heartbeat(q),
            }
            # 延迟导入：各视图模块到对应阶段才建，未建时返回明确错误
            extra = self._view_routes()
            routes.update(extra)
            if path in routes:
                qs = parse_qs(parsed.query, keep_blank_values=True)
                self._json(routes[path](qs))
                return
            self._json({"ok": False, "code": "not_found", "message": f"未知端点：{path}"}, 404)
        except Exception as exc:  # noqa: BLE001 - 统一错误契约
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if path == "/api/search-test":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise APIError("bad_json", f"请求体不是合法 JSON：{exc}") from exc
                self._json(self._view_routes()["/api/search-test"](body))
                return
            self._json({"ok": False, "code": "not_found", "message": f"未知端点：{path}"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _heartbeat(self, qs: dict[str, list[str]]) -> dict[str, Any]:
        """前端心跳：刷新存活时间戳，并回传当前宽限窗口供 UI 显示。

        `visible=0` 表示页面在后台（浏览器会节流定时器），后端改用更长的宽限期。
        只有这个端点会 touch —— 状态轮询 / 检索请求失败时，心跳仍独立生效。
        """
        visible = (qs.get("visible") or ["1"])[0] != "0"
        return self.server.touch(visible=visible)  # type: ignore[attr-defined]

    def _view_routes(self) -> dict[str, Callable[[Any], Any]]:
        """各阶段视图路由；模块不存在时给出明确提示而不是 500。"""
        routes: dict[str, Callable[[Any], Any]] = {}
        try:
            from . import starmap_view, recall_view, review, memory_state, simulate
        except ImportError as exc:
            # 未实现的阶段：返回占位，前端显示"待接入"而不是崩
            def not_ready(q: Any) -> dict[str, Any]:
                return {"ok": False, "code": "not_ready", "message": f"视图尚未接入：{exc}"}

            for p in (
                "/api/starmap", "/api/recall", "/api/review", "/api/memory", "/api/search-test"
            ):
                routes[p] = not_ready
            return routes

        routes["/api/starmap"] = lambda q: starmap_view.get(self.source)
        routes["/api/recall"] = lambda q: recall_view.get(self.source, q)
        routes["/api/review"] = lambda q: review.get(self.source)
        routes["/api/memory"] = lambda q: memory_state.get(self.source)
        routes["/api/search-test"] = lambda b: simulate.post(self.source, b)
        return routes

    def _serve_index(self) -> None:
        index = _STATIC_DIR / "index.html"
        if not index.is_file():
            self._json(
                {"ok": False, "code": "no_frontend", "message": "前端页面缺失"}, 404
            )
            return
        self._send(200, index.read_bytes(), content_type="text/html; charset=utf-8")


class WebServer(ThreadingHTTPServer):
    """带心跳租约的本地服务：前端断线后自动 shutdown，不留僵尸进程。

    租约状态只在内存里，不落盘、不进库 —— 观测台对记忆库仍是纯只读。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr: tuple[str, int],
        source: WebSource,
        *,
        heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT,
        hidden_grace: float = DEFAULT_HIDDEN_GRACE,
        startup_grace: float = DEFAULT_STARTUP_GRACE,
    ) -> None:
        self.source = source
        # timeout <= 0 表示关闭自动退出（调试用）；watchdog 不会启动
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.hidden_grace = max(float(hidden_grace), self.heartbeat_timeout)
        self.startup_grace = max(float(startup_grace), 1.0)
        self._hb_lock = threading.Lock()
        self._last_seen = time.monotonic()
        # 启动期先用宽限期当租约：浏览器还没打开时不能按 10s 判死
        self._lease = self.startup_grace
        self._activated = False
        super().__init__(addr, Handler)

    def touch(self, *, visible: bool) -> dict[str, Any]:
        """收到一次心跳：刷新最后存活时间与本次租约长度。"""
        with self._hb_lock:
            self._last_seen = time.monotonic()
            self._lease = self.heartbeat_timeout if visible else self.hidden_grace
            self._activated = True
            remaining = max(0.0, self._last_seen + self._lease - time.monotonic())
            return {
                "ok": True,
                "now": int(time.time() * 1000),
                "interval_ms": _HEARTBEAT_INTERVAL_MS,
                "timeout": round(self._lease, 1),
                "remaining": round(remaining, 1),
            }

    def heartbeat_status(self) -> dict[str, Any]:
        """只读快照，便于测试与排查（不改变 last_seen）。"""
        with self._hb_lock:
            remaining = max(0.0, self._last_seen + self._lease - time.monotonic())
            return {
                "activated": self._activated,
                "timeout": self._lease,
                "remaining": round(remaining, 2),
            }

    def start_watchdog(self) -> threading.Thread | None:
        """起守护线程盯租约；超时后从线程里调 shutdown()（不能在主线程自shutdown）。"""
        if self.heartbeat_timeout <= 0:
            return None

        def loop() -> None:
            while True:
                time.sleep(0.25)
                with self._hb_lock:
                    # 启动期的 lease 就是 startup_grace，第一个心跳会把它换成正式超时，
                    # 不需要额外分支 —— 否则浏览器一直没打开时反而永远不退出。
                    idle = time.monotonic() - self._last_seen
                    lease = self._lease
                if idle > lease:
                    print(f"\n前端心跳中断超过 {lease:g}s，观测台自动退出。", flush=True)
                    self.shutdown()
                    return

        thread = threading.Thread(target=loop, name="hm-heartbeat-watchdog", daemon=True)
        thread.start()
        return thread


def serve(
    target: str | Path | None,
    *,
    port: int = 8765,
    open_browser: bool = True,
    heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT,
    hidden_grace: float = DEFAULT_HIDDEN_GRACE,
    startup_grace: float = DEFAULT_STARTUP_GRACE,
) -> int:
    """启动本地服务：Ctrl+C 或前端心跳超时都会优雅退出。返回进程退出码。"""
    source = WebSource(target)
    try:
        server = WebServer(
            ("127.0.0.1", port),
            source,
            heartbeat_timeout=heartbeat_timeout,
            hidden_grace=hidden_grace,
            startup_grace=startup_grace,
        )
    except OSError as exc:
        raise SourceError("port_busy", f"端口 {port} 被占用：{exc}\n换一个：--port <其它>") from exc

    url = f"http://127.0.0.1:{port}"
    print(f"记忆观测台已启动：{url}")
    print(f"  数据源：{source.db}")
    print(f"  库目录：{source.library_dir}")
    if source.cfg_error:
        print(f"  配置提示：{source.cfg_error}（已退回默认参数）")
    print("  只读模式：不写库、不改活跃度、不写检索日志")
    if server.heartbeat_timeout > 0:
        print(
            f"  心跳探活：可见页 {server.heartbeat_timeout:g}s / 后台页 "
            f"{server.hidden_grace:g}s 无心跳自动退出；启动宽限 {server.startup_grace:g}s"
        )
    else:
        print("  心跳探活：已关闭（--heartbeat-timeout 0）")
    print("  按 Ctrl+C 退出")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
            pass

    server.start_watchdog()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        server.server_close()
    return 0
