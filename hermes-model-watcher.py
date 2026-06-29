#!/usr/bin/env python3
"""
Hermes model-switch watcher -- a no-SSH model lever for your Hermes companions.

Hermes pins each companion's model in its profile config.yaml (`model.default`); its
OpenAI-compatible API server IGNORES the per-request `model` field, so the Discord relay
cannot switch model per-message. This watcher closes that gap:

  Discord:  `cy: model deepseek-reasoner`  (owner-only command, already shipped)
     -> the bot writes active_model="deepseek-reasoner" to Halseth companion_settings
     -> THIS watcher (on the VPS) notices the change, maps it via hermes-model-map.json,
        runs `hermes [--profile X] config set model.default <id>`, and restarts that one
        gateway (~5-10s). A Telegram ping confirms the switch.

So model changes happen from Discord (or anything that writes active_model -- Hearth, a
Telegram command), never SSH. Stdlib only. Reuses the per-companion Halseth secret in each
profile .env for reads and a configured Telegram bot token for the confirmation ping.

NOTE on the User-Agent header: Cloudflare's WAF returns `error code: 1010` to the default
`Python-urllib` agent, so every Halseth request MUST send a normal UA or it 403s.
"""
import json, os, time, subprocess, urllib.request, urllib.error, pathlib

# ── Configuration (all overridable by environment) ───────────────────────────
# HALSETH_BASE     your Halseth Worker URL (where each companion's active_model is read from)
# HERMES_HOME      Hermes config dir for the default profile (default: ~/.hermes)
# HERMES_BIN       path to the hermes CLI (default: <HERMES_HOME>/hermes-agent/venv/bin/hermes)
# HERMES_MODEL_MAP path to hermes-model-map.json (default: next to this script)
# NOTIFY_ENV       .env holding TELEGRAM_BOT_TOKEN + TELEGRAM_HOME_CHANNEL for confirmations
HALSETH_BASE = os.environ.get("HALSETH_BASE", "https://your-halseth.workers.dev").rstrip("/")
HERMES_HOME  = pathlib.Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
HERMES_BIN   = os.environ.get("HERMES_BIN", str(HERMES_HOME / "hermes-agent/venv/bin/hermes"))
MAP_FILE     = pathlib.Path(os.environ.get(
    "HERMES_MODEL_MAP", str(pathlib.Path(__file__).resolve().parent / "hermes-model-map.json")))
STATE        = HERMES_HOME / ".model-watcher-state.json"
NOTIFY_ENV   = pathlib.Path(os.environ.get("NOTIFY_ENV", str(HERMES_HOME / ".env")))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
UA           = "Mozilla/5.0 (HermesModelWatcher)"   # MUST be non-default or Cloudflare may 1010

# companion id -> profile facts. EDIT THIS for your own companions/gateways.
# This example wires three Hermes profiles, each running as its own systemd --user gateway:
#   - the default profile  -> config <HERMES_HOME>/config.yaml,        unit "hermes-gateway"
#   - an extra profile <p> -> config <HERMES_HOME>/profiles/<p>/...,   unit "hermes-gateway-<p>"
# The companion id (key) must match the active_model owner id you write to Halseth.
PROFILES = {
    "companion-a": {"display": "Companion A", "cfg": str(HERMES_HOME / "config.yaml"),
                    "env": str(HERMES_HOME / ".env"), "unit": "hermes-gateway", "args": []},
    "companion-b": {"display": "Companion B", "cfg": str(HERMES_HOME / "profiles/companion-b/config.yaml"),
                    "env": str(HERMES_HOME / "profiles/companion-b/.env"), "unit": "hermes-gateway-companion-b",
                    "args": ["--profile", "companion-b"]},
    "companion-c": {"display": "Companion C", "cfg": str(HERMES_HOME / "profiles/companion-c/config.yaml"),
                    "env": str(HERMES_HOME / "profiles/companion-c/.env"), "unit": "hermes-gateway-companion-c",
                    "args": ["--profile", "companion-c"]},
}


