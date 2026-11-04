"""配置装配：环境变量 + .env 文件，无额外依赖。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = Path.home() / ".humanity-memory"

# 半衰期默认值（天）：事实几乎不忘，经历慢忘，日志最快
DEFAULT_HALFLIFE = {"fact": 3650.0, "episode": 180.0, "log": 14.0}

# 不同记忆层的默认重要性
DEFAULT_IMPORTANCE = {"fact": 0.80, "episode": 0.40, "log": 0.30}


def parse_env_file(path: Path) -> dict[str, str]:
    """解析极简 .env：KEY=VALUE，支持 # 注释、export 前缀、成对引号。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _collect_env() -> dict[str, str]:
    """优先级：真实环境变量 > 就近 .env > 用户目录 .env。"""
    home = Path(os.environ.get("HM_HOME") or DEFAULT_HOME)
    merged: dict[str, str] = {}
    # 先叠加低优先级（用户目录），再叠加高优先级（当前目录）
    for candidate in (home / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            merged.update(parse_env_file(candidate))
    merged.update({k: v for k, v in os.environ.items() if v != ""})
    return merged


@dataclass(slots=True)
class Config:
    # 存储
    home: Path
    db_path: Path
    default_namespace: str

    # embedding
    embed_base_url: str
    embed_api_key: str
    embed_model: str
    embed_dim: int
    embed_batch: int
    embed_timeout: float

    # 检索权重
    w_retrieval: float
    w_importance: float
    w_strength: float

    # 生命周期
    halflife: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_HALFLIFE))
    archive_strength: float = 0.05
    reinforce_boost: float = 0.20

    # 其它
    vec_extension: str | None = None
    candidate_multiplier: int = 8
    max_candidates: int = 240
    global_facts_warn: int = 50  # 全局事实超过此数量时提示整理

    # ---- 派生属性 ----
    @property
    def embedding_configured(self) -> bool:
        if not self.embed_model or not self.embed_base_url:
            return False
        if self.embed_api_key:
            return True
        # 本地服务通常不需要 key
        host = self.embed_base_url.lower()
        return any(h in host for h in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))

    @property
    def embed_endpoint(self) -> str:
        base = self.embed_base_url.rstrip("/")
        if base.endswith("/embeddings"):
            return base
        return f"{base}/embeddings"

    def ensure_home(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls, environ: dict[str, str] | None = None) -> "Config":
        env = dict(_collect_env() if environ is None else environ)

        def get(key: str, default: str = "") -> str:
            return env.get(key, default) or default

        def get_float(key: str, default: float) -> float:
            try:
                return float(env[key])
            except (KeyError, ValueError):
                return default

        def get_int(key: str, default: int) -> int:
            try:
                return int(env[key])
            except (KeyError, ValueError):
                return default

        home = Path(get("HM_HOME", str(DEFAULT_HOME))).expanduser()
        db_path = Path(get("HM_DB_PATH", str(home / "memory.db"))).expanduser()

        halflife = dict(DEFAULT_HALFLIFE)
        halflife["fact"] = get_float("HM_HALFLIFE_FACT", halflife["fact"])
        halflife["episode"] = get_float("HM_HALFLIFE_EPISODE", halflife["episode"])
        halflife["log"] = get_float("HM_HALFLIFE_LOG", halflife["log"])

        return cls(
            home=home,
            db_path=db_path,
            default_namespace=get("HM_NAMESPACE", "default"),
            embed_base_url=get("HM_EMBED_BASE_URL", ""),
            embed_api_key=get("HM_EMBED_API_KEY", ""),
            embed_model=get("HM_EMBED_MODEL", ""),
            embed_dim=get_int("HM_EMBED_DIM", 1024),
            embed_batch=max(1, get_int("HM_EMBED_BATCH", 32)),
            embed_timeout=get_float("HM_EMBED_TIMEOUT", 30.0),
            w_retrieval=get_float("HM_W_RETRIEVAL", 0.72),
            w_importance=get_float("HM_W_IMPORTANCE", 0.12),
            w_strength=get_float("HM_W_STRENGTH", 0.16),
            halflife=halflife,
            archive_strength=get_float("HM_ARCHIVE_STRENGTH", 0.05),
            reinforce_boost=get_float("HM_REINFORCE_BOOST", 0.20),
            vec_extension=get("HM_VEC_EXTENSION", "") or None,
            global_facts_warn=get_int("HM_GLOBAL_FACTS_WARN", 50),
        )

    def describe(self) -> dict[str, object]:
        """脱敏后的配置摘要，供 doctor / stats 展示。"""
        key = self.embed_api_key
        masked = "not set"
        if key:
            masked = key[:6] + "..." + key[-4:] if len(key) > 12 else "set"
        return {
            "home": str(self.home),
            "db_path": str(self.db_path),
            "namespace": self.default_namespace,
            "embedding": {
                "configured": self.embedding_configured,
                "base_url": self.embed_base_url or "not set",
                "model": self.embed_model or "not set",
                "dim": self.embed_dim,
                "api_key": masked,
            },
            "weights": {
                "retrieval": self.w_retrieval,
                "importance": self.w_importance,
                "strength": self.w_strength,
            },
            "halflife_days": self.halflife,
            "archive_threshold": self.archive_strength,
        }
