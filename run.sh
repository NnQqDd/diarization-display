#!/usr/bin/env bash
# Start the dataset viewer/editor.
#
#   ./run.sh                     # http://127.0.0.1:8200
#   PORT=9200 ./run.sh           # somewhere else
#
# The dataset it opens comes from ANNOTATEPATH in .env. Copy .env.example and
# point it at your diarization-annotate checkout.
#
# It binds to localhost on purpose. This app shows the sidecar in full -- IP
# addresses and user agents included -- and can overwrite and delete other
# people's annotations. Do not put it behind a public vhost.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8200}"
HOST="${HOST:-127.0.0.1}"
WORKERS="${WORKERS:-1}"

PYTHON=".venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Creating .venv and installing requirements…"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

[[ -f .env ]] || echo "WARNING: no .env — falling back to ../diarization-annotate"
command -v ffprobe >/dev/null || \
  echo "WARNING: ffprobe not found — an uploaded pair will be measured from its annotation."

# Say what is actually being opened, before anything binds. Getting this wrong
# is the failure everybody hits first.
"$PYTHON" - <<'PY'
from app.config import settings
print(f"dataset     {settings.dataset_dir}{'' if settings.dataset_ok else '   (NOT FOUND)'}")
print(f"audios      {settings.audios_dir}{'' if settings.audios_ok else '   (not found)'}")
PY

args=(-m uvicorn app.main:app --host "$HOST" --port "$PORT")
((WORKERS > 1)) && args+=(--workers "$WORKERS")

echo "UI          http://${HOST}:${PORT}"
exec "$PYTHON" "${args[@]}" "$@"