def env(path):
    e = {}
    try:
        for line in open(path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                e[k] = v
    except Exception:
        pass
    return e


def tg(method, params):
    e = env(NOTIFY_ENV)
    tok = e.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        return None
    try:
        import urllib.parse
        data = urllib.parse.urlencode(params).encode()
        r = urllib.request.urlopen("https://api.telegram.org/bot%s/%s" % (tok, method),
                                   data=data, timeout=20)
        return json.loads(r.read().decode())
    except Exception:
        return None


def ping(text):
    chat = env(NOTIFY_ENV).get("TELEGRAM_HOME_CHANNEL", "")
    if chat:
        tg("sendMessage", {"chat_id": chat, "text": text})


_SETTINGS_SECRET = None


def settings_secret():
    """Halseth's authGuard accepts any valid companion (or admin) token for the
    /companion/settings route -- it is NOT scoped per companion. So probe the profiles and
    cache the first secret that actually authenticates, then use it for every read/write
    (handy if some profile tokens are placeholders that 401)."""
    global _SETTINGS_SECRET
    if _SETTINGS_SECRET:
        return _SETTINGS_SECRET
    for c in PROFILES:
        s = env(PROFILES[c]["env"]).get("MCP_HALSETH_API_KEY", "")
        if not s:
            continue
        req = urllib.request.Request(
            "%s/companion/settings/%s" % (HALSETH_BASE, c),
            headers={"Authorization": "Bearer " + s, "User-Agent": UA},
        )
        try:
            urllib.request.urlopen(req, timeout=8).read()
            _SETTINGS_SECRET = s
            return s
        except Exception:
            continue
    return None


def read_active_model(companion):
    """GET active_model for a companion using the first working Halseth secret."""
    secret = settings_secret()
    if not secret:
        return None
    req = urllib.request.Request(
        "%s/companion/settings/%s" % (HALSETH_BASE, companion),
        headers={"Authorization": "Bearer " + secret, "User-Agent": UA},
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        v = data.get("active_model")
        return v.strip() if isinstance(v, str) and v.strip() else None
    except Exception:
        return None


def load_map():
    try:
        return json.loads(MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def current_config_default(companion):
    """First `default:` line in the profile config.yaml is model.default."""
    try:
        for line in open(PROFILES[companion]["cfg"]):
            s = line.strip()
            if s.startswith("default:"):
                return s.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}


def _remove_model_key(cfg_path, key):
    """Delete `  <key>: ...` from the top-level model: block of a config.yaml so the provider
    preset (built-in base_url) / env key is used instead of a stale override from the previous
    provider. No `hermes config unset` exists, so we edit the yaml directly. Returns True if a
    line was removed."""
    try:
        lines = open(cfg_path).read().splitlines(keepends=True)
    except Exception:
        return False
    out, in_model, removed = [], False, False
    for ln in lines:
        s = ln.rstrip("\n")
        if s.startswith("model:") and not s.startswith("model_"):
            in_model = True
            out.append(ln)
            continue
        if in_model:
            if s and not s.startswith(" "):       # left the model block (next top-level key)
                in_model = False
            elif s.strip().startswith(key + ":"):  # a key inside the model block
                removed = True
                continue
        out.append(ln)
    if removed:
        try:
            open(cfg_path, "w").write("".join(out))
        except Exception:
            return False
    return removed


def apply_model(companion, entry):
    """Write model.* into the profile config and restart that gateway. Idempotent:
    if model.default already matches, skip the restart. Clears base_url/api_key when the new
    entry doesn't specify them, so cross-provider switches don't carry a stale override.
    Returns (changed, error)."""
    prof = PROFILES[companion]
    cfg = prof["cfg"]
    target = entry.get("default")
    if not target:
        return False, "map entry missing 'default'"

    if current_config_default(companion) == target:
        return False, None  # already there -- no restart needed

    sets = [("model.default", target)]
    if entry.get("provider"):
        sets.append(("model.provider", entry["provider"]))
    # base_url / api_key: set when the entry has one, else strip any stale value so the
    # provider's built-in preset / env key is used (e.g. deepseek/anthropic vs custom local).
    for field in ("base_url", "api_key"):
        if entry.get(field):
            sets.append(("model." + field, entry[field]))

    for key, val in sets:
        try:
            p = subprocess.run([HERMES_BIN] + prof["args"] + ["config", "set", key, val],
                               env=clean_env(), capture_output=True, text=True, timeout=60)
            if p.returncode != 0:
                return False, "config set %s failed: %s" % (key, (p.stderr or p.stdout)[:200])
        except Exception as ex:
            return False, "config set %s raised: %s" % (key, ex)

    for field in ("base_url", "api_key"):
        if not entry.get(field):
            _remove_model_key(cfg, field)

    try:
        subprocess.run(["systemctl", "--user", "restart", prof["unit"]],
                       timeout=90, capture_output=True)
    except Exception as ex:
        return True, "config set but restart failed: %s" % ex
    return True, None


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return None


def save_state(st):
    try:
        STATE.write_text(json.dumps(st))
    except Exception:
        pass


def main():
    st = load_state()
    first_run = st is None
    if first_run:
        # Adopt whatever Halseth currently says as "seen" without applying it, so starting
        # the watcher never triggers a surprise restart storm. Real switches are CHANGES.
        st = {c: (read_active_model(c) or "") for c in PROFILES}
        save_state(st)
        print("[model-watcher] first run -- adopted current active_model as baseline: %s" % st,
              flush=True)

    warned = set()   # (companion, key) already warned as unknown -- don't spam
    while True:
        mapping = load_map()
        for companion in PROFILES:
            want = read_active_model(companion)
            if want is None:
                continue
            if want == st.get(companion):
                continue  # no change since last applied/seen
            entry = mapping.get(want)
            if not entry:
                wkey = (companion, want)
                if wkey not in warned:
                    warned.add(wkey)
                    ping("⚠ %s: unknown model key '%s' (not in hermes-model-map.json) -- "
                         "ignored. Config unchanged." % (PROFILES[companion]["display"], want))
                    print("[model-watcher] unknown key %r for %s -- ignored" % (want, companion),
                          flush=True)
                st[companion] = want      # record so we don't re-warn every cycle
                save_state(st)
                continue
            changed, err = apply_model(companion, entry)
            if err:
                ping("⚠ %s: model switch to %s FAILED -- %s"
                     % (PROFILES[companion]["display"], entry.get("label", want), err))
                print("[model-watcher] apply error %s/%s: %s" % (companion, want, err), flush=True)
                # leave state unchanged so the next cycle retries
                continue
            st[companion] = want
            warned.discard((companion, want))
            save_state(st)
            if changed:
                ping("\U0001F500 %s switched to %s (%s). ~10s to settle."
                     % (PROFILES[companion]["display"], entry.get("label", want),
                        entry.get("default")))
                print("[model-watcher] %s -> %s (%s)" % (companion, want, entry.get("default")),
                      flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
