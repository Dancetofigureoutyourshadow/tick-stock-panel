"""Run a read-only CZSC differential audit over random local A shares."""
from __future__ import annotations

import argparse
import json

from app.services.chan_czsc_diff import audit_local_edge_cases, compare_random_local_stocks
from app.tickflow.repository import DataStore, KlineRepository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-bars", type=int, default=800)
    parser.add_argument("--edge-cases", action="store_true")
    parser.add_argument("--max-symbols", type=int, default=80)
    args = parser.parse_args()
    store = DataStore()
    try:
        repo = KlineRepository(store)
        if args.edge_cases:
            result = audit_local_edge_cases(
                repo,
                seed=args.seed,
                max_symbols=max(1, args.max_symbols),
                window_bars=max(120, args.max_bars),
            )
        else:
            result = compare_random_local_stocks(
                repo,
                count=max(1, args.count),
                seed=args.seed,
                max_bars=max(120, args.max_bars),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.edge_cases:
            return 0 if result["passed"] else 1
        return 0 if result["checked"] == result["requested"] == result["passed"] else 1
    finally:
        store.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
