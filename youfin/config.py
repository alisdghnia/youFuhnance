"""Settings loaded from the project's .env (never printed or logged)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PORTFOLIO_PATH = DATA_DIR / "portfolio.json"


def _find_dotenv() -> Path | None:
    # Walk up so git worktrees nested in the repo still find the main .env.
    for d in [ROOT, *ROOT.parents][:6]:
        if (d / ".env").is_file():
            return d / ".env"
    return None


def _load_dotenv() -> None:
    path = _find_dotenv()
    if not path:
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()
# After the .env load, so YOUFIN_DB set there is honoured like the other settings.
DB_PATH = Path(os.environ.get("YOUFIN_DB") or DATA_DIR / "youfin.db")


def you_api_key() -> str:
    key = os.environ.get("YOU_COM_API_KEY") or os.environ.get("YDC_API_KEY")
    if not key:
        raise RuntimeError("Set YOU_COM_API_KEY in .env")
    return key
