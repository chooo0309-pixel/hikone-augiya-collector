"""
Supabase REST API (PostgREST) を叩くための薄いクライアント。
service_role キーを使うため RLS はバイパスされる(書き込み専用の用途)。

ACT GOLD長浜のcollectorと同じSupabaseプロジェクトを共有するが、
テーブルはオーギヤ彦根店専用のものを使う(hikone_augiya_*)。
"""

from __future__ import annotations

import logging
import os

import httpx

from .scraper import DayReport

logger = logging.getLogger("collector.supabase_client")

DAILY_DATA_TABLE = "hikone_augiya_daily_data"
COLLECTION_LOG_TABLE = "hikone_augiya_collection_log"
REBUILD_HISTORY_RPC = "rebuild_hikone_augiya_machine_history"


class SupabaseClient:
    def __init__(self) -> None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "環境変数 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY が設定されていません"
            )
        self.base_url = url.rstrip("/") + "/rest/v1"
        self.client = httpx.Client(
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def upsert_day_report(self, report: DayReport) -> int:
        """1日分のレポートをhikone_augiya_daily_dataにupsertする。挿入/更新した行数を返す。"""
        payload = []
        for row in report.rows:
            payload.append(
                {
                    "report_date": report.report_date.isoformat(),
                    "machine_name": row.machine_name,
                    "unit_number": row.unit_number,
                    "diff_medals": row.diff_medals,
                    "game_count": row.game_count,
                    "payout_rate": row.payout_rate,
                    "bb_count": row.bb_count,
                    "rb_count": row.rb_count,
                    "composite_denom": row.composite_denom,
                    "bb_rate_denom": row.bb_rate_denom,
                    "rb_rate_denom": row.rb_rate_denom,
                    "source_url": report.source_url,
                }
            )

        if not payload:
            return 0

        resp = self.client.post(
            f"{self.base_url}/{DAILY_DATA_TABLE}",
            params={"on_conflict": "report_date,machine_name,unit_number"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload,
        )
        resp.raise_for_status()
        return len(payload)

    def log_collection(
        self,
        report_date: str | None,
        source_url: str | None,
        status: str,
        rows_collected: int,
        run_mode: str,
        error_message: str | None = None,
    ) -> None:
        payload = {
            "report_date": report_date,
            "source_url": source_url,
            "status": status,
            "rows_collected": rows_collected,
            "error_message": error_message,
            "run_mode": run_mode,
        }
        resp = self.client.post(
            f"{self.base_url}/{COLLECTION_LOG_TABLE}",
            params={"on_conflict": "report_date,run_mode"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload,
        )
        resp.raise_for_status()

    def get_collected_dates(self, run_mode: str = "backfill") -> set[str]:
        resp = self.client.get(
            f"{self.base_url}/{COLLECTION_LOG_TABLE}",
            params={
                "select": "report_date",
                "run_mode": f"eq.{run_mode}",
                "status": "eq.success",
            },
        )
        resp.raise_for_status()
        return {row["report_date"] for row in resp.json() if row.get("report_date")}

    def rebuild_machine_history(self) -> None:
        """hikone_augiya_daily_data全体からhikone_augiya_machine_historyを再構築する(SupabaseのRPC関数を呼ぶ)。"""
        resp = self.client.post(f"{self.base_url}/rpc/{REBUILD_HISTORY_RPC}", json={})
        resp.raise_for_status()

    def close(self) -> None:
        self.client.close()
