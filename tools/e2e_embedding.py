"""embedding 客户端的异常路径验证：限速退避、维度校验、认证失败、服务不可达。

正常链路由 verify_mcp_config.py 用真实端点覆盖；这里专测出错时会怎样，
用本地 mock 服务，不消耗任何 API 额度。
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 64
STATE = {"rate_limit_left": 0}


def fake_embedding(text: str) -> list[float]:
    chars = [c for c in text if not c.isspace()]
    grams = chars if len(chars) < 3 else [text[i : i + 2] for i in range(len(chars) - 1)]
    vec = [0.0] * DIM
    for gram in grams:
        digest = hashlib.md5(gram.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % DIM
        vec[idx] += -1.0 if digest[4] % 2 else 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        if STATE["rate_limit_left"] > 0:
            STATE["rate_limit_left"] -= 1
            self._send(429, {"error": {"message": "rate limit exceeded (simulated)"}})
            return

        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "bad json"}})
            return

        if not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send(401, {"error": {"message": "missing token"}})
            return

        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        if not isinstance(inputs, list):
            self._send(400, {"error": {"message": "input must be array"}})
            return

        data = [
            {"index": i, "embedding": fake_embedding(t)} for i, t in enumerate(inputs)
        ]
        self._send(200, {"object": "list", "data": data, "model": body.get("model", "mock")})

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def make_config(base_url: str, dim: int, key: str = "test-key"):
    from humanity_memory.config import Config

    return Config.load(
        {
            "HM_HOME": "C:/Users/Public/hm-embed-test",
            "HM_EMBED_BASE_URL": base_url,
            "HM_EMBED_MODEL": "mock-embed",
            "HM_EMBED_DIM": str(dim),
            "HM_EMBED_API_KEY": key,
            "HM_EMBED_BATCH": "8",
            "HM_EMBED_TIMEOUT": "5",
        }
    )


def main() -> int:
    from humanity_memory.embeddings import EmbeddingError, EmbeddingProvider

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/v1"
    print(f"mock 服务: {base}")
    failures = 0

    print("\n[1] 正常调用与自动分批")
    provider = EmbeddingProvider(make_config(base, DIM))
    vec = provider.embed_one("正常文本")
    print(f"    单条 dim={len(vec)}")
    batch = provider.embed([f"文本{i}" for i in range(9)])
    print(f"    批量 {len(batch)} 条（batch=8，应自动分两批）")
    if len(batch) != 9:
        failures += 1
        print("    !! 批量条数不符")
    provider.close()

    print("\n[2] 维度不匹配应报错")
    bad = EmbeddingProvider(make_config(base, DIM * 2))
    try:
        bad.embed_one("维度检查")
        print("    !! 维度不符却成功了")
        failures += 1
    except EmbeddingError as exc:
        print(f"    正确报错：{str(exc)[:70]}…")
    bad.close()

    print("\n[3] 429 应退避重试而非直接降级")
    STATE["rate_limit_left"] = 1
    retry = EmbeddingProvider(make_config(base, DIM))
    started = time.time()
    try:
        vec = retry.embed_one("限速重试")
        elapsed = time.time() - started
        print(f"    重试后成功：dim={len(vec)} 耗时={elapsed:.1f}s")
        if elapsed < 1.5:
            failures += 1
            print("    !! 耗时过短，退避没生效")
    except EmbeddingError as exc:
        failures += 1
        print(f"    !! 限速未重试：{exc}")
    retry.close()

    print("\n[4] 401 应快速失败，不浪费重试")
    nc = EmbeddingProvider(make_config(base, DIM, key=""))
    nc.cfg.embed_api_key = ""
    nc.cfg.embed_base_url = base  # 非 localhost，必须带 key
    started = time.time()
    try:
        nc.embed_one("没有 key")
        print("    !! 没 key 竟然成功")
        failures += 1
    except EmbeddingError as exc:
        elapsed = time.time() - started
        print(f"    正确失败（{elapsed:.1f}s）：{str(exc)[:52]}…")
        if elapsed > 2.0:
            failures += 1
            print("    !! 4xx 不该重试")
    nc.close()

    print("\n[5] 服务不可达应优雅降级")
    dead = EmbeddingProvider(make_config("http://127.0.0.1:1/v1", DIM))
    dead.cfg.embed_timeout = 1.0
    try:
        dead.embed_one("连不上")
        print("    !! 竟然成功")
        failures += 1
    except EmbeddingError as exc:
        print(f"    正确失败：{str(exc)[:56]}…")
    print(f"    try_embed 降级返回：{dead.try_embed(['x'])}")
    dead.close()

    server.shutdown()
    print()
    print("EMBEDDING E2E:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
