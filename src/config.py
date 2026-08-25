"""
配置加载。非敏感项模板在仓库根的 config.example.ini（入库）；
真实配置（内网主机名/仓库名/账号、收件邮箱）放 config.ini（本地、已 gitignore，勿提交）。
纯标准库。
"""
from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # [d2] 内网连接（真实值只在本地 config.ini）
    d2_host: str = ""
    d2_repo: str = ""
    d2_account: str = ""
    # [report]
    language: str = "en"
    cover_weekend_on_monday: bool = True
    subject_prefix: str = "[D2 Daily]"
    report_title: str = "Documentum D2 Daily Intake"
    # [email]
    send_from: str = ""
    recipients: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"https://{self.d2_host}/D2-REST" if self.d2_host else ""


def _split(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n;]+", raw or "") if x.strip()]


def load(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else ROOT / "config.ini"
    cfg = configparser.ConfigParser()
    if p.exists():
        cfg.read(p, encoding="utf-8")
    d2 = cfg["d2"] if cfg.has_section("d2") else {}
    rep = cfg["report"] if cfg.has_section("report") else {}
    eml = cfg["email"] if cfg.has_section("email") else {}
    cover = cfg.getboolean("report", "cover_weekend_on_monday", fallback=True) \
        if cfg.has_section("report") else True
    return Settings(
        d2_host=d2.get("host", ""),
        d2_repo=d2.get("repo", ""),
        d2_account=d2.get("account", ""),
        language=rep.get("language", "en"),
        cover_weekend_on_monday=cover,
        subject_prefix=rep.get("subject_prefix", "[D2 Daily]"),
        report_title=rep.get("report_title", "Documentum D2 Daily Intake"),
        send_from=eml.get("send_from", ""),
        recipients=_split(eml.get("recipients", "")),
    )
