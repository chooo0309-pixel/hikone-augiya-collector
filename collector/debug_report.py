"""
テストスクリプト: タグ一覧ページから最新レポートを見つけて1日分だけ実際に取得し、
リトライ機構込みで正しくデータが取れるか確認する。Supabaseへの保存はしない。

オーギヤ彦根店はこのcollectorで初めて扱う店舗のため、まずこのdebugスクリプトを
GitHub Actions上で実行し、min-repo.comのページ構造がACT GOLD長浜と同じ形
(全台データ一覧ページに機種/台番/差枚/G数/出率のテーブルがある)かどうかを
確認してから daily/backfill を回すこと。
"""

from __future__ import annotations

import asyncio
import logging

from .scraper import new_page, scrape_report_page, find_latest_report_url

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("collector.debug_report")

TAG_URL = "https://min-repo.com/tag/%E3%82%AA%E3%83%BC%E3%82%AE%E3%83%A4%E5%BD%A6%E6%A0%B9%E5%BA%97/"


async def main() -> None:
    playwright, browser, context, page = await new_page(headless=True)
    try:
        logger.info("=== タグ一覧ページから最新レポートURLを探索 ===")
        latest_url = await find_latest_report_url(page, TAG_URL)
        logger.info("latest_url: %s", latest_url)
        if not latest_url:
            logger.error("最新レポートURLが見つかりませんでした。タグURLやページ構造を確認してください。")
            return

        logger.info("=== レポートページを取得 ===")
        report = await scrape_report_page(page, latest_url)
        logger.info("=== 収集成功 ===")
        logger.info("report_date: %s", report.report_date)
        logger.info("source_url: %s", report.source_url)
        logger.info("prev_day_url: %s", report.prev_day_url)
        logger.info("行数: %d", len(report.rows))
        for row in report.rows[:10]:
            logger.info(
                "台番%s %s 差枚=%s G数=%s 出率=%s",
                row.unit_number, row.machine_name, row.diff_medals, row.game_count, row.payout_rate,
            )
    except Exception as e:
        logger.error("=== 収集失敗: %r ===", e)
    finally:
        await browser.close()
        await playwright.stop()


if __name__ == "__main__":
    asyncio.run(main())
