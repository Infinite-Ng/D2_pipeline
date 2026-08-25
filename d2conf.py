"""
探测脚本共用的 D2 连接参数读取（避免把内网主机名/仓库名/账号硬编码进公开代码）。
优先级：环境变量 > 本地 config.ini 的 [d2] 段。config.ini 已被 .gitignore 挡住。
纯标准库。
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path


def load_d2() -> tuple[str, str, str]:
    """返回 (host, repo, account)。host 未配置则报错提示。"""
    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).parent / "config.ini", encoding="utf-8")
    d2 = cfg["d2"] if cfg.has_section("d2") else {}
    host = os.environ.get("D2_HOST") or d2.get("host", "")
    repo = os.environ.get("D2_REPO") or d2.get("repo", "")
    account = os.environ.get("D2_ACCOUNT") or d2.get("account", "")
    if not host:
        raise SystemExit(
            "未找到 D2 主机配置。请把 config.example.ini 复制成 config.ini 并填写 [d2] host/repo/account，"
            "或设置环境变量 D2_HOST/D2_REPO/D2_ACCOUNT。"
        )
    return host, repo, account
