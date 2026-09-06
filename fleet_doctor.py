#!/usr/bin/env python3
"""fleet_doctor — one command that says what is actually wrong with the fleet.

Written after the 2026-09 incident, where three problems were tangled together
and none of them was visible from Discord:

  * yfinance stopped answering, so VIX (the kill-switch input) went dark;
  * a host OS update had been stuck for months, and unpinning meant the
    container rebuilt onto a different dependency world than it was tested on;
  * commander reported "CRASHED" for every bot every cycle, which buried the
    one process that was genuinely failing.

Diagnosing that needed shell access to the box. This script replaces the shell
access: run it IN the container, paste the output.

    docker exec -w /app/code trading-fleet python3 fleet_doctor.py

It is read-only apart from one InfluxDB test point in its own measurement, and
it never places an order. Exit code 0 = everything passed, 1 = something FAILed.

Deliberately stdlib + requests only: it has to run when the fleet does not.
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))

# Every process PM2 runs, from deploy/ecosystem.config.js.
PROCESS_MODULES = [
    "market_analyst", "accountant", "commander", "error_watchdog",
    "wheel_bot", "trend_bot", "survivor_bot",
    "crypto_grid", "crypto_breakout",
]
IMPORT_EXEMPT = []

REQUIRED_CONFIG = ["API_KEY", "SECRET_KEY", "PAPER", "INFLUX_HOST",
                   "INFLUX_PORT", "INFLUX_DB_NAME", "WEBHOOK_OVERSEER"]

_results = []


def _emit(status, section, msg, detail=""):
    icon = {"PASS": "  ok  ", "FAIL": " FAIL ", "WARN": " warn "}[status]
    print(f"[{icon}] {msg}")
    if detail:
        for line in str(detail).rstrip().splitlines():
            print(f"          {line}")
    _results.append((status, section, msg))


def ok(section, msg, detail=""):   _emit("PASS", section, msg, detail)
def bad(section, msg, detail=""):  _emit("FAIL", section, msg, detail)
def warn(section, msg, detail=""): _emit("WARN", section, msg, detail)


def header(title):
    print(f"\n=== {title} ===")


def _run(cmd, timeout=25):
    """(returncode, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout, cwd=REPO)
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as e:
        return 1, str(e)


def _syntax_error(path):
    """Compile-check a file without writing anything. Returns an error string,
    or None if it parses. Read-only on purpose: py_compile drops .pyc files."""
    try:
        with open(path, "r", errors="replace") as f:
            src = f.read()
        compile(src, path, "exec")
        return None
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


# --- 1. WHERE AM I -------------------------------------------------------
def check_location():
    header("1. LOCATION — is this the code the fleet actually runs?")
    print(f"          repo dir : {REPO}")
    print(f"          cwd      : {os.getcwd()}")
    print(f"          python   : {sys.version.split()[0]} ({sys.executable})")
    print(f"          host     : {os.uname().nodename}")

    in_container = os.path.exists("/.dockerenv") or REPO.startswith("/app/code")
    if in_container:
        ok("location", f"Running inside the container, on the mounted code ({REPO}).")
    else:
        warn("location",
             "This does NOT look like the fleet container.",
             "The bots run inside `trading-fleet` against /app/code. Editing a\n"
             "copy elsewhere changes nothing they execute. Run:\n"
             "  docker exec -w /app/code trading-fleet python3 fleet_doctor.py")

    if REPO != os.getcwd():
        warn("location",
             f"cwd ({os.getcwd()}) is not the repo dir.",
             "The bots resolve bot_config.json / active_targets.json RELATIVE to\n"
             "cwd, so run this from the repo dir (-w /app/code) or paths will lie.")


