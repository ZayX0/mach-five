"""Offline self-check for clob.py — canned payloads, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clob import _jsonish, _levels


def check() -> None:
    assert _jsonish('["Cubs","Yankees"]') == ["Cubs", "Yankees"]
    assert _jsonish("not json") == "not json"
    lv = _levels([{"price": "0.57", "size": "1000"}, {"bad": 1}])
    assert lv == [(0.57, 1000.0)], lv
    print("ok")


if __name__ == "__main__":
    check()
