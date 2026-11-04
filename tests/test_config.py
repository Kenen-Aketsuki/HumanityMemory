"""配置装配：.env 解析与优先级。"""

from __future__ import annotations

from pathlib import Path

from humanity_memory.config import Config, parse_env_file


def test_parse_env_basic(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "KEY_A=value\n"
        "export KEY_B='quoted value'\n"
        'KEY_C="double"\n'
        "\n"
        "NO_EQUALS\n",
        encoding="utf-8",
    )
    parsed = parse_env_file(path)
    assert parsed == {"KEY_A": "value", "KEY_B": "quoted value", "KEY_C": "double"}


def test_parse_env_missing_file(tmp_path: Path) -> None:
    assert parse_env_file(tmp_path / "nope.env") == {}


def test_config_defaults(tmp_path: Path) -> None:
    cfg = Config.load({"HM_DB_PATH": str(tmp_path / "m.db")})
    assert cfg.db_path == tmp_path / "m.db"
    assert cfg.default_namespace == "default"
    assert cfg.embed_dim == 1024
    assert cfg.w_retrieval == 0.72


def test_embedding_configured_remote_requires_key(tmp_path: Path) -> None:
    cfg = Config.load(
        {
            "HM_DB_PATH": str(tmp_path / "m.db"),
            "HM_EMBED_BASE_URL": "https://api.openai.com/v1",
            "HM_EMBED_MODEL": "text-embedding-3-small",
        }
    )
    assert cfg.embedding_configured is False


def test_embedding_configured_local_without_key(tmp_path: Path) -> None:
    for host in ("http://127.0.0.1:11434/v1", "http://localhost:1234/v1"):
        cfg = Config.load(
            {
                "HM_DB_PATH": str(tmp_path / "m.db"),
                "HM_EMBED_BASE_URL": host,
                "HM_EMBED_MODEL": "bge-m3",
            }
        )
        assert cfg.embedding_configured is True, host


def test_embedding_configured_with_key(tmp_path: Path) -> None:
    cfg = Config.load(
        {
            "HM_DB_PATH": str(tmp_path / "m.db"),
            "HM_EMBED_BASE_URL": "https://api.siliconflow.cn/v1",
            "HM_EMBED_MODEL": "BAAI/bge-m3",
            "HM_EMBED_API_KEY": "sk-x",
        }
    )
    assert cfg.embedding_configured is True


def test_embed_endpoint_normalisation(tmp_path: Path) -> None:
    cfg = Config.load(
        {"HM_DB_PATH": str(tmp_path / "m.db"), "HM_EMBED_BASE_URL": "http://h/v1/"}
    )
    assert cfg.embed_endpoint == "http://h/v1/embeddings"

    cfg2 = Config.load(
        {"HM_DB_PATH": str(tmp_path / "m.db"), "HM_EMBED_BASE_URL": "http://h/v1/embeddings"}
    )
    assert cfg2.embed_endpoint == "http://h/v1/embeddings"


def test_describe_masks_key(tmp_path: Path) -> None:
    cfg = Config.load(
        {
            "HM_DB_PATH": str(tmp_path / "m.db"),
            "HM_EMBED_API_KEY": "sk-abcdefghijklmnop",
            "HM_EMBED_BASE_URL": "https://x/v1",
            "HM_EMBED_MODEL": "m",
        }
    )
    masked = cfg.describe()["embedding"]["api_key"]
    assert "abcdefghijkl" not in masked
    assert masked.startswith("sk-abc")