# --- 2. CODE INTEGRITY ---------------------------------------------------
def check_code():
    header("2. CODE — did the patch land, and does it parse?")

    # 2a. Every module compiles. A patch pasted into the wrong file, or pasted
    # twice, shows up here as a SyntaxError -- and a SyntaxError in a shared
    # module (utils.py, fleet_bot.py) takes down EVERY bot at once.
    broken = []
    for fn in sorted(f for f in os.listdir(REPO) if f.endswith(".py")):
        err = _syntax_error(os.path.join(REPO, fn))
        if err:
            broken.append((fn, err))
    if broken:
        for fn, err in broken:
            bad("code", f"{fn} does not parse — nothing importing it can start.", err)
    else:
        ok("code", "All .py files parse.")

    # 2b. Uncommitted / unpushed drift. A hand-edit that works is still a
    # hand-edit: the next `git pull` silently reverts it.
    rc, out = _run(["git", "rev-parse", "--short", "HEAD"])
    if rc == 0:
        head = out.strip()
        rc2, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        print(f"          git HEAD : {head} on {branch.strip()}")
        rc3, dirty = _run(["git", "status", "--porcelain"])
        tracked = [ln for ln in dirty.splitlines() if ln and not ln.startswith("??")]
        if tracked:
            warn("code",
                 f"{len(tracked)} tracked file(s) edited but not committed.",
                 "\n".join(tracked[:12]) +
                 "\n-> These are LIVE (the repo is volume-mounted) but the next\n"
                 "   `git pull` will overwrite or conflict with them. Commit or revert.")
        else:
            ok("code", "Working tree clean — running committed code.")
    else:
        warn("code", "Not a git checkout (or git unavailable) — cannot verify version.", out)

    # 2c. Has the VIX source chain actually landed in market_analyst?
    ma_path = os.path.join(REPO, "market_analyst.py")
    try:
        with open(ma_path, "r", errors="replace") as f:
            src = f.read()
    except Exception as e:
        bad("code", f"Cannot read market_analyst.py: {e}")
        return
    digest = hashlib.sha256(src.encode()).hexdigest()[:12]
    mtime = datetime.datetime.fromtimestamp(os.path.getmtime(ma_path))
    print(f"          market_analyst.py sha256:{digest} modified {mtime:%Y-%m-%d %H:%M}")

    if "VIX_SOURCES" in src:
        names = []
        for line in src.splitlines():
            line = line.strip()
            if line.startswith('("') and line.endswith(","):
                names.append(line.split('"')[1])
        ok("code", "VIX source chain is present in market_analyst.py.",
           f"sources: {', '.join(names) if names else 'see VIX_SOURCES'}")
    elif "yf.download(\"^VIX\"" in src or "yf.download('^VIX'" in src:
        bad("code",
            "market_analyst.py still fetches VIX from yfinance ONLY.",
            "The multi-source patch has not landed in the file the fleet runs.\n"
            "Fix on the HOST, not in the container:\n"
            "  cd ~/bots/repo && git pull && docker exec trading-fleet pm2 restart market_analyst")
    else:
        warn("code", "Could not identify the VIX fetch in market_analyst.py — check it by hand.")


