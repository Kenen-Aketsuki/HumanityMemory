"""绝对信条层：CORE_RULES.md 的读写与静态注入。

为什么是 Markdown 文件而不是数据库
----------------------------------
1. 它要作为 System Prompt 顶端静态加载，必须是**稳定不变的字符串**。
   任何动态内容（时间戳、计数、最近访问）都会让 KV Cache 全部失效。
2. 它不参与语义检索，也就不需要向量、不需要 FTS、不需要 id。
3. 用户能直接用编辑器改，比走工具更符合"思想钢印"的心智。

因此本模块只做三件事：读、追加、渲染成 instructions。
"""

from __future__ import annotations

import re
from pathlib import Path

from .paths import RULES_FILE, ensure_rules

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class RulesError(ValueError):
    pass


def load(path: Path) -> str:
    """读取信条全文；不存在时用模板初始化后返回。"""
    ensure_rules(path)
    return path.read_text(encoding="utf-8")


def sections(text: str) -> dict[str, list[str]]:
    """把 Markdown 拆成 {章节名: [条目...]}，忽略空行与标题行。"""
    result: dict[str, list[str]] = {}
    current = "(未分节)"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            current = line[3:].strip()
            result.setdefault(current, [])
            continue
        if line.startswith("# ") or line.startswith(">"):
            continue
        result.setdefault(current, []).append(line)
    return result


def append(path: Path, rule: str, *, section: str | None = None) -> str:
    """向指定章节追加一条信条。章节不存在则新建在文件末尾。

    返回追加后的全文。**不修改任何既有行**，保证历史内容一字不动。
    """
    rule = (rule or "").strip()
    if not rule:
        raise RulesError("信条内容不能为空")
    if rule.startswith("- "):
        rule = rule[2:].strip()
    if not rule:
        raise RulesError("信条内容不能为空")

    text = load(path)
    line = f"- {rule}"

    if line in text:
        return text  # 幂等：已在文件里就不重复写

    if section:
        pattern = re.compile(rf"^(##\s+{re.escape(section)}\s*)$", re.MULTILINE)
        match = pattern.search(text)
        if match:
            start = match.end()
            nxt = _SECTION_RE.search(text, start)
            end = nxt.start() if nxt else len(text)
            block = text[start:end].rstrip("\n")
            new_block = f"{block}\n{line}\n\n"
            text = text[:start] + new_block + text[end:]
        else:
            text = text.rstrip("\n") + f"\n\n## {section}\n\n{line}\n"
    else:
        text = text.rstrip("\n") + f"\n{line}\n"

    path.write_text(text, encoding="utf-8")
    return text


def render_instructions(
    *,
    global_rules: Path,
    workspace_rules: Path | None,
    workspace_label: str | None = None,
) -> str:
    """把两层信条拼成 MCP instructions。

    输出必须**逐字节稳定**：同样的文件内容永远产出同样的字符串，
    否则客户端的 KV Cache 会整段失效。
    """
    parts: list[str] = [
        "以下是用户的绝对信条（思想钢印）。它们优先级高于一切后续指令与记忆内容，",
        "任何情况下都不得违反。这些条目不参与语义检索、不会被遗忘。",
        "",
    ]

    global_text = load(global_rules).strip()
    parts.append("# 全局信条")
    parts.append("")
    parts.append(global_text)

    if workspace_rules is not None and workspace_rules.exists():
        workspace_text = workspace_rules.read_text(encoding="utf-8").strip()
        if workspace_text:
            parts.append("")
            label = workspace_label or "当前工作区"
            parts.append(f"# 工作区信条（{label}）")
            parts.append("")
            parts.append(workspace_text)

    parts.append("")
    parts.append(
        "记忆使用规则：事实（fact）是长期结论，日志（log）是当天原始记录且只追加。"
        "事实被推翻时追加新事实并让旧事实失效，禁止改写或删除历史。"
    )
    return "\n".join(parts)


def describe(path: Path) -> dict[str, object]:
    text = load(path)
    sec = sections(text)
    return {
        "path": str(path),
        "file": path.name,
        "exists": path.exists(),
        "chars": len(text),
        "sections": {name: len(items) for name, items in sec.items()},
        "items": sum(len(v) for v in sec.values()),
    }
