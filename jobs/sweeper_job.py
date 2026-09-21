# Packaging: make sibling repo packages importable under a serverless task
# (`__file__` is undefined in the serverless exec kernel; locate repo root by
# walking cwd / sys.path for the dir that holds agent/ and common/).
import os, sys


def _ensure_repo_root_on_path() -> None:
    starts = []
    try:
        starts.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    starts.append(os.getcwd())
    starts.extend(p for p in sys.path if p)
    checked = set()
    for start in starts:
        d = os.path.abspath(start)
        for _ in range(8):
            if d in checked:
                break
            checked.add(d)
            if os.path.isdir(os.path.join(d, "agent")) and os.path.isdir(os.path.join(d, "common")):
                if d not in sys.path:
                    sys.path.insert(0, d)
                return
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent


_ensure_repo_root_on_path()

from datetime import datetime

def abandoned_turn_ids(turns: list[dict], *, now: datetime, ttl_seconds: int) -> list[str]:
    return [t["turn_id"] for t in turns
            if t["state"] == "open"
            and (now - t["created_at"]).total_seconds() > ttl_seconds]

def main() -> None:  # Job entrypoint (verified live in Task 9)
    from datetime import timezone
    from common.lakebase import connect
    ttl = int(os.environ.get("TURN_TTL_SECONDS", "900"))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT turn_id, state, created_at FROM turns WHERE state='open'")
            turns = [{"turn_id": r[0], "state": r[1], "created_at": r[2]} for r in cur.fetchall()]
            stale = abandoned_turn_ids(turns, now=datetime.now(timezone.utc), ttl_seconds=ttl)
            if stale:
                cur.execute("UPDATE turns SET state='abandoned' WHERE turn_id = ANY(%s)", (stale,))
        conn.commit()

if __name__ == "__main__":
    main()
