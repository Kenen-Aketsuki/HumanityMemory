"""PyInstaller 的入口脚本（仅打包用，不参与运行时逻辑）。

`humanity_memory/cli.py` 里用的是相对导入，不能直接当脚本喂给 PyInstaller，
所以这里做一层最薄的转发。
"""

from __future__ import annotations

import sys

from humanity_memory.cli import main

if __name__ == "__main__":
    sys.exit(main())
