"""
CLI エントリーポイント(オーギヤ彦根店版)。

使い方:
    python -m collector.main daily
    python -m collector.main backfill --start 2026-07-22 --end 2026-09-21 [--start-url URL] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime

from .scraper import find_latest_report_url, new_page, scrape_report_page
from .supabase_client import SupabaseClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("collector.main")

TAG_URL = "https://min-repo.com/tag/%E3%82%AA%E3%83%BC%E3%82%AE%E3%83%A4%E5%BD%A6%E6%A0%B9%E5%BA%97/"
MAX_BACKFILL_DAYS = 120


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


async def collect_one(page, report_url: str, client: SupabaseClient, run_mode: str):
    """1日分を収集してSupabaseに保存する。DayReport (prev_day_url含む) を返す。"""
    try:
        report = await scrape_report_page(page, report_url)
    except Exception as e:
        logger.error("収集失敗: %s (%r)", report_url, e)
        client.log_collection(
            report_date=None,
            source_url=report_url,
            status="failed",
            rows_collected=0,
            run_mode=run_mode,
            error_message=str(e),
        )
        raise

    if not report.rows:
        logger.warning("データ0件: %s (%s)", report_url, report.report_date)
        client.log_collection(
            report_date=report.report_date.isoformat(),
            source_url=report_url,
            status="no_data",
            rows_collected=0,
            run_mode=run_mode,
        )
        return report

    saved = client.upsert_day_report(report)
    client.log_collection(
        report_date=report.report_date.isoformat(),
        source_url=report_url,
        status="success",
        rows_collected=saved,
        run_mode=run_mode,
    )
    logger.info("収集成功: %s -> %d行保存", report.report_date, saved)
    return report


async def run_daily() -> None:
    client = SupabaseClient()
    playwright, browser, context, page = await new_page(headless=True)
    try:
        latest_url = await find_latest_report_url(page, TAG_URL)
        if not latest_url:
            raise RuntimeError("最新レポートURLが見つかりませんでした")
        await collect_one(page, latest_url, client, run_mode="daily")
    finally:
        await browser.close()
        await playwright.stop()

    try:
        logger.info("machine_historyを再構築します")
        client.rebuild_machine_history()
        logger.info("machine_history再構築完了")
    except Exception as e:
        logger.error("machine_history再構築に失敗しました: %r", e)
    finally:
        client.close()


async def run_backfill(start: date, end: date, start_url: str | None, force: bool) -> None:
    if (end - start).days > MAX_BACKFILL_DAYS:
        raise RuntimeError(f"バックフィル期間が長すぎます(最大{MAX_BACKFILL_DAYS}日)")

    client = SupabaseClient()
    playwright, browser, context, page = await new_page(headless=True)

    try:
        collected_dates = set() if force else client.get_collected_dates(run_mode="backfill")

        if start_url:
            current_url = start_url
        else:
            current_url = await find_latest_report_url(page, TAG_URL)
            if not current_url:
                raise RuntimeError("開始URLが見つかりませんでした")

        while current_url:
            try:
                report = await collect_one(page, current_url, client, run_mode="backfill")
            except Exception:
                # collect_one内でログ済み。前日リンクが分からないため打ち切る。
                logger.error("このURLで打ち切ります(前日リンクが取得できないため): %s", current_url)
                break

            if report.report_date < start:
                logger.info("開始日(%s)より前に到達したため終了します: %s", start, report.report_date)
                break
            if report.report_date > end:
                logger.info("終了日(%s)より後のデータでした。スキップして続行: %s", end, report.report_date)
                current_url = report.prev_day_url
                continue

            current_url = report.prev_day_url

    finally:
        await browser.close()
        await playwright.stop()

    try:
        logger.info("machine_historyを再構築します")
        client.rebuild_machine_history()
        logger.info("machine_history再構築完了")
    except Exception as e:
        logger.error("machine_history再構築に失敗しました: %r", e)
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("daily")

    backfill_parser = sub.add_parser("backfill")
    backfill_parser.add_argument("--start", required=True, type=_parse_date)
    backfill_parser.add_argument("--end", required=True, type=_parse_date)
    backfill_parser.add_argument("--start-url", default=None)
    backfill_parser.add_argument("--force", action="store_true")

    args = parser.parse_args()

    if args.command == "daily":
        asyncio.run(run_daily())
    elif args.command == "backfill":
        asyncio.run(run_backfill(args.start, args.end, args.start_url, args.force))


if __name__ == "__main__":
    main()
