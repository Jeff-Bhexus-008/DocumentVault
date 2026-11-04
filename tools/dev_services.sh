#!/usr/bin/env bash
# Start/restart the local mock Supabase and the Doxari app for testing.
set -u
cd "$(dirname "$0")/.."
case "${1:-start}" in
  stop)
    for f in /tmp/dox_mock.pid /tmp/dox_app.pid; do
      [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null && rm -f "$f"
    done
    ;;
  start)
    for f in /tmp/dox_mock.pid /tmp/dox_app.pid; do
      [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; rm -f "$f"
    done
    sleep 1
    MOCK_PROFILE=upgraded PORT=54321 setsid nohup python3 tools/mock_supabase.py > /tmp/mock.log 2>&1 &
    echo $! > /tmp/dox_mock.pid
    sleep 2
    SUPABASE_URL=http://127.0.0.1:54321 SUPABASE_KEY=service-key \
      FLASK_SECRET_KEY=test-secret-key-for-local-testing-only PORT=5055 \
      setsid nohup python3 app.py > /tmp/app.log 2>&1 &
    echo $! > /tmp/dox_app.pid
    sleep 4
    curl -s -o /dev/null -w "app:%{http_code} " http://127.0.0.1:5055/login
    curl -s -o /dev/null -w "mock:%{http_code}\n" http://127.0.0.1:54321/rest/v1/
    ;;
esac
