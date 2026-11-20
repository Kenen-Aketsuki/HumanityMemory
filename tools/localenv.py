"""读取项目根的 `local.env` —— 这台机器的路径配置，不入库。

为什么单独一个文件而不是写死在脚本里：发布根、开发工作区、扫描根都是
「某台机器长什么样」，写进源码等于把本机目录结构提交上去。仓库里只留
`local.env.example` 作模板。

优先级：进程环境变量 > local.env > 调用方给的默认值。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ENV = ROOT / "local.env"


def read_file() -> dict[str, str]:
    """解析 local.env（KEY=VALUE，# 注释，允许值带引号）。文件缺失返回空表。"""
    try:
        text = LOCAL_ENV.read_text(encoding="utf-8")
    except OSError:
        return {}
    data: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def get(name: str, default: str | None = None) -> str | None:
    """环境变量优先，其次 local.env，最后 default。空白值视为未设置。"""
    from_env = os.environ.get(name, "").strip()
    if from_env:
        return from_env
    from_file = read_file().get(name, "").strip()
    return from_file or default


def get_path(name: str, default: Path) -> Path:
    """取一个路径配置，未配置时用 default。"""
    return Path(get(name) or default).expanduser()


def get_list(name: str) -> list[str]:
    """取逗号分隔的多值配置，忽略空项。"""
    raw = get(name) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]
