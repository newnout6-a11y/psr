import sys
sys.stdout.reconfigure(encoding='utf-8')
import sqlite3
import json

conn = sqlite3.connect('data/runtime/proposals.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT candidate_id, project_id, platform, title, platform_data, status FROM candidates WHERE platform_data LIKE '%files%' ORDER BY candidate_id DESC LIMIT 10",
).fetchall()

found = False
for r in rows:
    pd = json.loads(r["platform_data"]) if r["platform_data"] else {}
    files = pd.get("files", [])
    if not isinstance(files, list) or len(files) == 0:
        continue
    found = True
    print(f"#{r['candidate_id']} {r['platform']} {r['project_id']} {r['title'][:60]}")
    print(f"  status={r['status']}")
    print(f"  files={len(files)}")
    for f in files[:5]:
        name = f.get("fname") or f.get("name") or f.get("filename") or "?"
        url = str(f.get("url", ""))[:100]
        size = f.get("size", "?")
        print(f"    - {name} (size={size}) url={url}")
    print()

if not found:
    print("No candidates with files found in DB")
    # Show last 5 candidates regardless
    rows2 = conn.execute(
        "SELECT candidate_id, project_id, platform, title, status FROM candidates ORDER BY candidate_id DESC LIMIT 5",
    ).fetchall()
    print("\nLast 5 candidates:")
    for r in rows2:
        print(f"  #{r['candidate_id']} {r['platform']} {r['project_id']} {r['title'][:50]} status={r['status']}")

conn.close()
