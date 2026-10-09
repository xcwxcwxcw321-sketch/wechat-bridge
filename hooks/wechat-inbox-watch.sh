#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# Read-only check: list inbox messages whose message_id is not in
# state/processed.json. This script never writes bridge state, so dry
# runs cannot consume detections; the worker marks messages processed.

OUTPUT=$(python3 - <<'PY' || echo silent
import json, os
base = os.path.expanduser("~/workspace/wechat-bridge")
inbox = os.path.join(base, "inbox")
proc_path = os.path.join(base, "state", "processed.json")
processed = set()
try:
    with open(proc_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        processed = {str(x) for x in data}
    elif isinstance(data, dict):
        processed = {str(x) for x in data.get("processed_ids", [])}
except Exception:
    pass
fresh = []
try:
    names = sorted(os.listdir(inbox))
except FileNotFoundError:
    names = []
for name in names:
    if not name.endswith(".json"):
        continue
    path = os.path.join(inbox, name)
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        continue
    mid = str(rec.get("message_id") or name)
    if mid not in processed:
        fresh.append({"file": path, "message_id": mid})
if fresh:
    print("wake")
    print(json.dumps({"unprocessed": fresh, "count": len(fresh)}, ensure_ascii=False))
else:
    print("silent")
PY
)

DECISION=$(printf '%s\n' "$OUTPUT" | head -n 1)
if [ "$DECISION" = "wake" ]; then
  PAYLOAD=$(printf '%s\n' "$OUTPUT" | tail -n 1)
  log "unprocessed wechat inbox messages found" "$PAYLOAD"
  wake "wechat inbox has unprocessed user messages" "$PAYLOAD"
else
  silent "no unprocessed wechat inbox messages" '{}'
fi
