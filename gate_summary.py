"""One line for the gate log from a check report (stdin path as argv[1])."""
import json, sys
try:
    r = json.load(open(sys.argv[1]))
    a = r["soft"].get("anchors") or {}
    hard = {k: v for k, v in r["hard"].items() if isinstance(v, bool)}
    print(f"ok={r['ok']} hard={hard} anchors={a.get('verified')}/{a.get('failed')} "
          f"unreviewed={r.get('unreviewed_beliefs')} run={r.get('run_id')}")
except Exception as e:  # noqa: BLE001
    print(f"unparseable report: {e}")
