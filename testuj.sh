#!/bin/sh
set -eu

cd "$(dirname "$0")"
export INPUT_DIR=./dane

exec docker compose run --build --rm -T \
    --volume "$PWD/testuj.py:/app/testuj.py:ro" \
    --volume "$PWD/szablon-aplikacja/input:/przyklady:ro" \
    klasyfikacja python -u /app/testuj.py
