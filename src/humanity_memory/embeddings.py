"""Embedding provider：OpenAI 兼容 /v1/embeddings。

兼容目标：OpenAI、硅基流动、DeepSeek、Ollama、LM Studio、vLLM、Xinference
等一切实现了该接口的服务。任何失败都不应该让记忆功能整体不可用 ——
上层拿到异常后退化为纯关键词检索。
"""

from __future__ import annotations

import time
from typing import Sequence

import httpx

from .config import Config

MAX_INPUT_CHARS = 6_000  # bge-m3 上下文 8192 token，中文约 1 字≈1 token，留余量防超长


class EmbeddingError(RuntimeError):
    """embedding 调用失败（配置错误、网络、限流、维度不符等）。"""


class EmbeddingProvider:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client: httpx.Client | None = None
        self._verified = False

    # ---- 生命周期 ----
    @property
    def available(self) -> bool:
        return self.cfg.embedding_configured

    @property
    def name(self) -> str:
        return self.cfg.embed_model or "(unconfigured)"

    def _client_or_create(self) -> httpx.Client:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.cfg.embed_api_key:
                headers["Authorization"] = f"Bearer {self.cfg.embed_api_key}"
            self._client = httpx.Client(
                timeout=self.cfg.embed_timeout,
                headers=headers,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- 主入口 ----
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.available:
            raise EmbeddingError(
                "未配置 embedding 服务。请设置 HM_EMBED_BASE_URL / HM_EMBED_MODEL"
                "（远程服务还需 HM_EMBED_API_KEY），并确认 HM_EMBED_DIM 与模型一致。"
            )
        items = [self._prepare(t) for t in texts]
        if not items:
            return []

        vectors: list[list[float]] = []
        batch = max(1, self.cfg.embed_batch)
        for start in range(0, len(items), batch):
            vectors.extend(self._request(items[start : start + batch]))
        return vectors

    def embed_one(self, text: str) -> list[float]:
        out = self.embed([text])
        if not out:
            raise EmbeddingError("embedding 返回为空")
        return out[0]

    def try_embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        """失败即返回 None，供上层降级到关键词检索。"""
        if not self.available:
            return None
        try:
            return self.embed(texts)
        except EmbeddingError:
            return None

    def probe(self) -> dict[str, object]:
        """探活：真发一次请求，返回可用性与真实维度。"""
        if not self.available:
            return {"ok": False, "reason": "not configured"}
        started = time.perf_counter()
        try:
            vec = self.embed_one("dimension probe")
        except EmbeddingError as exc:
            return {"ok": False, "reason": str(exc)}
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "ok": len(vec) == self.cfg.embed_dim,
            "dim": len(vec),
            "expected_dim": self.cfg.embed_dim,
            "latency_ms": round(elapsed, 1),
            "model": self.name,
        }

    # ---- 内部 ----
    @staticmethod
    def _prepare(text: str) -> str:
        text = (text or "").strip()
        if not text:
            text = "(empty)"
        return text[:MAX_INPUT_CHARS]

    def _request(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.cfg.embed_model, "input": batch}
        last_error: Exception | None = None

        for attempt in range(3):
            delay = 0.6 * (2**attempt)
            try:
                resp = self._client_or_create().post(self.cfg.embed_endpoint, json=payload)
            except httpx.HTTPError as exc:
                last_error = EmbeddingError(f"连接 embedding 服务失败：{exc}")
            else:
                if resp.status_code == 200:
                    return self._parse(resp.json(), len(batch))
                last_error = EmbeddingError(self._explain_http(resp))
                if resp.status_code == 429:
                    # 免费额度/限速场景：退避久一点，别一次撞墙就降级
                    delay = 2.0 * (2**attempt)
                elif resp.status_code < 500:
                    # 401/403/404/400 这类重试也不会好
                    break

            if attempt == 2:
                break
            time.sleep(delay)

        raise last_error or EmbeddingError("embedding 调用失败")

    def _parse(self, body: object, expected: int) -> list[list[float]]:
        if not isinstance(body, dict) or "data" not in body:
            raise EmbeddingError(f"embedding 响应格式异常：{str(body)[:200]}")
        entries = body["data"]
        if not isinstance(entries, list):
            raise EmbeddingError("embedding 响应 data 字段不是数组")
        entries = sorted(entries, key=lambda e: e.get("index", 0) if isinstance(e, dict) else 0)

        vectors: list[list[float]] = []
        for entry in entries:
            if not isinstance(entry, dict) or "embedding" not in entry:
                raise EmbeddingError("embedding 响应缺少 embedding 字段")
            vec = [float(x) for x in entry["embedding"]]
            if len(vec) != self.cfg.embed_dim:
                raise EmbeddingError(
                    f"向量维度不匹配：模型返回 {len(vec)} 维，配置 HM_EMBED_DIM={self.cfg.embed_dim}。"
                    f"请把 HM_EMBED_DIM 改为 {len(vec)}；若库中已有数据，"
                    "还需运行 `humanity-memory reindex --reset-vectors` 重新嵌入。"
                )
            vectors.append(vec)

        if len(vectors) != expected:
            raise EmbeddingError(f"embedding 返回条数不符：期望 {expected}，实际 {len(vectors)}")
        return vectors

    @staticmethod
    def _explain_http(resp: httpx.Response) -> str:
        body = resp.text[:300]
        if resp.status_code in (401, 403):
            return (
                f"embedding 认证失败（HTTP {resp.status_code}）。请检查 HM_EMBED_API_KEY。"
                f"服务端返回：{body}"
            )
        if resp.status_code == 404:
            return (
                f"embedding 端点不存在（HTTP 404）。请检查 HM_EMBED_BASE_URL 是否以 /v1 结尾，"
                f"以及 HM_EMBED_MODEL 是否为该服务支持的模型名。返回：{body}"
            )
        if resp.status_code == 429:
            return f"embedding 被限流（HTTP 429）。免费模型通常有 RPM 限制，已自动退避重试。返回：{body}"
        if resp.status_code >= 500:
            return f"embedding 服务端错误（HTTP {resp.status_code}），可重试。返回：{body}"
        return f"embedding 调用失败（HTTP {resp.status_code}）。返回：{body}"
