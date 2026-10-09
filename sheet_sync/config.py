"""Environment-driven configuration. Missing credentials fail loudly."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SHEET_ID = "1kE1qrD85nju4qVYHv0erDRDko0seQL6BRCh-rH5SRfU"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class EspnConfig:
    league_id: int
    season_year: int
    espn_s2: str
    swid: str


def _load_dotenv():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _require(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    if value.startswith("PLACEHOLDER"):
        raise ConfigError(f"Environment variable {name} still holds a placeholder value")
    return value


def _require_int(name):
    value = _require(name)
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer")


def espn_config():
    _load_dotenv()
    return EspnConfig(
        league_id=_require_int("ESPN_LEAGUE_ID"),
        season_year=_require_int("ESPN_SEASON_YEAR"),
        espn_s2=_require("ESPN_S2"),
        swid=_require("SWID"),
    )


def sheet_id():
    _load_dotenv()
    return os.environ.get("GOOGLE_SHEET_ID", "").strip() or DEFAULT_SHEET_ID


def service_account_info():
    """GOOGLE_SERVICE_ACCOUNT_JSON may hold the key JSON itself (CI) or a path to the key file (local dev)."""
    _load_dotenv()
    raw = _require("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw.lstrip().startswith("{"):
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is neither key JSON nor a path to an existing file")
        raw = path.read_text()
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON (line {e.lineno}, column {e.colno})")
    if info.get("type") != "service_account":
        raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is not a service-account key")
    return info


SECRET_ENV_VARS = ("ESPN_S2", "SWID", "GOOGLE_SERVICE_ACCOUNT_JSON")


def _secret_values():
    values = []
    for name in SECRET_ENV_VARS:
        v = os.environ.get(name, "").strip()
        if v and not v.startswith("PLACEHOLDER"):
            values.append(v)
            if name == "SWID":
                values.append(v.strip("{}"))
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            text = raw if raw.startswith("{") else Path(raw).expanduser().read_text()
            info = json.loads(text)
            values += [info.get("private_key", ""), info.get("private_key_id", "")]
            values += info.get("private_key", "").splitlines()
        except (OSError, ValueError):
            pass
    return sorted({v for v in values if len(v) >= 8}, key=len, reverse=True)


def redact(text):
    """Replace any credential value that appears in text. Safety net for error output."""
    for v in _secret_values():
        text = text.replace(v, "[REDACTED]")
    return text
