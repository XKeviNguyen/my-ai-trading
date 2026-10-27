# My AI Trading — desktop client

A native PySide6 (Qt) client for the local, read-only My AI Trading engine. It talks only to
`http://127.0.0.1:8765` and holds no analysis logic of its own. See [`../docs/DESKTOP.md`](../docs/DESKTOP.md)
for features, security notes and limitations.

## Install (Ubuntu, Python 3.11+)

```bash
cd desktop
python3 -m venv .venv-desktop        # add --without-pip and bootstrap pip if ensurepip is missing
.venv-desktop/bin/pip install -e .   # pulls PySide6-Essentials (about 240 MB installed for 6.12)
```

## Run

```bash
my-ai-trading serve                  # in another terminal: start the local engine
.venv-desktop/bin/my-ai-trading-desktop
# or: .venv-desktop/bin/python -m my_ai_trading_desktop
```

The app reads the engine token from `~/.config/my-ai-trading/api_token` (or `$XDG_CONFIG_HOME`).
You can also paste a token in **Settings**; a pasted token is kept in memory for the session only.

## Test

The integration tests start the real engine API in-process with the synthetic fake exchange, so the
core package must be installed into the same environment:

```bash
.venv-desktop/bin/pip install -e '..[api,test]' -e '.[test]'
QT_QPA_PLATFORM=offscreen .venv-desktop/bin/python -m pytest -q
```

The tests import `tests/fakes.py` from the repository root. Set `MY_AI_TRADING_REPO_ROOT` to point at a
different checkout if needed.
