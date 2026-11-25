"""本地 HTTP 后端：ThreadingHTTPServer + JSON 路由。

只监听 127.0.0.1，全程只读。每个请求现读现算，无内存缓存（设计文档 §8）。
路由表与取数函数一一对应；取数失败统一转 {"ok":false,code,message}。

「观测哪个库」是运行时状态（`SourcePool`），不是启动参数：每个请求进来先对
当前库做一次快照，因此一次响应内永远只读一个库；`POST /api/library` 换库后
下一次请求才生效（前端换完再重载视图）。
"""

from __future__ import annotations

import json
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .datasource import SourceError, WebSource
from .libraries import LibraryNotFound, SourcePool

_STATIC_DIR = Path(__file__).parent / "static"


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Handler(BaseHTTPRequestHandler):
    """无状态请求处理：所有数据从本请求开头快照到的那个库现取。"""

    server_version = "HMWebtool/1.0"

    @property
    def pool(self) -> SourcePool:
        return self.server.pool  # type: ignore[attr-defined]

    @property
    def source(self) -> WebSource:
        return self._src  # type: ignore[attr-defined]

    def _snapshot(self) -> None:
        """一次请求只认一个库：切库发生在请求中途也不会读到半新半旧的数据。"""
        self._src = self.pool.source

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
        # 精简访问日志：4s 签名轮询不该刷屏
        if "/api/status" in (self.path or ""):
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
            self._snapshot()
            routes: dict[str, Callable[[dict[str, list[str]]], Any]] = {
                "/api/session": lambda q: self.source.session(),
                "/api/status": lambda q: {
                    **self.source.signatures(),
                    "now": int(time.time() * 1000),
                    "library": self.source.label,
                },
                # 库清单不依赖「当前库」，所以放在基础路由里：库刚被删光时也要能列出来
                "/api/libraries": lambda q: self.pool.listing(),
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
                self._snapshot()
                self._json(self._view_routes()["/api/search-test"](self._read_json()))
                return
            if path == "/api/library":
                # 换库要先于任何取数：换完这一次请求就返回新清单，视图由前端重载
                self._json(self._switch_library(self._read_json()))
                return
            self._json({"ok": False, "code": "not_found", "message": f"未知端点：{path}"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise APIError("bad_json", f"请求体不是合法 JSON：{exc}") from exc

    def _switch_library(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise APIError("bad_json", "请求体必须是 JSON 对象")
        label = (body.get("label") or "").strip() or None
        target = (body.get("target") or "").strip() or None
        if not label and not target:
            raise APIError("missing_param", "需要 label（registry 里的库名）或 target（任意库路径）")
        try:
            return self.pool.switch(label=label, target=target)
        except LibraryNotFound as exc:
            raise APIError("unknown_library", str(exc), 404) from exc

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
    """本地只读服务：进程生命周期由用户掌控（Ctrl+C 退出），无自动探活。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr: tuple[str, int],
        source: WebSource | SourcePool,
    ) -> None:
        # 兼容老调用：给一个 WebSource 就是「单库模式」，池里只有它一条
        self.pool = source if isinstance(source, SourcePool) else SourcePool.from_source(source)
        super().__init__(addr, Handler)


def serve(
    target: str | Path | None = None,
    *,
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """启动本地服务：Ctrl+C 优雅退出。返回进程退出码。

    `target` 省略 = 从 registry 取全部已登记的库，页面右上角下拉切换；
    给了 target 仍然照旧（拖拽一个项目 / token / 库目录 / db 直接看那一个），
    只是它同时也会出现在下拉里，随时能切走。
    """
    pool = SourcePool(target)
    try:
        server = WebServer(("127.0.0.1", port), pool)
    except OSError as exc:
        raise SourceError("port_busy", f"端口 {port} 被占用：{exc}\n换一个：--port <其它>") from exc

    src = pool.source
    url = f"http://127.0.0.1:{port}"
    print(f"记忆观测台已启动：{url}")
    print(f"  当前库：{pool.current_label}")
    print(f"  数据源：{src.db}")
    print(f"  库目录：{src.library_dir}")
    if src.cfg_error:
        print(f"  配置提示：{src.cfg_error}（已退回默认参数）")
    usable = sum(1 for e in pool.entries if e.ok)
    print(f"  可切换的库：{usable} 个（页面右上角下拉选择）")
    for warning in pool.warnings:
        print(f"  提示：{warning}")
    print("  只读模式：不写库、不改活跃度、不写检索日志")
    print("  按 Ctrl+C 退出")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
            pass

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        server.server_close()
    return 0
