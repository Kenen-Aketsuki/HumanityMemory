"""配置装配：环境变量 + .env 文件，无额外依赖。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = Path.home() / ".humanity-memory"

# 半衰期默认值（天）。陈述层几乎不忘，经历慢忘，日志最快。
# state 与 knowledge 同半衰期 —— 现状不是"衰减"掉的，是被显式更新掉的。
DEFAULT_HALFLIFE = {
    "knowledge": 3650.0,
    "state": 3650.0,
    "case": 180.0,
    "narrative": 180.0,
    "log": 14.0,
}

# 不同 kind 的默认重要性。case 是压过的定论，起点就该比叙述高。
DEFAULT_IMPORTANCE = {
    "knowledge": 0.80,
    "state": 0.85,
    "case": 0.75,
    "narrative": 0.40,
    "log": 0.30,
}

# 归档判据：超过 ttl 天没被碰过、且访问次数不超过 max_access → 归档。
#   knowledge 3 年；state 只有 30 天（现状会变，且续命必须显式确认）；
#   case / narrative 1 年。都不自动删除，只置 archived_at。
DEFAULT_ARCHIVE_TTL = {
    "knowledge": 1095.0,
    "state": 30.0,
    "case": 365.0,
    "narrative": 365.0,
}
DEFAULT_ARCHIVE_MAX_ACCESS = 1

# 经历的渐进压缩：刚复盘是完整叙述（level 0），随时间逐级变短
# level 0 → 1 → 2 → 3（归档时），触发门槛见下
DEFAULT_RECOMPRESS_DAYS = {"1": 30.0, "2": 180.0}


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


# 唯一的 .env 来源：相对本文件定位到仓库根，避免把机器绝对路径写进源码。
# 本包以 editable 方式安装，config.py 位于 <仓库根>/src/humanity_memory/ 下，
# parents[2] 即仓库根；仓库整体搬走时无需改任何代码。
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _collect_env() -> dict[str, str]:
    """优先级：真实环境变量 > ENV_FILE（唯一的 .env 来源）。

    只读 ENV_FILE 这一个文件，不再叠加 ~/.humanity-memory/.env 或 cwd/.env：
    配置加载与启动目录彻底解耦，避免 cwd 漂移让凭据静默失效。
    真实环境变量仍作为最高优先级保留 —— 令牌注入、mcp.json 的 env、
    CLI / 工具的显式覆盖都依赖这一层。
    """
    merged = parse_env_file(ENV_FILE)
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
    archive_ttl: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_ARCHIVE_TTL))
    archive_max_access: int = DEFAULT_ARCHIVE_MAX_ACCESS
    purge_after_days: float = 180.0
    recompress_days: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_RECOMPRESS_DAYS)
    )
    reinforce_boost: float = 0.20

    # 其它
    vec_extension: str | None = None
    candidate_multiplier: int = 8
    max_candidates: int = 240
    global_knowledge_warn: int = 50  # 全局条目超过此数量时提示整理

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
        halflife["knowledge"] = get_float("HM_HALFLIFE_KNOWLEDGE", halflife["knowledge"])
        halflife["state"] = get_float("HM_HALFLIFE_STATE", halflife["state"])
        halflife["case"] = get_float("HM_HALFLIFE_CASE", halflife["case"])
        halflife["narrative"] = get_float("HM_HALFLIFE_EPISODE", halflife["narrative"])
        halflife["log"] = get_float("HM_HALFLIFE_LOG", halflife["log"])

        archive_ttl = {
            "knowledge": get_float(
                "HM_ARCHIVE_TTL_KNOWLEDGE", DEFAULT_ARCHIVE_TTL["knowledge"]
            ),
            "state": get_float("HM_ARCHIVE_TTL_STATE", DEFAULT_ARCHIVE_TTL["state"]),
            "case": get_float("HM_ARCHIVE_TTL_CASE", DEFAULT_ARCHIVE_TTL["case"]),
            "narrative": get_float(
                "HM_ARCHIVE_TTL_EPISODE", DEFAULT_ARCHIVE_TTL["narrative"]
            ),
        }

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
            archive_ttl=archive_ttl,
            archive_max_access=get_int("HM_ARCHIVE_MAX_ACCESS", DEFAULT_ARCHIVE_MAX_ACCESS),
            purge_after_days=get_float("HM_PURGE_AFTER_DAYS", 180.0),
            recompress_days={
                "1": get_float("HM_RECOMPRESS_L0_DAYS", DEFAULT_RECOMPRESS_DAYS["1"]),
                "2": get_float("HM_RECOMPRESS_L1_DAYS", DEFAULT_RECOMPRESS_DAYS["2"]),
            },
            reinforce_boost=get_float("HM_REINFORCE_BOOST", 0.20),
            vec_extension=get("HM_VEC_EXTENSION", "") or None,
            global_knowledge_warn=get_int(
                "HM_GLOBAL_KNOWLEDGE_WARN", get_int("HM_GLOBAL_FACTS_WARN", 50)
            ),
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
            "archive_ttl_days": self.archive_ttl,
            "archive_max_access": self.archive_max_access,
            "purge_after_days": self.purge_after_days,
            "recompress_days": self.recompress_days,
        }
