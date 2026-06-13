"""
Batch metadata audit for all historically traded Kalshi markets.

Fetches Kalshi market metadata for every distinct ticker in the orders table,
compares parser interpretation to API fields, and persists results to
contract_semantics. Markets with parser_matches_metadata=False are flagged
unsupported — index.py will skip them.

Run after experiment ends or whenever new ticker formats are suspected:
    python scripts/batch_market_audit.py
    python scripts/batch_market_audit.py --dry-run   # fetch + compare, no DB write
    python scripts/batch_market_audit.py --limit 50  # only process first N tickers

Expected runtime: ~2 minutes for 300 tickers (Kalshi API concurrency limited).
404 responses (expired markets) are handled gracefully — not flagged unsupported.
"""
import asyncio
import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from src.services.kalshi_client import client
from src.db.dwtrader import DWTraderDB
from src.risk.contract_audit import fetch_and_audit_metadata


def _get_traded_tickers(db: DWTraderDB) -> List[str]:
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT ticker FROM orders ORDER BY ticker")
        return [row[0] for row in c.fetchall()]


async def _audit_one(
    ticker: str, db: DWTraderDB, dry_run: bool
) -> dict:
    """Audit a single ticker. Skips DB write when dry_run=True."""
    if dry_run:
        from src.risk.contract_audit import audit_ticker, _compare_parser_to_metadata
        audit = audit_ticker(ticker)
        try:
            meta = await client.get_market(ticker)
        except Exception:
            meta = None
        if meta:
            matches, dir_meta, mismatches = _compare_parser_to_metadata(audit, meta)
        else:
            matches, dir_meta, mismatches = None, None, None
        return {
            "ticker": ticker,
            "parser_matches_metadata": matches,
            "direction_meta": dir_meta,
            "mismatch_details": mismatches,
            "meta_found": meta is not None,
            "unsupported": matches is False,
        }
    else:
        return await fetch_and_audit_metadata(ticker, client, db)


async def main(limit: Optional[int], dry_run: bool) -> None:
    db = DWTraderDB()
    tickers = _get_traded_tickers(db)
    if limit:
        tickers = tickers[:limit]

    print(f"\nBATCH MARKET AUDIT — {len(tickers)} distinct tickers")
    print(f"Mode: {'DRY-RUN (no DB writes)' if dry_run else 'LIVE (will write to contract_semantics)'}")
    print("=" * 70)

    sem = asyncio.Semaphore(8)  # max 8 concurrent Kalshi API calls
    results = []

    async def audited(t: str):
        async with sem:
            try:
                r = await _audit_one(t, db, dry_run)
                results.append(r)
                status = (
                    "MATCH" if r.get("parser_matches_metadata") is True
                    else "MISMATCH" if r.get("parser_matches_metadata") is False
                    else "404/unknown" if not r.get("meta_found", True)
                    else "NOT_FETCHED"
                )
                print(f"  {t:<42} {status}")
            except Exception as e:
                print(f"  {t:<42} ERROR: {e}")
                results.append({"ticker": t, "error": str(e)})

    await asyncio.gather(*[audited(t) for t in tickers])

    # Summary
    matched   = sum(1 for r in results if r.get("parser_matches_metadata") is True)
    mismatched = sum(1 for r in results if r.get("parser_matches_metadata") is False)
    unavail   = sum(1 for r in results if r.get("parser_matches_metadata") is None)
    errors    = sum(1 for r in results if "error" in r)

    print("\n" + "=" * 70)
    print(f"SUMMARY:")
    print(f"  Total   : {len(tickers)}")
    print(f"  Match   : {matched}")
    print(f"  Mismatch: {mismatched}  << will be marked unsupported")
    print(f"  Unavail : {unavail}  (404 — market expired, left as allow)")
    print(f"  Errors  : {errors}")

    if mismatched:
        print(f"\nMISMATCHES (will block trading):")
        for r in results:
            if r.get("parser_matches_metadata") is False:
                print(f"  {r['ticker']}")
                print(f"    direction: {r.get('direction_meta')}")
                print(f"    details  : {r.get('mismatch_details')}")

    below_markets = [r for r in results if r.get("direction_meta") == "BELOW"]
    if below_markets:
        print(f"\nBELOW markets (p_yes will be inverted in index.py):")
        for r in below_markets:
            print(f"  {r['ticker']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch Kalshi market metadata audit.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only audit first N tickers (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + compare but do not write to DB")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.dry_run))
