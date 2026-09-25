"""
診断用スクリプト: 指定した1ページ(TARGET_URL環境変数)を開いて、
ページ内の全<a>タグのテキストとhrefを一覧表示する。

「前日」リンクがscraperの想定と違うテキスト/構造になっているケース
(例: 前日が定休日で表示が変わる等)を調べるために使う。
Supabaseへの保存はしない。
"""

from __future__ import annotations

import asyncio
import logging
import os

from .scraper import new_page, NAV_TIMEOUT_MS

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("collector.debug_links")

TARGET_URL = os.environ.get("TARGET_URL", "https://min-repo.com/3258573/")


async def main() -> None:
    playwright, browser, context, page = await new_page(headless=True)
    try:
        logger.info("=== ページへ遷移: %s ===", TARGET_URL)
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(2500)

        title = await page.title()
        logger.info("title: %s", title)

        links = page.locator("a")
        count = await links.count()
        logger.info("=== 全リンク数: %d ===", count)

        for i in range(count):
            try:
                text = (await links.nth(i).inner_text()).strip().replace("\n", " ")
            except Exception:
                text = ""
            try:
                href = await links.nth(i).evaluate("el => el.href || null")
            except Exception:
                href = None
            if not text and not href:
                continue
            if (href and "min-repo.com" in href) or ("日" in text):
                logger.info("[%d] text=%r href=%s", i, text[:40], href)

    except Exception as e:
        logger.error("=== エラー: %r ===", e)
    finally:
        await browser.close()
        await playwright.stop()


if __name__ == "__main__":
    asyncio.run(main())
