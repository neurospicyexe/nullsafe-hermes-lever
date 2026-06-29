# nullsafe-hermes-lever

**Switch which model a [Hermes](https://github.com/elsbrock/hermes) gateway uses — from chat, with no SSH.**

Hermes pins each profile's model in its `config.yaml` (`model.default`), and its OpenAI-compatible
server **ignores** the per-request `model` field. So a relay (a Discord bot, a web UI, anything) can't
switch models per message. This little watcher closes that gap:

```
  you, in chat:  "cy: model deepseek-reasoner"   (an owner-only command in your relay)
        │
        ▼  the relay writes  active_model = "deepseek-reasoner"  to a small state store (Halseth)
        │
        ▼  THIS watcher (running next to your gateways) notices the change, looks the key up in
           hermes-model-map.json, runs:  hermes [--profile X] config set model.default <id>
           and restarts just that one gateway (~10s)
        │
        ▼  optional Telegram ping confirms the switch
```

Model changes happen from chat (or anything that can write `active_model`), never SSH.
Pure Python standard library — no dependencies.

This is part of the [Nullsafe Suite](https://github.com/neurospicyexe/nullsafe-suite) but stands alone:
it works for **any** setup where one place stores a chosen model key and you want a Hermes gateway to follow it.

---

## What's here

| File | Purpose |
|------|---------|
| `hermes-model-watcher.py` | The watcher. Polls your state store, applies model switches, restarts gateways. |
| `hermes-model-map.json` | Maps friendly keys (`gpt-5.5`, `claude-opus`, `kimi-k2`, ...) → Hermes model id + provider. |
| `hermes-model-watcher.service` | Example `systemd --user` unit to keep the watcher running. |
| `.env.example` | The environment variables the watcher reads. |

---

## How it works

1. Your relay validates an **owner-only** command and writes the chosen key to a state store
   (here, a [Halseth](https://github.com/neurospicyexe/halseth) `companion_settings.active_model` row,
   read over HTTP). Any store works if you adapt `read_active_model()`.
2. The watcher polls every `POLL_SECONDS`, and on a change looks the key up in `hermes-model-map.json`.
3. It runs `hermes [--profile X] config set model.default <id>` (and `model.provider` / `base_url` when the
   key switches provider), strips any stale override, and restarts that profile's `--user` gateway unit.
4. State is remembered in a small JSON file so a restart never replays old switches.

The **map keys are yours to define** — keep them in sync with whatever your relay offers, and set each
`default` to the exact model id the provider currently ships (model ids change often; update them here).

---

## Setup

**Prerequisites:** one or more Hermes gateways running as `systemd --user` units, the `hermes` CLI installed,
and a state store your relay writes the chosen key to.

```bash
# 1. Put this folder where the watcher will run (next to your Hermes profiles)
git clone https://github.com/neurospicyexe/nullsafe-hermes-lever.git ~/nullsafe-hermes-lever
cd ~/nullsafe-hermes-lever

# 2. Configure
cp .env.example .env        # fill in HALSETH_BASE + (optional) Telegram
#    Edit hermes-model-watcher.py -> PROFILES to match YOUR companion ids,
#    config.yaml paths, and gateway unit names.
#    Edit hermes-model-map.json   -> the model keys you want to offer.

# 3. Run it (foreground, to test)
env $(grep -v '^#' .env | xargs) python3 hermes-model-watcher.py

# 4. Install as a service (keeps running, restarts on boot)
cp hermes-model-watcher.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-model-watcher
```

## Configuration

All paths default to `~/.hermes`; override anything via environment (see `.env.example`):

| Var | Default | Purpose |
|-----|---------|---------|
| `HALSETH_BASE` | `https://your-halseth.workers.dev` | Where the watcher reads each companion's `active_model`. |
| `HERMES_HOME` | `~/.hermes` | Your Hermes config dir (the default profile lives here). |
| `HERMES_BIN` | `<HERMES_HOME>/hermes-agent/venv/bin/hermes` | Path to the `hermes` CLI. |
| `HERMES_MODEL_MAP` | next to the script | Path to `hermes-model-map.json`. |
| `NOTIFY_ENV` | `<HERMES_HOME>/.env` | Holds `TELEGRAM_BOT_TOKEN` + `TELEGRAM_HOME_CHANNEL` for confirmation pings (optional). |
| `POLL_SECONDS` | `15` | How often to poll the state store. |

Each profile's `.env` should carry a `MCP_HALSETH_API_KEY` the watcher uses to read state.

> **Note:** when the state store is behind Cloudflare, the watcher sends a normal `User-Agent`
> (a default `Python-urllib` agent gets a `1010` block). Adjust `read_active_model()` if your store differs.

## License

[MIT](./LICENSE). No warranty — vibe-coded, unaudited; use at your own risk.
