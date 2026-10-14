"""
Config alignment audit.
For every key in every config JSON, greps the Python source code to check if it's read.
Outputs: PASS (key found in src/), WARN (found only in tests/), MISS (not found anywhere).
"""

import json
import subprocess
from pathlib import Path

BASE = Path(__file__).parent.parent
CONFIGS = [
    "config/global.json",
    "config/risk.json",
    "config/execution.json",
    "config/universe.json",
    "config/intelligence.json",
    "config/operator.json",
    "config/supervisor.json",
    "config/news.json",
    "config/dashboard.json",
    "config/strategies_registry.json",
]
SRC_DIR = BASE / "src"
TEST_DIR = BASE / "tests"


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.startswith("_"):
                continue
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif not isinstance(obj, list):
        yield prefix, obj


def grep_files(key: str, directory: Path) -> list[str]:
    try:
        r = subprocess.run(
            ["grep", "-r", "--include=*.py", "-l", key, str(directory)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip().splitlines()
    except Exception:
        return []


def main() -> None:
    results = []
    for cfg_path in CONFIGS:
        path = BASE / cfg_path
        if not path.exists():
            print(f"\n{'=' * 60}\nMISSING: {cfg_path}\n{'=' * 60}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n{'=' * 60}\nCONFIG: {cfg_path}\n{'=' * 60}")
        for key, value in flatten(data):
            leaf = key.split(".")[-1]
            src_hits = [f for f in grep_files(leaf, SRC_DIR) if "config_audit" not in f]
            if src_hits:
                status, detail = (
                    "PASS",
                    ", ".join(Path(f).relative_to(BASE).as_posix() for f in src_hits[:2]),
                )
            else:
                test_hits = grep_files(leaf, TEST_DIR)
                if test_hits:
                    status, detail = "WARN", "only in tests"
                else:
                    status, detail = "MISS", "NOT IN ANY SOURCE FILE"
            mark = "OK" if status == "PASS" else ("--" if status == "WARN" else "XX")
            print(f"  [{mark}] {status:4}  {key:<52} = {str(value)[:18]:<20}  {detail}")
            results.append((status, cfg_path, key, value))

    total, passed = len(results), sum(1 for r in results if r[0] == "PASS")
    warned, missed = (
        sum(1 for r in results if r[0] == "WARN"),
        sum(1 for r in results if r[0] == "MISS"),
    )
    print(f"\n{'=' * 60}")
    print(f"TOTAL {total} keys  |  PASS {passed}  |  WARN {warned}  |  MISS {missed}")
    print(f"{'=' * 60}")
    if missed:
        print("\nMISSED keys — config values the code never reads:")
        for r in results:
            if r[0] == "MISS":
                print(f"  XX  {r[1]}  ->  {r[2]}  =  {r[3]}")


if __name__ == "__main__":
    main()
