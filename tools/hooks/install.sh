#!/bin/sh
# 安装仓库自带的 git 钩子（本机一次即可；换机器 / 重新 clone 后再跑一次）
#
#   sh tools/hooks/install.sh
#
# 装什么：
#   .git/hooks/pre-commit  ← tools/hooks/pre-commit
#     ① 阻止提交「本地专用工具 / 记忆库」（ExternalCommandSystem、.workbuddy、.ai_memory）
#     ② 阻止提交内容里出现**本机真实路径**（用户名目录、工作根、安装目录）
#
# 为什么不放在 core.hooksPath：钩子是按 clone 生效的本地配置，指到仓库内目录会让
# 别人的 clone 也受我们的规则约束；而这条规则的强度依赖「拒绝」而非「提醒」，
# 装到 .git/hooks 最直接。
set -eu

repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    printf '错误：不在 git 仓库里\n' >&2
    exit 1
}

src="$repo_root/tools/hooks/pre-commit"
dst="$repo_root/.git/hooks/pre-commit"

[ -f "$src" ] || { printf '错误：找不到 %s\n' "$src" >&2; exit 1; }

if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
    backup="$dst.bak-$(date +%Y%m%d-%H%M%S)"
    cp "$dst" "$backup"
    printf '已有不同的 pre-commit，先备份到 %s\n' "${backup##*/}"
fi

cp "$src" "$dst"
chmod +x "$dst"
printf '已安装 pre-commit → %s\n' "$dst"
printf '自检：git hook run pre-commit\n'
