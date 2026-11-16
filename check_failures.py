"""Show what differs between each non-faithful answer and the original code (no tokens used)."""

import difflib                                          # Compare two versions line by line
import json                                             # Read the JSONL files
from pathlib import Path                                # File paths

ROOT = Path(__file__).resolve().parent
tasks = {}                                              # id -> task (with the original, correct code)
for line in (ROOT / "evals" / "tasks.jsonl").read_text(encoding="utf-8").splitlines():
    if line.strip():
        t = json.loads(line)
        tasks[t["id"]] = t

for cond in ("no_tools", "compiler_only", "all_tools"):
    path = ROOT / "evals" / f"results_{cond}.jsonl"
    if not path.exists():                               # This condition hasn't run yet
        continue
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["faithful"]:                               # Only show the failures
            continue
        print(f"===== {cond} | {r['id']} | compiles={r['compiles']} changed={r['changed']} deleted={r['deleted']} "
              f"finish={r.get('finish_reason', '?')}")
        original = tasks[r["id"]]["fixed"]
        if not r["final_code"].strip():
            print("(the model gave no code block)")
            print("Raw answer:", r.get("final_text", "")[:600] or "(empty)")   # What the model actually wrote
        elif original:
            diff = difflib.unified_diff(original.splitlines(), r["final_code"].splitlines(), lineterm="", n=0)
            changes = [x for x in diff if x[:1] in "+-" and not x.startswith(("+++", "---"))]
            print("\n".join(changes[:20]) or "(identical to the original)")   # - original, + model's answer
        if r["remaining_errors"]:
            print("Remaining errors:\n" + r["remaining_errors"][:400])
        print()