# --- 3. CONFIG -----------------------------------------------------------
def check_config():
    header("3. CONFIG — config.py, the one file that is not in git")
    if not os.path.exists(os.path.join(REPO, "config.py")):
        bad("config", "config.py is MISSING — every process dies at import.",
            "Copy config.example.py to config.py in the repo dir and fill it in.")
        return None
    try:
        sys.path.insert(0, REPO)
        import config
    except Exception as e:
        bad("config", f"config.py does not import: {type(e).__name__}: {e}",
            "Every process imports it first, so ALL of them will crash-loop.")
        return None
    ok("config", "config.py imports.")

    missing = [k for k in REQUIRED_CONFIG if not hasattr(config, k)]
    if missing:
        bad("config", f"config.py is missing: {', '.join(missing)}")
    else:
        ok("config", "All required keys present.")

    # The classic: localhost inside a container is the container itself.
    host = getattr(config, "INFLUX_HOST", None)
    if host in ("localhost", "127.0.0.1"):
        bad("config", f'INFLUX_HOST is "{host}".',
            'Inside the container that is the container itself, so every metric\n'
            'write silently goes nowhere. It must be "influxdb" (Docker DNS).')
    elif host:
        ok("config", f'INFLUX_HOST = "{host}".')

    db = getattr(config, "INFLUX_DB_NAME", None)
    if db != "trading_bots":
        warn("config", f'INFLUX_DB_NAME is "{db}", expected "trading_bots".',
             "It must match compose INFLUXDB_DB; a mismatch drops writes silently.")

    if getattr(config, "PAPER", None) is not True:
        warn("config", f"PAPER = {getattr(config, 'PAPER', None)} — this is LIVE trading.")

    unset = [k for k in dir(config)
             if k.startswith("WEBHOOK_")
             and isinstance(getattr(config, k), str)
             and ("YOUR" in getattr(config, k) or not getattr(config, k))]
    if unset:
        warn("config", f"Unconfigured webhooks (alerts silently skipped): {', '.join(sorted(unset))}")
    return config


# --- 4. IMPORTS ----------------------------------------------------------
def check_imports():
    header("4. IMPORTS — would each PM2 process survive startup?")
    print("          (an import-time crash is the ONE failure the bots' own")
    print("           try/except main loops cannot catch — it is a PM2 crash loop)")
    any_bad = False
    for mod in PROCESS_MODULES:
        rc, out = _run([sys.executable, "-c", f"import {mod}"], timeout=90)
        if rc == 0:
            ok("imports", f"{mod} imports cleanly.")
        else:
            any_bad = True
            tail = "\n".join(out.strip().splitlines()[-6:])
            bad("imports", f"{mod} FAILS AT IMPORT — PM2 will crash-loop it.", tail)
    for mod in IMPORT_EXEMPT:
        err = _syntax_error(os.path.join(REPO, f"{mod}.py"))
        if err:
            any_bad = True
            bad("imports", f"{mod} does not parse.", err)
        else:
            ok("imports", f"{mod} parses (not imported: it connects to Discord at module scope).")
    if not any_bad:
        ok("imports", "Every process module starts. Crash-looping bots are NOT an import problem.")


# --- 5. ALPACA -----------------------------------------------------------
def check_alpaca(config):
    header("5. ALPACA — the broker connection everything depends on")
    if config is None:
        warn("alpaca", "Skipped: no usable config.")
        return
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except Exception as e:
        bad("alpaca", f"alpaca-py will not import: {e}")
        return

    try:
        tc = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
        acct = tc.get_account()
        ok("alpaca", f"Account reachable: equity ${float(acct.portfolio_value):,.2f}, "
                     f"status {acct.status}.")
    except Exception as e:
        bad("alpaca", f"Account fetch failed: {type(e).__name__}: {e}",
            "Bad keys, or PAPER does not match the key type.")
        return

    try:
        clock = tc.get_clock()
        ok("alpaca", f"Clock: market {'OPEN' if clock.is_open else 'closed'} "
                     f"(next open {clock.next_open}).")
    except Exception as e:
        warn("alpaca", f"Clock fetch failed: {e}")

    # SPY bars drive the regime — the half of market_analyst that is NOT yfinance.
    try:
        dc = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
        req = StockBarsRequest(
            symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
            start=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400))
        df = dc.get_stock_bars(req).df
        n = 0 if df is None else len(df)
        if n >= 200:
            ok("alpaca", f"SPY daily bars: {n} rows (need >=200 for SMA200).")
        else:
            bad("alpaca", f"SPY daily bars: only {n} rows — regime cannot compute.")
    except Exception as e:
        bad("alpaca", f"SPY bars failed: {type(e).__name__}: {e}",
            "The regime half of market_analyst is down, not just VIX.")


