#!/usr/bin/env bash
# Daily substack cookie-health probe, run from host cron (see README
# "Operational notes"). Appends one timestamped verdict per run to
# data/cookie-health.log; the in-app admin banner remains the primary alert.
set -u
cd "$(dirname "$(readlink -f "$0")")/.."
{
    printf '%s ' "$(date -Is)"
    .venv/bin/python scripts/check_substack_access.py
} >> data/cookie-health.log 2>&1
