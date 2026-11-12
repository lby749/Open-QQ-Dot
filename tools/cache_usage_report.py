"""Offline cache hit report for a single friend directory (no network calls)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from usage_accounting import cache_summary


def main():
    parser = argparse.ArgumentParser(description='Provider-reported cache token hit rates')
    parser.add_argument('friend_directory', type=Path)
    parser.add_argument('--calls', type=int, default=100)
    args = parser.parse_args()
    report = cache_summary(args.friend_directory, args.calls)
    if report['hit_rate'] is None:
        print('No verified API cache usage yet; no hit rate can be calculated.')
    else:
        print(f"Verified API requests: {report['calls']} | cache hit rate: {report['hit_rate']:.1%}")
        print(f"Input: {report['input_tokens']:,} | cached: {report['cached_input_tokens']:,} | written: {report['cache_write_input_tokens']:,}")
        for model, row in sorted(report['by_model'].items()):
            rate = row['cached_input_tokens'] / row['input_tokens'] if row['input_tokens'] else 0
            print(f"  {model}: {row['calls']} requests, {rate:.1%} cached input")
    print(f"Excluded older/estimated/malformed records: {report['excluded']}")


if __name__ == '__main__':
    main()
