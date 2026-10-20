"""mail server 管理頁的外寄信件 archive scraper。
從 mail server 的 outbound archive 頁 scrape 外寄信件，補齊 mbsync IMAP
路線看不到的寄出信件缺口。

公開 API：
    run_scrape(from_date, to_date, *, dry_run, database_path) -> ScrapeSummary
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.poolmanager import PoolManager

from .imap_sync import _build_legacy_ssl_context
from .models import MailboxParticipant, NormalizedAttachment, NormalizedMessage
from .storage import connect_database, initialize_database, persist_message

CREDENTIALS_ENV_PATH: Path = Path(r"C:\IT_Projects\mail-server\.env")
"""mail server 的憑證檔（見 design.md Decision 9）。"""

CREDENTIALS_ENV_VAR: str = "MAIL_SERVER_ENV_PATH"
"""環境變數名：WSL 部署時注入 credentials 檔路徑。

見 fix-credentials-path-wsl-portability change。
"""

LOG_DIR_NAME: str = ".archive-scrape-logs"
"""相對於專案根目錄的 daily log 目錄名（見 spec Requirement 7）。"""

DEFAULT_TIMEOUT_SECONDS: int = 15
"""HTTP request default timeout（秒）。"""

EXIT_OK: int = 0
EXIT_ENV_PROBLEM: int = 1
EXIT_PARTIAL_FAILURE: int = 5


# ---------------------------------------------------------------------------
# Custom exception classes (task 2.3)
# ---------------------------------------------------------------------------


class EnvNotFoundError(RuntimeError):
    """credentials 檔不存在（exit 1）。"""

    exit_code: int = EXIT_ENV_PROBLEM

    def __init__(self, env_path: Path) -> None:
        super().__init__(f"MAIL_SERVER_ENV_NOT_FOUND: {env_path}")


class EnvMissingFieldError(RuntimeError):
    """credentials 檔缺必填欄位或值為空（exit 1）。"""

    exit_code: int = EXIT_ENV_PROBLEM

    def __init__(self, key: str) -> None:
        super().__init__(f"MAIL_SERVER_ENV_MISSING_FIELD: {key}")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScrapeSummary:
    """單次 scrape run 的 JSON-encodable 結果 schema。

    對應 spec Requirement：「The system SHALL emit a single-line JSON summary
    to stdout per run」之 10 個欄位。
    """

    started_at: datetime
    ended_at: datetime
    date_range: tuple[date, date]
    candidate_entries: int
    matched_sender: int
    fetched_messages: int
    inserted_messages: int
    skipped_duplicates: int
    errors: int
    exit_code: int


@dataclass(frozen=True, slots=True)
class MlsCredentials:
    """從 mail server 憑證檔載入的三欄位。

    讀取邏輯與失敗 exit code 對應 spec Requirement「load credentials」。
    """

    url: str
    user: str
    password: str = field(repr=False)


# ---------------------------------------------------------------------------
# task 1.2 — default credentials path resolver (env var override)
# ---------------------------------------------------------------------------


def _default_credentials_env_path() -> Path:
    """回傳預設 credentials 檔路徑，優先讀 ``MAIL_SERVER_ENV_PATH`` 環境變數。

    Returns
    -------
    Path
        若 ``MAIL_SERVER_ENV_PATH`` 已設定且非空字串，回傳 ``Path(override)``；
        否則（未設定或空字串）回傳 hardcoded 預設 ``CREDENTIALS_ENV_PATH``。
    """
    override = os.environ.get(CREDENTIALS_ENV_VAR)
    if override:
        return Path(override)
    return CREDENTIALS_ENV_PATH


# ---------------------------------------------------------------------------
# task 2.1 — credentials loader
# ---------------------------------------------------------------------------


def load_credentials(env_path: Path | None = None) -> MlsCredentials:
    """從 mail server 憑證檔讀取 MLS_URL / MLS_USER / MLS_PASS。

    Parameters
    ----------
    env_path:
        `.env` 路徑。``None`` 時使用 :func:`_default_credentials_env_path`
        解析（優先讀 ``MAIL_SERVER_ENV_PATH`` 環境變數，fallback 到
        hardcoded ``CREDENTIALS_ENV_PATH``）。

    Raises
    ------
    EnvNotFoundError
        檔案不存在。
    EnvMissingFieldError
        必填欄位缺失或值為空。
    """
    path = env_path if env_path is not None else _default_credentials_env_path()

    if not path.exists():
        raise EnvNotFoundError(path)

    parsed: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()

    for key in ("MLS_URL", "MLS_USER", "MLS_PASS"):
        if not parsed.get(key):
            raise EnvMissingFieldError(key)

    return MlsCredentials(
        url=parsed["MLS_URL"],
        user=parsed["MLS_USER"],
        password=parsed["MLS_PASS"],
    )


# ---------------------------------------------------------------------------
# task 2.2 — session builder
# ---------------------------------------------------------------------------


class _LegacySslAdapter(HTTPAdapter):
    """HTTPAdapter 注入 legacy SSL context。"""

    def init_poolmanager(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        kwargs["ssl_context"] = _build_legacy_ssl_context()
        self.poolmanager = PoolManager(*args, **kwargs)


def build_session(
    creds: MlsCredentials,
    *,
    verify: bool = False,
) -> requests.Session:
    """建立已設好 Basic auth + legacy SSL context 的 requests.Session。

    mail server 的自簽憑證需放寬 TLS 檢查（Trust 來自「user 擁有並控制
    mail server」，不依賴 PKI）。詳見
    imap_sync._build_legacy_ssl_context 同源處置。

    Parameters
    ----------
    creds:
        由 :func:`load_credentials` 載入的憑證。
    verify:
        SSL 憑證驗證（預設 False，自簽）。
    """
    if not verify:
        urllib3.disable_warnings(InsecureRequestWarning)

    session = requests.Session()
    session.auth = (creds.user, creds.password)
    session.verify = verify
    session.mount("https://", _LegacySslAdapter())
    return session


# ---------------------------------------------------------------------------
# Selectors — module-level constants (kept in one place so they are easy to swap)
# ---------------------------------------------------------------------------

# list 頁
_SEL_DATE_SELECT = 'select[name="dt"]'
_SEL_DATA_ROW = "tr[id^='ML_']"  # pattern: ML_0, ML_1, ...
_SEL_PAGE_INPUT_GO = "button.list_tool_bt_go"

# detail header 頁 (q=1)
_SEL_DETAIL_HEADER_ROWS = "tr.TextAttr"
_SEL_IFRAME_EMAIL = "iFrame[name='email_frame']"
_SEL_ATTACHMENT_LINK = "a[href*='cd=3']"

# detail body 頁 (q=0&cd=2)
_SEL_BODY_PRE = "pre"

# ViewDetail onclick pattern: ViewDetail(DT, SN, NO, ...)  → SN = entry_id
_RE_VIEWDETAIL_ENTRY = re.compile(
    r"ViewDetail\([^,]+,\s*'(\d+)'",
)
# InputPage call's last arg is pagemax: InputPage(form, num, e, act, uipagenum, pagemax)
_RE_INPUTPAGE_PAGEMAX = re.compile(
    r"InputPage\(this\.form,\s*0,\s*event,\s*'GO',\s*\d+,\s*(\d+)\)"
)
_RE_PAGE_VALUE = re.compile(r'name="page" value="(\d+)"')

# Label texts in Chinese UI (used for detail header parsing)
_LABEL_FROM = "寄件者"
_LABEL_DATE = "日期"
_LABEL_MSG_ID = "郵件 ID"
_LABEL_TO = "收件者"
_LABEL_CC = "副本"
_LABEL_SUBJECT = "主旨"


# ---------------------------------------------------------------------------
# Data classes — list + detail (task 3.x / 4.x)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListRowMeta:
    """Archive list 頁一行 row 的 metadata。"""

    time_str: str
    """當日時間字串，如 ``16:47:19``。"""

    sender: str
    """寄件者 email。"""

    recipient: str
    """收件者（逗號分隔，多個）。"""

    message_id: str
    """郵件 ID（完整 message-id 字串）。"""

    subject: str
    """郵件主旨（HTML entity 已解碼）。"""

    entry_id: int
    """Detail 頁的 ``entry=N`` 值，1-based 歸檔序號。"""


@dataclass(frozen=True, slots=True)
class DetailHeader:
    """Detail 頁 header（q=1）解析結果。"""

    from_addr: str
    """寄件者 email。"""

    to_addrs: tuple[str, ...]
    """收件者 email 列表。"""

    cc_addrs: tuple[str, ...]
    """副本 email 列表。"""

    subject: str
    """郵件主旨。"""

    sent_at: datetime
    """寄送時間（含時區）。"""

    message_id: str
    """郵件 ID。"""


@dataclass(frozen=True, slots=True)
class AttachmentMeta:
    """Archive 附件 metadata（task 4.2）。不含 raw bytes。"""

    sequence: int
    """附件序號（1-based）。"""

    filename: str
    """附件檔名，含 ``[N] <real_filename>`` prefix（寫入 message_attachments.attachment_name 用）。"""  # noqa: E501


# ---------------------------------------------------------------------------
# ---- list parsing (task 3.x) ----
# ---------------------------------------------------------------------------


def parse_date_options(html: str) -> list[date]:
    """從 list 頁 HTML 解析 ``<select name="dt">`` 內所有 option 的日期。

    Parameters
    ----------
    html:
        archive 列表頁回傳的完整 HTML。

    Returns
    -------
    list[date]
        所有 option value（格式 ``YYYYMMDD``）對應的 :class:`datetime.date` 物件，
        順序同 HTML 出現順序。

    Raises
    ------
    ValueError
        找不到 ``<select name="dt">`` 或無任何 option。
    """
    soup = BeautifulSoup(html, "html.parser")
    select = soup.select_one(_SEL_DATE_SELECT)
    if select is None:
        raise ValueError("parse_date_options: cannot find <select name='dt'>")
    options = select.find_all("option")
    if not options:
        raise ValueError(
            "parse_date_options: <select name='dt'> has no <option> elements"
        )

    result: list[date] = []
    for opt in options:
        val = opt.get("value", "").strip()
        if not val or len(val) != 8 or not val.isdigit():
            continue
        try:
            d = date(int(val[:4]), int(val[4:6]), int(val[6:8]))
        except ValueError as exc:
            raise ValueError(f"parse_date_options: invalid date value {val!r}") from exc
        result.append(d)
    return result


def validate_date_range(
    from_d: date,
    to_d: date,
    available: list[date],
) -> tuple[list[date], list[date]]:
    """對 ``from_d``..``to_d`` 範圍與 ``available`` 視窗做交叉驗證。

    超出視窗的日期 emit warning 到 stderr 但不 raise。

    Parameters
    ----------
    from_d, to_d:
        Inclusive 日期範圍（``from_d <= to_d``）。
    available:
        由 :func:`parse_date_options` 回傳的 MLS retention 視窗日期列表。

    Returns
    -------
    tuple[list[date], list[date]]
        ``(in_window, out_of_window)``：前者可用、後者超出 retention。
    """
    available_set = set(available)
    in_window: list[date] = []
    out_of_window: list[date] = []

    current = from_d
    while current <= to_d:
        if current in available_set:
            in_window.append(current)
        else:
            out_of_window.append(current)
        # advance by 1 day

        current = current + timedelta(days=1)

    if out_of_window:
        out_strs = ", ".join(str(d) for d in out_of_window)
        warnings.warn(
            f"validate_date_range: {len(out_of_window)} date(s) outside MLS "
            f"retention window will be skipped: {out_strs}",
            stacklevel=2,
        )

    return in_window, out_of_window


def parse_list_rows(
    html: str,
) -> tuple[list[ListRowMeta], bool, str | None]:
    """從 list 頁 HTML 解析所有 row metadata。

    Parameters
    ----------
    html:
        archive 列表頁（指定日期）回傳的完整 HTML（單一頁面）。

    Returns
    -------
    tuple[list[ListRowMeta], bool, str | None]
        ``(rows, has_next_page, next_page_url)``。
        ``next_page_url`` 在 ``has_next_page=False`` 時為 ``None``；
        本函式回傳的 URL 為 placeholder（實際 URL 需呼叫端依表單 POST 構建）。

    Raises
    ------
    ValueError
        row 缺少必要欄位（time / sender / message_id / entry_id）。
    """
    soup = BeautifulSoup(html, "html.parser")
    data_rows = soup.select(_SEL_DATA_ROW)

    rows: list[ListRowMeta] = []
    for tr in data_rows:
        tds = tr.find_all("td")
        if len(tds) < 7:
            raise ValueError(
                f"parse_list_rows: row {tr.get('id', '?')} has only "
                f"{len(tds)} td, expected >= 7"
            )

        # td[1] = time_str
        time_str = tds[1].get_text(strip=True)

        # td[2] = sender (link text)
        sender_link = tds[2].find("a")
        if sender_link is None:
            raise ValueError(
                f"parse_list_rows: row {tr.get('id', '?')} missing sender link"
            )
        sender = sender_link.get_text(strip=True)

        # td[3] = recipient (link text)
        recipient_link = tds[3].find("a")
        recipient = recipient_link.get_text(strip=True) if recipient_link else ""

        # td[4] = message_id (hidden input + text; prefer hidden input value)
        mid_input = tds[4].find("input", {"type": "hidden"})
        if mid_input is not None:
            message_id = mid_input.get("value", "").strip()
        else:
            message_id = tds[4].get_text(strip=True)

        # td[6] = subject link + ViewDetail onclick for entry_id
        subject_link = tds[6].find("a")
        if subject_link is None:
            raise ValueError(
                f"parse_list_rows: row {tr.get('id', '?')} missing subject link"
            )
        subject = subject_link.get_text(strip=True)

        onclick = subject_link.get("onclick", "")
        m = _RE_VIEWDETAIL_ENTRY.search(onclick)
        if m is None:
            # fallback: try checkbox entry value in td[0]
            cb = tds[0].find("input", {"name": "entry"})
            if cb is not None:
                entry_id = int(cb.get("value", "0"))
            else:
                raise ValueError(
                    f"parse_list_rows: row {tr.get('id', '?')} cannot extract entry_id"
                )
        else:
            entry_id = int(m.group(1))

        rows.append(
            ListRowMeta(
                time_str=time_str,
                sender=sender,
                recipient=recipient,
                message_id=message_id,
                subject=subject,
                entry_id=entry_id,
            )
        )

    # Pagination detection: find current page and pagemax from InputPage call
    m_pagemax = _RE_INPUTPAGE_PAGEMAX.search(html)
    m_page = _RE_PAGE_VALUE.search(html)
    if m_pagemax and m_page:
        pagemax = int(m_pagemax.group(1))
        current_page = int(m_page.group(1))
        has_next_page = current_page < pagemax
    else:
        has_next_page = False

    next_page_url: str | None = "__next_page_post__" if has_next_page else None
    return rows, has_next_page, next_page_url


def filter_by_sender(rows: list[ListRowMeta], sender: str) -> list[ListRowMeta]:
    """從 rows 中篩選 sender 完全匹配的 row（case-insensitive, stripped）。

    Parameters
    ----------
    rows:
        由 :func:`parse_list_rows` 回傳的 row 列表。
    sender:
        目標寄件者 email。

    Returns
    -------
    list[ListRowMeta]
        sender 匹配的 row 子集。
    """
    target = sender.strip().lower()
    return [r for r in rows if r.sender.strip().lower() == target]


# ---------------------------------------------------------------------------
# ---- detail parsing (task 4.x) ----
# ---------------------------------------------------------------------------

# Email address extraction regex (loose, sufficient for admin UI output)
_RE_EMAIL_IN_ANGLE = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
_RE_BARE_EMAIL = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")


def _extract_emails(text: str) -> list[str]:
    """從 textarea/td 原始文字擷取所有 email 地址。

    優先抓 ``<email>`` angle bracket 格式，其次 bare email。
    """
    in_angle = _RE_EMAIL_IN_ANGLE.findall(text)
    if in_angle:
        return in_angle
    return _RE_BARE_EMAIL.findall(text)


def parse_detail_header(
    html_q1: str,
    *,
    fallback_message_id: str | None = None,
    fallback_sent_at: datetime | None = None,
) -> DetailHeader:
    """從 detail header 頁（q=1）HTML 解析六欄位。

    Parameters
    ----------
    html_q1:
        archive 明細頁回傳的完整 HTML。
    fallback_message_id:
        當 detail page 缺少「郵件 ID」欄位（或值為空 / ``(null)``）時使用的
        fallback message_id。若為 ``None`` 且欄位缺失，則 raise ``ValueError``。
        典型值：``"archive:<date>:entry=<N>"``（由呼叫端提供）。
    fallback_sent_at:
        當日期欄位無法解析（例如 mail server 顯示 ``---``）時使用的 fallback
        ``datetime``。若為 ``None`` 且日期無法解析，則 raise ``ValueError``。

    Returns
    -------
    DetailHeader
        包含 from_addr, to_addrs, cc_addrs, subject, sent_at, message_id。

    Raises
    ------
    ValueError
        必要欄位缺失或無法解析，且未提供對應 fallback。
    """
    soup = BeautifulSoup(html_q1, "html.parser")
    header_rows = soup.select(_SEL_DETAIL_HEADER_ROWS)

    fields: dict[str, str] = {}
    for tr in header_rows:
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        # Skip attachment dropdown rows: their first td contains an anchor link
        # (e.g. "[01] ATT00005.txt") rather than a text label.
        if tds[0].find("a") is not None:
            continue
        label = tds[0].get_text(strip=True).rstrip(":")
        # recipient / cc use textarea; others use plain text
        ta = tds[1].find("textarea")
        value = ta.get_text() if ta is not None else tds[1].get_text(strip=True)
        fields[label.strip()] = value

    def _require(key: str) -> str:
        for k, v in fields.items():
            if key in k:
                return v
        raise ValueError(
            f"parse_detail_header: missing field matching {key!r}; "
            f"found keys: {list(fields.keys())}"
        )

    from_addr = _require(_LABEL_FROM).strip()
    date_str = _require(_LABEL_DATE).strip()

    # message_id: use detail page value when present and non-trivial;
    # fall back to caller-supplied fallback_message_id for entries where
    # the mail server does not generate a Message-ID (stored as "(null)" or absent).
    import contextlib

    raw_message_id: str | None = None
    with contextlib.suppress(ValueError):
        raw_message_id = _require(_LABEL_MSG_ID).strip()
    if not raw_message_id or raw_message_id == "(null)":
        if fallback_message_id is None:
            raise ValueError(
                f"parse_detail_header: missing field matching {_LABEL_MSG_ID!r}; "
                f"found keys: {list(fields.keys())}"
            )
        message_id = fallback_message_id
    else:
        message_id = raw_message_id

    to_raw = _require(_LABEL_TO)
    cc_raw = _require(_LABEL_CC) if any(_LABEL_CC in k for k in fields) else ""
    subject = _require(_LABEL_SUBJECT).strip()

    # Parse datetime; fall back to fallback_sent_at when the mail server renders "---"
    sent_at: datetime
    try:
        sent_at = parsedate_to_datetime(date_str)
    except Exception as exc:
        if fallback_sent_at is None:
            raise ValueError(
                f"parse_detail_header: cannot parse date {date_str!r}: {exc}"
            ) from exc
        sent_at = fallback_sent_at

    to_addrs = tuple(_extract_emails(to_raw))
    cc_addrs = tuple(_extract_emails(cc_raw))

    return DetailHeader(
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        subject=subject,
        sent_at=sent_at,
        message_id=message_id,
    )


def parse_detail_body(html_body: str) -> str:
    """從 body iframe 頁（q=0&cd=2）HTML 抽取 ``<pre>`` plain text。

    Parameters
    ----------
    html_body:
        archive 內文頁回傳的完整 HTML。

    Returns
    -------
    str
        ``<pre>`` 元素內的 plain text（保留換行）。

    Raises
    ------
    ValueError
        找不到 ``<pre>`` 元素。
    """
    soup = BeautifulSoup(html_body, "html.parser")
    pre = soup.select_one(_SEL_BODY_PRE)
    if pre is None:
        raise ValueError("parse_detail_body: cannot find <pre> element in body HTML")
    return pre.get_text()


def parse_attachment_links(html_q1: str) -> list[AttachmentMeta]:
    """從 detail header 頁（q=1）解析附件 metadata。

    解析 ``cd=3&no=N`` 連結，記錄序號與檔名。**不**下載 raw bytes。
    ``AttachmentMeta.filename`` 包含 ``[N] <real_filename>`` prefix，
    方便後續寫入 ``message_attachments.attachment_name``（schema 無 sequence 欄）。

    Parameters
    ----------
    html_q1:
        archive 明細頁回傳的完整 HTML。

    Returns
    -------
    list[AttachmentMeta]
        附件列表，序號 1-based，與 HTML 出現順序一致。空列表表示無附件。
    """
    soup = BeautifulSoup(html_q1, "html.parser")
    attachment_links = soup.select(_SEL_ATTACHMENT_LINK)

    result: list[AttachmentMeta] = []
    for a in attachment_links:
        href = a.get("href", "")
        m = re.search(r"no=(\d+)", href)
        if m is None:
            continue
        seq = int(m.group(1))
        # Raw text contains [ NN ] prefix from HTML; strip it
        raw_text = a.get_text(strip=True)
        # Pattern: "[ 01 ]image001.png" → strip bracket prefix
        real_filename = re.sub(r"^\[\s*\d+\s*\]\s*", "", raw_text).strip()
        if not real_filename:
            continue
        # Construct prefixed filename for schema workaround
        prefixed = f"[{seq}] {real_filename}"
        result.append(AttachmentMeta(sequence=seq, filename=prefixed))

    return result


# ---------------------------------------------------------------------------
# ---- HTTP fetch helpers (Group 5 glue) ----
# ---------------------------------------------------------------------------


def _fetch_list_page(session: requests.Session, base_url: str, dt: date) -> str:
    """Fetch the outbound archive list page for a given date.

    On the first call per session, navigates the admin dashboard menu
    (steps 1+2) to establish menu state before requesting the list page.
    Subsequent calls skip steps 1+2 (menu state cached on session object).

    Parameters
    ----------
    session:
        Authenticated requests.Session (from build_session).
    base_url:
        Base URL of the mail server admin UI, e.g. ``https://192.0.2.10:<port>``.
    dt:
        Date to fetch list for.

    Returns
    -------
    str
        HTML text of the list page (encoding forced to UTF-8).
    """
    if not getattr(session, "_mls_menu_navigated", False):
        session.get(
            f"{base_url}/cgi-bin/index.cgi",
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        session.get(
            f"{base_url}/cgi-bin/index.cgi"
            "?query=8&MULTI_LANG=ch&click_v=59&click_v=62&click_v=12345",
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        session._mls_menu_navigated = True  # type: ignore[attr-defined]

    dt_str = dt.strftime("%Y%m%d")
    r3 = session.get(
        f"{base_url}/cgi-bin/mail_log.cgi"
        f"?dir=2&dt={dt_str}&menu=click_v%3d59%0aclick_v%3d62%0a&MULTI_LANG=ch",
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    r3.encoding = "utf-8"
    return r3.text


def _fetch_detail_q1(
    session: requests.Session,
    base_url: str,
    entry_id: int,
    dt: date,
    sender: str,
    recipient: str,
) -> str:
    """Fetch the detail header page (q=1) for a single archive entry.

    Parameters
    ----------
    session:
        Authenticated requests.Session.
    base_url:
        Base URL of the mail server admin UI.
    entry_id:
        Archive entry sequence number (entry=N in URL).
    dt:
        Date of the archive entry.
    sender:
        Sender email (fsed= parameter).
    recipient:
        First recipient email (frcpt= parameter).

    Returns
    -------
    str
        HTML text of the detail header page (encoding forced to UTF-8).
    """
    dt_str = dt.strftime("%Y%m%d")
    url = (
        f"{base_url}/cgi-bin/mail_log.cgi"
        f"?q=1&dir=2&entry={entry_id}&dt={dt_str}"
        f"&MULTI_LANG=ch&cc=^o^&rrd=1&menulv=59_62"
        f"&fsed={sender}&frcpt={recipient}&rdd=4&sk=0"
    )
    r = session.get(url, timeout=DEFAULT_TIMEOUT_SECONDS)
    r.encoding = "utf-8"
    return r.text


def _extract_cf_filename(html_q1: str) -> str:
    """Extract the cf= archive filename from the email_frame iframe src.

    Parameters
    ----------
    html_q1:
        HTML of the detail header page (q=1).

    Returns
    -------
    str
        The cf= query parameter value (archive filename).

    Raises
    ------
    ValueError
        If the email_frame iframe is not found or cf= is missing from the src.
    """
    soup = BeautifulSoup(html_q1, "html.parser")
    iframe = soup.select_one(_SEL_IFRAME_EMAIL)
    if iframe is None:
        raise ValueError("_extract_cf_filename: cannot find email_frame iframe")
    src = iframe.get("src", "")
    parsed = urllib.parse.urlparse(src)
    qs = urllib.parse.parse_qs(parsed.query)
    cf_values = qs.get("cf", [])
    if not cf_values:
        raise ValueError(
            f"_extract_cf_filename: no cf= parameter in iframe src: {src!r}"
        )
    return cf_values[0]


def _fetch_detail_body(
    session: requests.Session,
    base_url: str,
    entry_id: int,
    dt: date,
    cf_filename: str,
) -> str:
    """Fetch the body iframe page (q=0&cd=2) for a single archive entry.

    Parameters
    ----------
    session:
        Authenticated requests.Session.
    base_url:
        Base URL of the mail server admin UI.
    entry_id:
        Archive entry sequence number.
    dt:
        Date of the archive entry.
    cf_filename:
        Archive filename from the cf= query parameter of the iframe src.

    Returns
    -------
    str
        HTML text of the body iframe page (encoding forced to UTF-8).
    """
    dt_str = dt.strftime("%Y%m%d")
    url = (
        f"{base_url}/cgi-bin/mail_log.cgi"
        f"?q=0&cd=2&cf={cf_filename}&cc=^o^"
        f"&entry={entry_id}&dir=2&dt={dt_str}"
    )
    r = session.get(url, timeout=DEFAULT_TIMEOUT_SECONDS)
    r.encoding = "utf-8"
    return r.text


# ---------------------------------------------------------------------------
# ---- DB integration (task 5.1, 5.2) ----
# ---------------------------------------------------------------------------


def _build_pst_source(cf_filename: str) -> str:
    """Build pst_source string for archive-sourced messages.

    Parameters
    ----------
    cf_filename:
        Archive filename from the cf= query parameter
        (e.g. ``192.0.2.10_20260101000000_LogBackupOut[1].eml.content``).

    Returns
    -------
    str
        ``archive:<cf_filename>``
    """
    return f"archive:{cf_filename}"


def _build_normalized_message(
    header: DetailHeader,
    body_text: str,
    attachments: list[AttachmentMeta],
    cf_filename: str,
    base_url: str,
    entry_id: str,
    dt: date,
) -> NormalizedMessage:
    """Build a NormalizedMessage from parsed archive detail data.

    Parameters
    ----------
    header:
        Parsed detail header (from parse_detail_header).
    body_text:
        Plain text body (from parse_detail_body).
    attachments:
        Attachment metadata list (from parse_attachment_links).
    cf_filename:
        Archive filename (cf= parameter from iframe src).
    base_url:
        mail server base URL (stored as source_requested_from).
    entry_id:
        Archive entry sequence number as string.
    dt:
        Date of the archive entry.

    Returns
    -------
    NormalizedMessage
        Fully populated normalized message ready for persist_message().
    """
    return NormalizedMessage(
        subject=header.subject,
        sender=MailboxParticipant(
            display_name=None,
            email=header.from_addr.strip(),
        ),
        to_recipients=tuple(
            MailboxParticipant(display_name=None, email=addr)
            for addr in header.to_addrs
        ),
        cc_recipients=tuple(
            MailboxParticipant(display_name=None, email=addr)
            for addr in header.cc_addrs
        ),
        bcc_recipients=(),
        sent_at=header.sent_at,
        received_at=None,
        body_text=body_text,
        attachments=tuple(
            NormalizedAttachment(attachment_name=a.filename) for a in attachments
        ),
        message_id=header.message_id,
        in_reply_to=None,
        references=None,
        pst_source=_build_pst_source(cf_filename),
        folder_path="",
        logical_locator=f"archive:{dt.isoformat()}:entry={entry_id}",
        source_input_mode="archive",
        source_requested_from=base_url,
    )


# ---------------------------------------------------------------------------
# ---- Orchestration (task 5.3, 6.1, 6.2) ----
# ---------------------------------------------------------------------------


def _summary_to_json(s: ScrapeSummary) -> str:
    """Serialise ScrapeSummary to single-line JSON string.

    Parameters
    ----------
    s:
        ScrapeSummary instance to serialise.

    Returns
    -------
    str
        Single-line JSON string (no trailing newline) with 10 fields.
    """
    return json.dumps(
        {
            "started_at": s.started_at.isoformat(),
            "ended_at": s.ended_at.isoformat(),
            "date_range": [
                s.date_range[0].isoformat(),
                s.date_range[1].isoformat(),
            ],
            "candidate_entries": s.candidate_entries,
            "matched_sender": s.matched_sender,
            "fetched_messages": s.fetched_messages,
            "inserted_messages": s.inserted_messages,
            "skipped_duplicates": s.skipped_duplicates,
            "errors": s.errors,
            "exit_code": s.exit_code,
        },
        ensure_ascii=False,
    )


def _append_to_daily_log(
    summary_json: str,
    log_dir: Path,
    now: datetime,
) -> Path:
    """Append summary_json line to the daily log file.

    Parameters
    ----------
    summary_json:
        Single-line JSON string (from _summary_to_json).
    log_dir:
        Directory to write log files into (created if absent).
    now:
        Datetime used to determine log filename date.

    Returns
    -------
    Path
        Path of the log file written to.
    """
    log_file = log_dir / f"archive-scrape-{now.date().isoformat()}.log"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_file, mode="a", encoding="utf-8", newline="") as fh:
        fh.write(summary_json + "\n")
    return log_file


def run_scrape(
    from_date: date | None = None,
    to_date: date | None = None,
    *,
    senders: frozenset[str],
    dry_run: bool = False,
    database_path: Path | None = None,
    log_dir: Path | None = None,
) -> ScrapeSummary:
    """執行一次 archive scrape，回傳 :class:`ScrapeSummary`。

    Parameters
    ----------
    from_date, to_date:
        Inclusive 日期範圍。兩者皆 ``None`` 時預設只跑「今日」。落在 MLS
        retention 視窗外的日期將被略過並 emit warning。
    dry_run:
        ``True`` 時列舉並計數 entry，但 MUST NOT 對 DB 寫入。
    database_path:
        SQLite DB 路徑。``None`` 時用預設 ``mailattic.db``。
    log_dir:
        Daily log 目錄。``None`` 時用預設 ``Path.cwd() / LOG_DIR_NAME``。
        Tests 可傳入 ``tmp_path`` 避免污染 production log 目錄。

    Returns
    -------
    ScrapeSummary
        含 10 個欄位的 JSON-encodable summary，對應 spec Requirement 7。
    """
    started_at = datetime.now().astimezone()
    today = started_at.date()
    from_d = from_date if from_date is not None else today
    to_d = to_date if to_date is not None else today

    candidate_entries: int = 0
    matched_sender: int = 0
    fetched_messages: int = 0
    inserted_messages: int = 0
    skipped_duplicates: int = 0
    errors: int = 0
    exit_code: int = EXIT_OK

    try:
        creds = load_credentials()
        session = build_session(creds)
        base_url = creds.url.rstrip("/")

        # Fetch list page to discover available dates on the server
        list_html = _fetch_list_page(session, base_url, from_d)
        available_dates = parse_date_options(list_html)
        in_window, _ = validate_date_range(from_d, to_d, available_dates)

        conn = None
        if not dry_run:
            db_path = (
                database_path if database_path is not None else Path("mailattic.db")
            )
            initialize_database(db_path)
            conn = connect_database(db_path)

        try:
            for dt in in_window:
                # Fetch list for this date (menu navigation is cached on session)
                day_html = _fetch_list_page(session, base_url, dt)
                all_rows, _has_next, _ = parse_list_rows(day_html)
                candidate_entries += len(all_rows)
                matched_rows = [
                    row
                    for sender in sorted(senders)
                    for row in filter_by_sender(all_rows, sender)
                ]
                matched_sender += len(matched_rows)

                for row in matched_rows:
                    eid = row.entry_id
                    try:
                        first_recipient = (
                            row.recipient.split(",")[0].strip() if row.recipient else ""
                        )
                        html_q1 = _fetch_detail_q1(
                            session,
                            base_url,
                            eid,
                            dt,
                            sender=row.sender,
                            recipient=first_recipient,
                        )
                        # Build fallbacks for entries where the mail server omits
                        # Message-ID or renders date as "---".
                        # message_id fallback: "archive:<date>:entry=<N>"
                        # sent_at fallback: date + time_str parsed as CST (+08:00)
                        _fb_mid = f"archive:{dt.isoformat()}:entry={eid}"
                        _tz_cst = timezone(timedelta(hours=8))
                        try:
                            _h, _m, _s = (int(x) for x in row.time_str.split(":"))
                            _fb_sent_at: datetime | None = datetime(
                                dt.year, dt.month, dt.day, _h, _m, _s, tzinfo=_tz_cst
                            )
                        except Exception:
                            _fb_sent_at = datetime(
                                dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=_tz_cst
                            )
                        header = parse_detail_header(
                            html_q1,
                            fallback_message_id=_fb_mid,
                            fallback_sent_at=_fb_sent_at,
                        )
                        cf_filename = _extract_cf_filename(html_q1)
                        html_body = _fetch_detail_body(
                            session, base_url, eid, dt, cf_filename
                        )
                        body_text = parse_detail_body(html_body)
                        attachments = parse_attachment_links(html_q1)

                        fetched_messages += 1

                        if not dry_run and conn is not None:
                            msg = _build_normalized_message(
                                header,
                                body_text,
                                attachments,
                                cf_filename,
                                base_url,
                                str(eid),
                                dt,
                            )
                            result = persist_message(conn, msg)
                            conn.commit()
                            if result.inserted:
                                inserted_messages += 1
                            else:
                                skipped_duplicates += 1

                    except Exception as exc:
                        print(
                            f"PARSE_FAILED entry={eid} date={dt}"
                            f" err={type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
                        errors += 1
                        continue
        finally:
            if conn is not None:
                conn.close()

    except (EnvNotFoundError, EnvMissingFieldError) as exc:
        print(str(exc), file=sys.stderr)
        exit_code = exc.exit_code

    # Partial failure: no inserts succeeded but at least one error occurred
    if exit_code == EXIT_OK and inserted_messages == 0 and errors > 0:
        exit_code = EXIT_PARTIAL_FAILURE

    ended_at = datetime.now().astimezone()
    summary = ScrapeSummary(
        started_at=started_at,
        ended_at=ended_at,
        date_range=(from_d, to_d),
        candidate_entries=candidate_entries,
        matched_sender=matched_sender,
        fetched_messages=fetched_messages,
        inserted_messages=inserted_messages,
        skipped_duplicates=skipped_duplicates,
        errors=errors,
        exit_code=exit_code,
    )

    summary_json = _summary_to_json(summary)
    print(summary_json)

    _log_dir = log_dir if log_dir is not None else Path.cwd() / LOG_DIR_NAME
    log_path = _append_to_daily_log(summary_json, _log_dir, started_at)
    print(f"[archive_scrape] log: {log_path}", file=sys.stderr)

    return summary