# --- 6. VIX SOURCES ------------------------------------------------------
def check_vix():
    header("6. VIX SOURCES — each provider tried independently")
    print("          (the fleet needs ONE of these; the chain takes the first")
    print("           sane reading, so a red line here is not an outage by itself)")
    try:
        sys.path.insert(0, REPO)
        import market_analyst as ma
    except Exception as e:
        bad("vix", f"Cannot import market_analyst: {e}")
        return

    sources = getattr(ma, "VIX_SOURCES", None)
    if not sources:
        warn("vix", "No VIX_SOURCES in market_analyst — running the old single-source code.")
        return

    live = 0
    for name, fetch in sources:
        t0 = time.time()
        try:
            val = fetch()
        except Exception as e:
            bad("vix", f"{name}: {type(e).__name__}: {str(e)[:160]}")
            continue
        dt = time.time() - t0
        if val is None:
            bad("vix", f"{name}: returned no data ({dt:.1f}s).")
        elif not (ma.VIX_MIN <= val <= ma.VIX_MAX):
            bad("vix", f"{name}: {val} is outside the sane band "
                       f"[{ma.VIX_MIN}, {ma.VIX_MAX}] — rejected.")
        else:
            live += 1
            ok("vix", f"{name}: VIX = {val:.2f} ({dt:.1f}s).")

    if live == 0:
        bad("vix", "NO VIX source is reachable from this container.",
            "The fleet will run on the stale fail-safe: CRITICAL_VOLATILITY +\n"
            "VIX 25 + data_stale=true after 45 min. That is SAFE (entries gated)\n"
            "but it is not a market reading. Check egress from the container.")
    elif live < len(sources):
        ok("vix", f"{live}/{len(sources)} sources live — the chain has a spare.")
    else:
        ok("vix", "Every VIX source is live.")


# --- 7. INFLUXDB ---------------------------------------------------------
def check_influx(config):
    header("7. INFLUXDB — where every metric and error lands")
    if config is None:
        warn("influx", "Skipped: no usable config.")
        return
    try:
        import requests
    except Exception as e:
        bad("influx", f"requests will not import: {e}")
        return

    base = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}"
    try:
        r = requests.get(f"{base}/ping", timeout=5)
        if r.status_code in (200, 204):
            ok("influx", f"{base} reachable.")
        else:
            bad("influx", f"{base}/ping returned HTTP {r.status_code}.")
            return
    except Exception as e:
        bad("influx", f"{base} unreachable: {type(e).__name__}: {e}",
            'From inside the container the host must be "influxdb" (Docker DNS).')
        return

    try:
        url = f"{base}/write?db={config.INFLUX_DB_NAME}"
        r = requests.post(url, data=f"fleet_doctor,check=write value=1 {time.time_ns()}",
                          timeout=5)
        if r.status_code == 204:
            ok("influx", f'Test point written to db "{config.INFLUX_DB_NAME}".')
        else:
            bad("influx", f"Write rejected: HTTP {r.status_code} {r.text[:120]}",
                "Usually the database does not exist under that name (check\n"
                "compose INFLUXDB_DB against config.INFLUX_DB_NAME).")
    except Exception as e:
        bad("influx", f"Write failed: {e}")


# --- 8. RUNTIME STATE ----------------------------------------------------
def _age(path):
    return None if not os.path.exists(path) else time.time() - os.path.getmtime(path)


