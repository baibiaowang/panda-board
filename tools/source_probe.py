#!/usr/bin/env python3
"""Read-only public data-source probe. Does not ingest or claim exchange-wide coverage."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sources import get_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True)
    parser.add_argument('--source', choices=['eastmoney', 'cninfo'], default='eastmoney')
    args = parser.parse_args()
    date.fromisoformat(args.day)
    source = get_source(args.source)
    try:
        counts = source.probe_day_counts([args.day])
        result = {'ok': args.day in counts, 'source': args.source, 'date': args.day,
                  'source_count': counts.get(args.day), 'note': 'null 表示请求失败或未知，不是零公告'}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['ok'] else 1
    finally:
        source.close()


if __name__ == '__main__': sys.exit(main())
