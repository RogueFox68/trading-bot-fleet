"""Tails logs/fleet_error_registry.jsonl and ships each error event to
InfluxDB as a bot_error_events row, so registry errors show up in Grafana
instead of sitting in a file nobody reads.

Runs as a PM2 process inside the fleet container (see
deploy/ecosystem.config.js). Uses the same config.py + raw line-protocol
write path as the rest of the fleet - no extra client libraries.
"""
import json
import os
import time
import datetime
import requests
import config

REGISTRY_FILE = "logs/fleet_error_registry.jsonl"
WRITE_URL = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
QUERY_URL = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/query"


def ensure_database():
    """Idempotently create the database (matches docker-compose INFLUXDB_DB)."""
    try:
        requests.post(QUERY_URL, params={"q": f'CREATE DATABASE "{config.INFLUX_DB_NAME}"'}, timeout=5)
    except Exception as e:
        print(f"[watchdog] could not ensure database (continuing): {e}")


def _tag(value):
    """Sanitize a tag value for line protocol (no spaces/commas/equals)."""
    return str(value or "unknown").replace(" ", "_").replace(",", "_").replace("=", "_")


def _field_str(value):
    """Escape a string field value for line protocol."""
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _timestamp_ns(iso_str):
    """Registry timestamps are naive local time; fall back to now on parse failure."""
    try:
        dt = datetime.datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return int(dt.timestamp() * 1e9)
    except Exception:
        return time.time_ns()


def push_error(error_data):
    line = (
        f"bot_error_events,"
        f"bot_id={_tag(error_data.get('bot_id'))},"
        f"component={_tag(error_data.get('component'))},"
        f"error_type={_tag(error_data.get('error_type'))} "
        f'value=1i,message="{_field_str(error_data.get("message"))}",'
        f'context="{_field_str(error_data.get("context"))}" '
        f"{_timestamp_ns(error_data.get('timestamp', ''))}"
    )
    r = requests.post(WRITE_URL, data=line.encode("utf-8"), timeout=5)
    if r.status_code != 204:
        print(f"[watchdog] influx write failed: {r.status_code} {r.text[:200]}")


def tail_and_push():
    print(f"[watchdog] tailing {REGISTRY_FILE} -> {WRITE_URL}")
    os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)
    if not os.path.exists(REGISTRY_FILE):
        open(REGISTRY_FILE, "a").close()

    f = open(REGISTRY_FILE, "r")
    f.seek(0, 2)  # only ship errors logged after startup
    inode = os.fstat(f.fileno()).st_ino

    while True:
        line = f.readline()
        if line:
            try:
                push_error(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue  # skip malformed lines
            except Exception as e:
                print(f"[watchdog] write failure (surviving): {e}")
                time.sleep(5)
            continue

        # No new data: watch for RotatingFileHandler swapping the file out
        # under us, otherwise we'd tail a rotated file forever.
        time.sleep(1)
        try:
            st = os.stat(REGISTRY_FILE)
            if st.st_ino != inode or st.st_size < f.tell():
                f.close()
                f = open(REGISTRY_FILE, "r")
                inode = os.fstat(f.fileno()).st_ino
                print("[watchdog] registry file rotated, reopened.")
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    ensure_database()
    tail_and_push()