def check_state():
    header("8. RUNTIME STATE — config, targets, regime freshness, processes")

    cfg_path = os.path.join(REPO, "bot_config.json")
    if not os.path.exists(cfg_path):
        bad("state", "bot_config.json missing — budget checks FAIL CLOSED (no entries).")
    else:
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except Exception as e:
            bad("state", f"bot_config.json is not valid JSON: {e}",
                "Every bot falls back to defaults and the analyst cannot publish.")
            cfg = None
        if cfg:
            gs = cfg.get("global_settings", {})
            ok("state", f"bot_config.json parses ({len(cfg.get('bots', {}))} bots).")
            print(f"          regime   : {gs.get('market_condition', '?')}")
            print(f"          vix      : {gs.get('vix', '?')} "
                  f"(source: {gs.get('vix_source', 'not recorded')})")
            print(f"          updated  : {gs.get('regime_updated', 'never')}")
            if gs.get("data_stale"):
                warn("state", "data_stale=true — the fleet is on the VIX fail-safe, "
                              "not a live reading.")
            if gs.get("emergency_stop"):
                warn("state", "emergency_stop=true — /panic is latched. Use /resume.")
            if gs.get("CAPITAL_CRUNCH"):
                warn("state", "CAPITAL_CRUNCH=true — entries are gated by the CFO.")
            paused = [b for b, d in cfg.get("bots", {}).items()
                      if d.get("status") != "active"]
            if paused:
                print(f"          paused   : {', '.join(paused)}")

            age = _age(cfg_path)
            if age is not None and age > 3600:
                bad("state", f"bot_config.json last written {age/3600:.1f}h ago.",
                    "market_analyst writes it every 15 min when anything moves.\n"
                    "This old means the analyst is dead, wedged, or cannot fetch.")

    tgt = os.path.join(REPO, "active_targets.json")
    age = _age(tgt)
    if age is None:
        bad("state", "active_targets.json MISSING — bots are on fallback watchlists.",
            "It is SCP'd from the Corsair scout 3x daily.")
    elif age > 86400:
        bad("state", f"active_targets.json is {age/3600:.1f}h old (stale > 24h).",
            "The scout run or the SCP is failing on the Corsair side.")
    else:
        ok("state", f"active_targets.json is {age/3600:.1f}h old.")

    rc, out = _run(["pm2", "jlist"], timeout=20)
    if rc != 0:
        warn("state", "pm2 jlist unavailable — cannot read process state.", out[:300])
        return
    try:
        procs = json.loads(out)
    except Exception:
        warn("state", "pm2 jlist did not return JSON.", out[:300])
        return

    print(f"          {'process':<18}{'status':<10}{'restarts':<10}{'mem':<10}cpu")
    unhealthy = []
    for p in procs:
        env = p.get("pm2_env", {})
        monit = p.get("monit", {}) or {}
        status = env.get("status", "?")
        restarts = env.get("restart_time", 0)
        mem = f"{(monit.get('memory') or 0) / 1048576:.0f}M"
        print(f"          {p.get('name', '?'):<18}{status:<10}{restarts:<10}{mem:<10}"
              f"{monit.get('cpu', 0)}%")
        if status != "online":
            unhealthy.append(f"{p.get('name')} ({status})")
        elif restarts > 20:
            unhealthy.append(f"{p.get('name')} (online, {restarts} restarts)")

    if unhealthy:
        bad("state", f"Processes needing attention: {', '.join(unhealthy)}",
            "For a crash loop, the reason is in the PM2 error log:\n"
            "  docker exec trading-fleet pm2 logs <name> --err --lines 40 --nostream")
    else:
        ok("state", f"All {len(procs)} PM2 processes online with sane restart counts.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-network", action="store_true",
                    help="skip Alpaca/VIX/InfluxDB checks (offline code review)")
    args = ap.parse_args()

    print("=" * 68)
    print(" FLEET DOCTOR")
    print(f" {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 68)

    check_location()
    check_code()
    cfg = check_config()
    check_imports()
    if args.skip_network:
        print("\n(network checks skipped)")
    else:
        check_alpaca(cfg)
        check_vix()
        check_influx(cfg)
    check_state()

    fails = [r for r in _results if r[0] == "FAIL"]
    warns = [r for r in _results if r[0] == "WARN"]
    print("\n" + "=" * 68)
    print(f" {len(_results) - len(fails) - len(warns)} passed, "
          f"{len(fails)} FAILED, {len(warns)} warnings")
    if fails:
        print("\n Failures, in the order worth fixing:")
        for _, section, msg in fails:
            print(f"   [{section}] {msg}")
    print("=" * 68)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
