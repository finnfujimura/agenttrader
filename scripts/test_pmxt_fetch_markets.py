from __future__ import annotations

import argparse
import sys

import pmxt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test pmxt fetch_markets against Polymarket or Kalshi."
    )
    parser.add_argument(
        "--exchange",
        choices=["polymarket", "kalshi"],
        default="polymarket",
        help="Which PMXT exchange client to query.",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Optional search text to pass to fetch_markets.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Maximum number of markets to print.",
    )
    parser.add_argument(
        "--status",
        default="active",
        help="Optional status filter passed through to PMXT.",
    )
    args = parser.parse_args()

    client = pmxt.Polymarket() if args.exchange == "polymarket" else pmxt.Kalshi()

    kwargs = {"limit": args.limit}
    if args.status:
        kwargs["status"] = args.status

    try:
        markets = client.fetch_markets(query=args.query, **kwargs)
    except Exception as exc:
        print(f"fetch_markets failed: {exc}")
        return 1

    if not markets:
        print("No markets returned.")
        return 0

    for market in markets:
        print(f"{market.market_id}	{market.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
