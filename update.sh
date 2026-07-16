#!/bin/bash
# Ben OTA updater — release tags only, health-checked, auto-rollback
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$BASE_DIR/update.log"
exec >> "$LOG" 2>&1
echo "=== $(date '+%F %T') update start ==="

cd "$BASE_DIR" || exit 1

# Never destroy customer data
for f in qa_pairs.json vision_data.json pose_store.json settings.json; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    echo "ABORT: $f is tracked — update would overwrite customer data"
    exit 1
  fi
done

CURRENT=$(git rev-parse HEAD)
CURRENT_V=$(cat VERSION 2>/dev/null || echo unknown)
echo "current: $CURRENT_V ($CURRENT)"

git fetch --tags --quiet || { echo "FAIL: cannot reach repo"; exit 1; }
LATEST=$(git tag -l 'v*' --sort=-v:refname | head -1)
[ -z "$LATEST" ] && { echo "no release tags"; exit 0; }
echo "latest tag: $LATEST"

if git describe --tags --exact-match HEAD 2>/dev/null | grep -qx "$LATEST"; then
  echo "already on $LATEST"
  exit 0
fi

echo "updating -> $LATEST"
git checkout "tags/$LATEST" --force --quiet || { echo "FAIL: checkout"; exit 1; }
"$BASE_DIR/venv/bin/pip" install -r requirements.txt -q 2>&1 | tail -3

systemctl restart piassistant

OK=0
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:5000/ping >/dev/null 2>&1 && { OK=1; break; }
  sleep 2
done

if [ "$OK" = "1" ]; then
  echo "SUCCESS: now $(cat VERSION 2>/dev/null) ($LATEST)"
  systemctl restart robot-display 2>/dev/null
  exit 0
fi

echo "HEALTH CHECK FAILED — rolling back to $CURRENT_V"
git checkout "$CURRENT" --force --quiet
"$BASE_DIR/venv/bin/pip" install -r requirements.txt -q 2>&1 | tail -3
systemctl restart piassistant
sleep 5
curl -sf http://127.0.0.1:5000/ping >/dev/null 2>&1 \
  && echo "rollback OK" || echo "CRITICAL: rollback failed too"
exit 1
