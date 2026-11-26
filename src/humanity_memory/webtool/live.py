"""把老的星图 / 检索单文件模板改造成「启动时从 API 取数」的实时页面。

老模板（starmap._HTML_TEMPLATE / retrieval_view._TEMPLATE）的渲染 JS 是成熟的、
经过验证的：力导向拖拽缩放、悬停全文、边/节点筛选、热度榜、自动诊断等等。
webtool 不重写这套逻辑，而是在运行时把模板里的数据注入点 `/*__DATA__*/`
换成一个异步 loader：fetch API → 得到 DATA → 再执行原脚本主体。

这样老 CLI 报告和新 HTTP 工具共用同一份渲染代码，UI 与交互零漂移。
"""

from __future__ import annotations

import json

from .. import retrieval_view, starmap


def _wrap_live(template: str, api_path: str, *, unwrap: str | None = None) -> str:
    """把 `<script>/*__DATA__*/ ... </script>` 改成 async 取数后执行。

    ``unwrap`` 是响应里真正要作为 DATA 的键（星图是 ``graph``；检索响应本身即 DATA）。

    老模板的数据注入点形如：
        <script>
        /*__DATA__*/
        <渲染主体>（星图是 IIFE；检索是裸语句）
        </script>

    注入点被替换成一段 async 启动器，渲染主体整个搬进同一个 async 作用域，
    于是主体里的 `DATA` 就是 fetch 到的实时数据，其余代码原封不动。
    """
    marker = "/*__DATA__*/"
    if marker not in template:
        raise ValueError("模板缺少数据注入点 " + marker)
    before, after = template.split(marker, 1)

    boot = (
        "(async()=>{\n"
        "  let DATA;\n"
        "  try{\n"
        "    const resp = await fetch(" + json.dumps(api_path) + ",{cache:'no-store'});\n"
        "    let payload = await resp.json();\n"
        "    if(payload && payload.ok===false) throw new Error(payload.message||payload.code);\n"
        + (f"    DATA = payload[{json.dumps(unwrap)}];\n" if unwrap else "    DATA = payload;\n") +
        "  }catch(e){\n"
        "    document.body.insertAdjacentHTML('afterbegin',\n"
        "      '<div style=\"padding:16px;color:#f08a8a;font:13px sans-serif\">'+\n"
        "      '数据加载失败：'+String(e.message||e)+'</div>');\n"
        "    return;\n"
        "  }\n"
        # 下面是原模板的渲染主体（引用外层 DATA），最后需要闭合 async 函数。
    )
    # 找到主体后面的 </script>，在其前面闭合 async 函数
    close_tag = "</script>"
    idx = after.find(close_tag)
    if idx < 0:
        raise ValueError("模板缺少 </script>")
    body = after[:idx]
    tail = after[idx:]
    # 主体可能是 `(function(){...})();` 后带换行；追加闭合括号
    wrapped = before + boot + body + "\n})();\n" + tail
    return wrapped


def starmap_live_html() -> str:
    """星图实时页面：数据来自 /api/starmap 的 graph。"""
    return _wrap_live(starmap._HTML_TEMPLATE, "/api/starmap", unwrap="graph")


def retrieval_live_html() -> str:
    """检索分析实时页面：数据来自 /api/recall（结构即老报告的 DATA）。"""
    return _wrap_live(retrieval_view._TEMPLATE, "/api/recall")
