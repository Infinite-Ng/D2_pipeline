"""
取数层：只读 DQL over Documentum D2-REST。

安全承诺（与 M0 探测脚本一致）：
  * 只用 HTTP GET；只发 SELECT 查询；绝不修改/删除仓库任何内容。
  * 凭据来源优先级：环境变量 > keyring(Windows 凭据管理器) > 交互式 getpass。
    密码只在内存构造 Basic 头，不落盘、不写日志。

内网主机名/仓库名/账号不硬编码，全部由 config.ini（本地、gitignore）传入。
纯标准库；keyring 为可选依赖（没装就自动跳过）。
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

KEYRING_SERVICE = "D2_pipeline"
TIMEOUT = 30


def get_credentials(default_account: str = "") -> tuple[str, str]:
    """按 env -> keyring -> getpass 顺序取凭据。返回 (user, password)。"""
    user = os.environ.get("D2_USER") or default_account
    pwd = os.environ.get("D2_PASSWORD")
    if pwd:
        return user, pwd
    try:
        import keyring  # 可选
        if user:
            stored = keyring.get_password(KEYRING_SERVICE, user)
            if stored:
                return user, stored
    except Exception:  # noqa: BLE001
        pass
    typed = input(f"    D2 登录名{f' [{user}]' if user else ''}: ").strip() or user
    pwd = getpass.getpass("    D2 密码（不保存、不外传）: ")
    return typed, pwd


class D2Client:
    """只读 DQL 客户端。base/repo 由调用方（config）给定。"""

    def __init__(self, user: str, password: str, base: str, repo: str):
        self.user = user          # 登录身份；默认作为要查询的 inbox 归属
        self.base = base.rstrip("/")
        self.repo = repo
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # 内网自签证书
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def _get(self, url: str, params: dict) -> tuple[int, str]:
        full = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(full, method="GET", headers={
            "Accept": "application/json", "User-Agent": "D2-pipeline/1", "Authorization": self._auth})
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return e.code, ""

    def dql_page(self, query: str, page: int = 1, page_size: int = 200) -> list[dict]:
        """执行一页 DQL SELECT，返回该页的 properties 列表。出错抛异常。"""
        if not query.lstrip().lower().startswith("select"):
            raise ValueError("只允许 SELECT 查询")  # 只读护栏
        st, text = self._get(f"{self.base}/repositories/{urllib.parse.quote(self.repo)}",
                             {"dql": query, "items-per-page": page_size, "page": page})
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            data = None
        if st != 200 or not isinstance(data, dict):
            raise RuntimeError(f"DQL 失败 status={st}: {text[:300]}")
        rows = []
        for e in data.get("entries", []) or []:
            c = e.get("content") if isinstance(e, dict) else None
            props = (c or {}).get("properties") if isinstance(c, dict) else None
            rows.append(props if isinstance(props, dict) else {})
        return rows

    def dql_all(self, query: str, page_size: int = 200, max_pages: int = 50) -> list[dict]:
        """翻页取全部结果。某页不足 page_size 即停止。"""
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            rows = self.dql_page(query, page=page, page_size=page_size)
            out.extend(rows)
            if len(rows) < page_size:
                break
        return out


def _q(value: str) -> str:
    return (value or "").replace("'", "''")


def fetch_inbox(client: D2Client, account: str | None = None) -> list[dict]:
    """
    取指定账号 inbox 的全部未删除条目（只读）。
    account 默认 = 登录账号（读自己的队列一定有权限）；
    也可用环境变量 D2_INBOX 指定别的队列（前提是登录身份对它有读权限，一般需 superuser）。
    """
    account = account or os.environ.get("D2_INBOX") or client.user
    query = (
        "select r_object_id, item_id, router_id, task_subject, task_state, "
        "date_sent, sent_by, supervisor_name, priority, read_flag "
        f"from dmi_queue_item where name = '{_q(account)}' and delete_flag = false "
        "order by date_sent desc"
    )
    return client.dql_all(query)
