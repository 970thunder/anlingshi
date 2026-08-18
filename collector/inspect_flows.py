"""Summarize a captured flows.jsonl file without printing payload secrets."""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/flows.jsonl")
hosts = collections.Counter()
paths = collections.Counter()
keywords = collections.Counter()
total = 0
if not path.exists():
    raise SystemExit(f"未找到 {path}，先启动抓包并操作一局。")
for line in path.open(encoding="utf-8"):
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        continue
    total += 1
    hosts[item.get("host", "unknown")] += 1
    url = item.get("url", "")
    paths[url.split("?", 1)[0]] += 1
    text = json.dumps(item, ensure_ascii=False).lower()
    for keyword in ("winner", "result", "win", "red", "blue", "红", "蓝", "round", "match", "settle"):
        if keyword in text:
            keywords[keyword] += 1
print(f"记录总数: {total}")
print("\n域名:")
for host, count in hosts.most_common():
    print(f"  {count:>4}  {host}")
print("\n接口:")
for endpoint, count in paths.most_common(30):
    print(f"  {count:>4}  {endpoint}")
print("\n关键词命中次数（仅用于定位，不代表已解析）:")
print("  " + ", ".join(f"{key}={value}" for key, value in keywords.most_common()))
