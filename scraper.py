"""
ACT GOLD長浜のスロットデータをmin-repo.comから収集するスクレイパー。

min-repo.comはJavaScriptでレンダリングされるため、Playwrightの
ヘッドレスブラウザを使用する。

これまでの調査で判明したこと:
- 個別レポートページ(例: https://min-repo.com/3363553/)には、
  台番ごとの個別データそのものは無く、「全台データ一覧・差枚ランキング」
  というリンク(href: "?kishu=all")の先に個別台データ一覧がある。
- そのURL(<report_url>?kishu=all)には、
  header=['機種','台番','差枚','G数','出率'] の5列・約300行超のテーブルがある。
  BB/RB/合成等の列は含まれていない(スキーマ上NULL許容なのでそのまま欠損として扱う)。
- min-repo.comは間欠的にBot対策/レート制限とみられる挙動を示し、
  レポートページ本体・全台データ一覧ページのどちらでも、
  「テーブルやリンクが一時的に見つからない」状態が起こりうる。
  再試行すると成功することが多いため、両方の取得ステップにリトライを入れている。
- page.goto(..., wait_until="networkidle") はこのサイトでは高確率でタイムアウトする
  (広告等が継続的に通信するため)。wait_until="domcontentloaded" + 明示的なtimeoutを使う。
- 前日リンクのhrefは相対URLで書かれていることがあるため、
  get_attribute("href") ではなく evaluate("el => el.href") で
  ブラウザ解決後の絶対URLを取得する。
- 同じURLに短時間で連続アクセスすると失敗しやすくなる可能性があるため、
  呼び出し側(main.py)は同一ページへの再ナビゲーションを避けること。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from playwright.async_api import async_playwright

logger = logging.getLogger("collector.scraper")

NAV_TIMEOUT_MS = 45000
RETRY_COUNT = 4
RETRY_DELAY_MS = 6000

# 個別台データ表のヘッダー -> 内部フィールド名
COLUMN_MAP = {
    "機種": "machine_name",
    "台番": "unit_number",
    "差枚": "diff_medals",
    "G数": "game_count",
    "出率": "payout_rate",
    "BB": "bb_count",
    "RB": "rb_count",
    "合成": "composite_denom",
    "BB率": "bb_rate_denom",
    "RB率": "rb_rate_denom",
}


@dataclass
class MachineRow:
    machine_name: str
    unit_number: int
    diff_medals: int | None = None
    game_count: int | None = None
    payout_rate: float | None = None
    bb_count: int | None = None
    rb_count: int | None = None
    composite_denom: int | None = None
    bb_rate_denom: int | None = None
    rb_rate_denom: int | None = None


@dataclass
class DayReport:
    report_date: date
    source_url: str
    rows: list[MachineRow] = field(default_factory=list)
    prev_day_url: str | None = None


def _parse_int(text: str) -> int | None:
    if text is None:
        return None
    t = text.strip().replace(",", "")
    if t in ("", "-", "--", "―", "N/A"):
        return None
    m = re.match(r"^[+\-]?\d+$", t)
    if not m:
        return None
    return int(t)


def _parse_float(text: str) -> float | None:
    if text is None:
        return None
    t = text.strip().replace(",", "").replace("%", "")
    if t in ("", "-", "--", "―", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _parse_fraction_denom(text: str) -> int | None:
    """'89/304' のような分数表記から分母(または分子)を int で取り出す補助関数。
    現状の個別台一覧の列には使わないが、将来 BB/RB 率が出てきた場合のために残す。"""
    if text is None:
        return None
    t = text.strip()
    m = re.match(r"^(\d+)\s*/\s*(\d+)$", t)
    if not m:
        return _parse_int(t)
    return int(m.group(2))


def _extract_date_from_text(text: str) -> date | None:
    """フォールバック用: 'YYYY年M月D日' 形式の日付を抽出する。
    注意: ページ本文中のこの表記は「実プレー日」ではなく「更新日」の
    ことがあり、実プレー日の翌日になっているケースが確認されている。
    可能な限り _extract_report_date() を使うこと。"""
    if not text:
        return None
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def _extract_report_date(title: str, body_text: str) -> date | None:
    """実際のプレー日を抽出する。

    タイトル(例: '9/20(日) act gold長浜 | ...')の 'M/D(曜日)' 表記が
    実プレー日そのものだが年が含まれていないため、本文中の
    'YYYY年M月D日'(更新日、プレー日の翌日であることが多い)から
    年だけを借用して組み立てる。タイトルから月日が取れない場合は
    本文の日付にそのままフォールバックする。
    """
    body_date = _extract_date_from_text(body_text)
    year = body_date.year if body_date else None

    m = re.search(r"(\d{1,2})/(\d{1,2})\s*\(", title or "")
    if m and year:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            candidate = date(year, mo, d)
        except ValueError:
            candidate = None
        if candidate:
            if body_date and (body_date - candidate).days > 300:
                try:
                    candidate = date(year - 1, mo, d)
                except ValueError:
                    pass
            elif body_date and (candidate - body_date).days > 300:
                try:
                    candidate = date(year + 1, mo, d)
                except ValueError:
                    pass
            return candidate

    return body_date


async def new_page(headless: bool = True):
    """Playwrightのbrowser/context/pageをiPhone風UAで初期化する。"""
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=headless)
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.5 Mobile/15E148 Safari/604.1"
        ),
        viewport={"width": 390, "height": 844},
        locale="ja-JP",
    )
    page = await context.new_page()
    return playwright, browser, context, page


async def _find_all_units_link(page) -> str | None:
    candidates = page.locator(
        "a:has-text('全台データ一覧'), button:has-text('全台データ一覧')"
    )
    count = await candidates.count()
    if count == 0:
        return None
    return await candidates.first.evaluate("el => el.href || null")


async def _find_prev_day_url(page) -> str | None:
    candidates = page.locator("a:has-text('前日')")
    count = await candidates.count()
    if count == 0:
        return None
    return await candidates.first.evaluate("el => el.href || null")


async def _dump_unit_table(page) -> list[MachineRow] | None:
    """ページ内の<table>から、台番ごとの個別データ表を見つけてパースする。"""
    tables = page.locator("table")
    table_count = await tables.count()
    if table_count == 0:
        return None

    target = None
    max_rows = -1
    target_headers: list[str] = []
    for i in range(table_count):
        table = tables.nth(i)
        header_cells = table.locator("tr:first-child th, tr:first-child td")
        header_texts = [t.strip() for t in await header_cells.all_inner_texts()]
        if "台番" not in header_texts:
            continue
        row_count = max(0, await table.locator("tr").count() - 1)
        if row_count > max_rows:
            max_rows = row_count
            target = table
            target_headers = header_texts

    if target is None:
        return None

    field_order = [COLUMN_MAP.get(h) for h in target_headers]

    rows_locator = target.locator("tr")
    total_rows = await rows_locator.count()
    results: list[MachineRow] = []

    for i in range(1, total_rows):
        cells = rows_locator.nth(i).locator("td, th")
        texts = [t.strip() for t in await cells.all_inner_texts()]
        if len(texts) != len(field_order):
            continue

        values: dict[str, str] = {}
        for field_name, text in zip(field_order, texts):
            if field_name:
                values[field_name] = text

        unit_number = _parse_int(values.get("unit_number", ""))
        machine_name = values.get("machine_name", "").strip()
        if unit_number is None or not machine_name:
            continue

        row = MachineRow(
            machine_name=machine_name,
            unit_number=unit_number,
            diff_medals=_parse_int(values.get("diff_medals", "")),
            game_count=_parse_int(values.get("game_count", "")),
            payout_rate=_parse_float(values.get("payout_rate", "")),
            bb_count=_parse_int(values["bb_count"]) if "bb_count" in values else None,
            rb_count=_parse_int(values["rb_count"]) if "rb_count" in values else None,
            composite_denom=_parse_fraction_denom(values["composite_denom"]) if "composite_denom" in values else None,
            bb_rate_denom=_parse_fraction_denom(values["bb_rate_denom"]) if "bb_rate_denom" in values else None,
            rb_rate_denom=_parse_fraction_denom(values["rb_rate_denom"]) if "rb_rate_denom" in values else None,
        )
        results.append(row)

    return results


async def _goto_with_retry(page, url: str, referer: str | None, check) -> bool:
    """urlへ遷移し、check(page)がTrueを返すまでリトライする。成功したらTrueを返す。"""
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            kwargs = {"wait_until": "domcontentloaded", "timeout": NAV_TIMEOUT_MS}
            if referer:
                kwargs["referer"] = referer
            await page.goto(url, **kwargs)
            await page.wait_for_timeout(2000)
            if await check(page):
                return True
            logger.warning(
                "条件を満たさなかったためリトライします (試行 %d/%d): %s", attempt, RETRY_COUNT, url
            )
        except Exception as e:
            logger.warning("遷移中にエラー (試行 %d/%d): %r (%s)", attempt, RETRY_COUNT, e, url)

        if attempt < RETRY_COUNT:
            await page.wait_for_timeout(RETRY_DELAY_MS)

    return False


async def scrape_report_page(page, report_url: str) -> DayReport:
    """1日分のレポートページから、台番ごとの個別データを収集する。"""

    all_units_url_holder: dict[str, str | None] = {"url": None}
    prev_day_url_holder: dict[str, str | None] = {"url": None}
    date_holder: dict[str, date | None] = {"date": None}

    async def check_report_page(p) -> bool:
        body_text = await p.inner_text("body")
        title = await p.title()
        date_holder["date"] = _extract_report_date(title, body_text)
        all_units_url_holder["url"] = await _find_all_units_link(p)
        prev_day_url_holder["url"] = await _find_prev_day_url(p)
        return all_units_url_holder["url"] is not None and date_holder["date"] is not None

    ok = await _goto_with_retry(page, report_url, referer=None, check=check_report_page)
    if not ok:
        raise RuntimeError(
            f"レポートページから必要な情報(日付・全台データ一覧リンク)を取得できませんでした: {report_url}"
        )

    all_units_url = all_units_url_holder["url"]
    report_date = date_holder["date"]
    prev_day_url = prev_day_url_holder["url"]

    rows_holder: dict[str, list[MachineRow] | None] = {"rows": None}

    async def check_unit_table(p) -> bool:
        rows_holder["rows"] = await _dump_unit_table(p)
        return bool(rows_holder["rows"])

    ok = await _goto_with_retry(page, all_units_url, referer=report_url, check=check_unit_table)
    if not ok:
        raise RuntimeError(f"全台データ一覧から台番データを取得できませんでした: {all_units_url}")

    return DayReport(
        report_date=report_date,
        source_url=report_url,
        rows=rows_holder["rows"] or [],
        prev_day_url=prev_day_url,
    )


async def find_latest_report_url(page, tag_url: str) -> str | None:
    """タグ一覧ページから最新のレポートURLを1件取得する。"""
    logger.info("タグ一覧ページへ遷移: %s", tag_url)
    await page.goto(tag_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_timeout(1500)

    links = page.locator("a[href^='https://min-repo.com/']")
    count = await links.count()
    pattern = re.compile(r"^https://min-repo\.com/\d+/?$")

    for i in range(count):
        href = await links.nth(i).evaluate("el => el.href || null")
        if href and pattern.match(href):
            return href

    return None
