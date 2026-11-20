"""内置资源定位：源码态与冻结态（PyInstaller）用同一套回答。

为什么单独抽出来
----------------
`schema.sql` 这类随包发布的文件，以前靠 `Path(__file__).with_name(...)` 找。
源码态成立，但 PyInstaller 冻结后代码被打进 exe / `_internal`，`__file__` 不再是
那个目录，路径必然错位。把「内置资源在哪」收敛到这一个函数，别处不再自己推路径。

打包约定
--------
资源必须以「包名前缀」放进 `_internal/`，即：

    --add-data "src/humanity_memory/schema.sql;humanity_memory"

所以冻结态要多一层 `humanity_memory/`；源码态本文件就在包目录下，直接同目录取。
两者少了任何一半，都会得到 `<目录>/<目录>/schema.sql` 这种不存在的路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 冻结态 onedir 的结构：<exe 同级>/_internal/humanity_memory/<资源>
_PACKAGE_DIR = "humanity_memory"


def resource(name: str) -> Path:
    """返回包内资源文件的路径（不检查存在性，由调用方给出上下文更准的报错）。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / _PACKAGE_DIR
    else:
        base = Path(__file__).parent
    return base / name
