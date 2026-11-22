# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 XHTV
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前泄露自检（stdlib only）

用途：把 new-api-new 推上 GitHub 之前跑一遍，确认没有把私人凭据 / 本机路径 / 数据库带出去。

    python tools/check-secrets.py            # 扫当前仓库
    python tools/check-secrets.py --quiet    # 只输出结论
    python tools/check-secrets.py --strict   # 连「被 .gitignore 挡住的文件」也一起扫

退出码：0 = 通过，1 = 有阻断项。

设计要点（踩过的坑）：
  · 只扫「将来会进仓库的文件」—— vault/keys.json、one-api.db、logs/ 这类本来就被忽略，
    扫它们只会制造噪音，把真问题淹掉。
  · 32 位 hex 太常见（OAuth client_id、fixture、uuid），必须带上下文关键字才算命中。
  · 上游第三方代码里的示例 key（free-one-api 的 mock、g4f 的 fixtures）用白名单排除，
    白名单里每行都要写理由，方便复核。
"""

import argparse
import os
import re
import sys

# ── 扫描范围 ────────────────────────────────────────────────
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__",
    "venv-foa", "venv-g4f", ".venv", "venv", "dist", "build",
}
TEXT_EXT = {
    ".py", ".js", ".mjs", ".ts", ".json", ".bat", ".cmd", ".ps1", ".sh",
    ".md", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env",
}

# ── 凡是匹配这些前缀/后缀的，都不该进仓库（默认不扫内容）──────
IGNORE_EXACT = {
    "vault/keys.json",
    "one-api.db",
    "p.tmp.json",
    "embedded/aiclient/configs/provider_pools.json",
    "embedded/aiclient/configs/token-store.json",
    "embedded/aiclient/configs/config.json",
    "embedded/aiclient/configs/pwd",
    "embedded/aiclient/configs/api-potluck-keys.json",
}
IGNORE_PREFIX = ("logs/", "vault/keys.json.", "embedded/free-one-api/data/")
IGNORE_SUFFIX = (".bak", ".orig", ".tmp", ".log", ".db", ".db-wal", ".db-shm")

# ── 检测规则（命中即阻断）────────────────────────────────────
PLACEHOLDER = (
    "your_", "your-", "replace", "example", "placeholder", "xxx",
    "dummy", "changeme", "<你的", "todo", "username",
)

SECRET_RULES = [
    ("Cloudflare 令牌 cfut_", re.compile(r"cfut_[A-Za-z0-9]{20,}")),
    ("通用 sk- 密钥", re.compile(r"\bsk-(?!aiclient|xxx)[A-Za-z0-9\-_]{20,}")),
    ("Google AI Studio AQ.", re.compile(r"\bAQ\.[A-Za-z0-9_\-]{20,}")),
    ("Google API Key AIza", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("JWT 凭据（eyJ 开头）", re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}")),
    ("Bearer 长令牌", re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]{32,}")),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]

# 32 位 hex：必须同一行出现上下文关键字，否则多是 OAuth client_id / fixture / uuid
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
HEX32_CONTEXT = re.compile(
    r"(?i)(account[_\- ]?id|api[_\- ]?key|secret|token|passwd|password|密码|密钥|账号)"
)

PATH_RULES = [
    ("Windows 用户目录", re.compile(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.\-]+")),
    ("Git Bash 用户目录", re.compile(r"/c/Users/[A-Za-z0-9_.\-]+")),
    ("本机内网地址", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b(?!/)")),
]

# 必须被 .gitignore 覆盖的条目
GITIGNORE_MUST = [
    "vault/keys.json", "one-api.db", "logs/", "provider_pools.json",
    "__pycache__", "node_modules",
]

MASK_KEEP = 8


def mask(s):
    s = str(s).replace("\n", " ")
    return s[:MASK_KEEP] + "…" if len(s) > MASK_KEEP else s


def is_placeholder(line):
    low = line.lower()
    return any(p in low for p in PLACEHOLDER)


def rel(root, p):
    return os.path.relpath(p, root).replace("\\", "/")


def is_ignored(r):
    if r in IGNORE_EXACT:
        return True
    if any(r.startswith(x) for x in IGNORE_PREFIX):
        return True
    if any(r.endswith(x) for x in IGNORE_SUFFIX):
        return True
    if r.split("/")[0] in ("logs",):
        return True
    return False


def load_allowlist(root):
    p = os.path.join(root, "tools", "secret-allowlist.txt")
    entries = []
    if not os.path.exists(p):
        return entries
    for line in open(p, encoding="utf-8", errors="ignore"):
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(line.replace("\\", "/"))
    return entries


def iter_text_files(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if os.path.splitext(f)[1].lower() not in TEXT_EXT:
                continue
            p = os.path.join(dp, f)
            try:
                if os.path.getsize(p) > 4 * 1024 * 1024:
                    continue
            except OSError:
                continue
            yield p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--strict", action="store_true", help="连被忽略的文件也扫（更啰嗦，用于排查）")
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    allowlist = load_allowlist(root)
    findings = []
    checked = 0
    skipped = 0

    for p in iter_text_files(root):
        r = rel(root, p)
        if not args.strict and is_ignored(r):
            skipped += 1
            continue
        if any(r.startswith(a) for a in allowlist):
            skipped += 1
            continue
        try:
            text = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        checked += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if is_placeholder(line):
                continue
            for name, rx in SECRET_RULES:
                m = rx.search(line)
                if m:
                    findings.append(("SECRET", r, "L%d %s: %s" % (lineno, name, mask(m.group(0)))))
            if HEX32.search(line) and HEX32_CONTEXT.search(line):
                findings.append(("SECRET", r, "L%d 32位hex+密钥上下文: %s"
                                 % (lineno, mask(HEX32.search(line).group(0)))))
            for name, rx in PATH_RULES:
                m = rx.search(line)
                if m:
                    findings.append(("PATH", r, "L%d %s: %s" % (lineno, name, mask(m.group(0)))))

    # 敏感文件物理存在（不算失败，但要让作者知道它们在不在）
    present = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            r = rel(root, os.path.join(dp, f))
            if r in IGNORE_EXACT or any(r.startswith(x) for x in IGNORE_PREFIX):
                present.append(r)

    # .gitignore 覆盖
    gi_path = os.path.join(root, ".gitignore")
    if not os.path.exists(gi_path):
        gi_missing = list(GITIGNORE_MUST)
    else:
        gi = open(gi_path, encoding="utf-8", errors="ignore").read()
        gi_missing = [x for x in GITIGNORE_MUST if x not in gi]

    if not args.quiet:
        print("扫描根目录: %s" % root)
        print("检查文本文件: %d 个（跳过忽略/白名单 %d 个）\n" % (checked, skipped))
        if findings:
            print("✗ 内容命中（必须处理才能发布）:")
            for level, f, msg in findings:
                print("   [%s] %s  %s" % (level, f, msg))
        else:
            print("✓ 内容未命中任何密钥 / 本机路径规则")
        print()
        if present:
            print("· 敏感文件物理存在（由 .gitignore 挡住，不会入库）:")
            for r in sorted(present):
                print("   - " + r)
            print()
        if gi_missing:
            print("✗ .gitignore 缺少条目: " + ", ".join(gi_missing))
        else:
            print("✓ .gitignore 覆盖完整")
        print()

    if findings or gi_missing:
        print("结论: FAIL —— 内容命中 %d 条、忽略规则缺失 %d 条"
              % (len(findings), len(gi_missing)))
        return 1
    print("结论: PASS —— 可以 push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
