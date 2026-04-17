"""
Secrets store — API credentials for PAX8 and Moneo.
Backend: Azure Table "secrets" if available, else api/secrets.json.
Env vars remain as a last-resort read fallback (never written back).
"""

import json
import os
import logging
from typing import Optional

import storage

logger = logging.getLogger(__name__)

SECRETS_FILE = os.path.join(os.path.dirname(__file__), "secrets.json")
TABLE_NAME = "secrets"
PARTITION = "default"

KEYS = (
    "pax8_client_id",
    "pax8_client_secret",
    "moneo_api_key",
    "moneo_company_id",
)

SECRET_KEYS = ("pax8_client_secret", "moneo_api_key")


# ---------- backend-aware read/write --------------------------------------

def _load_file() -> dict:
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"secrets.json parse error: {e}")
        return {}


def _save_file(data: dict) -> None:
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_all() -> dict:
    """Return {key: value} for every stored secret, from whichever backend is active."""
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        result = {}
        try:
            for e in table.query_entities(f"PartitionKey eq '{PARTITION}'"):
                rk = e.get("RowKey")
                val = e.get("value")
                if rk and val is not None:
                    result[rk] = val
        except Exception as ex:
            logger.error(f"Failed to read secrets table: {ex}")
        return result
    return _load_file()


def _upsert_one(key: str, value: str) -> None:
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        table.upsert_entity({"PartitionKey": PARTITION, "RowKey": key, "value": value})
        return
    data = _load_file()
    data[key] = value
    _save_file(data)


def _delete_one(key: str) -> None:
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        try:
            table.delete_entity(partition_key=PARTITION, row_key=key)
        except Exception:
            pass
        return
    data = _load_file()
    data.pop(key, None)
    _save_file(data)


# ---------- public API ----------------------------------------------------

def get_secret(key: str) -> Optional[str]:
    """Return value from active backend, falling back to the UPPERCASE env var."""
    value = _load_all().get(key)
    if value:
        return value
    return os.environ.get(key.upper())


def get_masked_all() -> dict:
    """
    Return a dict showing which keys are configured, with masked previews.
    For secret-type keys: '••••' + last 4 chars.
    For non-secret keys (e.g. moneo_company_id): full value.
    """
    stored = _load_all()
    backend = "azure" if storage.using_azure() else "file"
    result = {}
    for key in KEYS:
        value = stored.get(key) or os.environ.get(key.upper()) or ""
        if stored.get(key):
            source = backend
        elif os.environ.get(key.upper()):
            source = "env"
        else:
            source = None
        if not value:
            result[key] = {"configured": False, "preview": "", "source": None}
            continue
        if key in SECRET_KEYS:
            preview = "••••" + value[-4:] if len(value) > 4 else "••••"
        else:
            preview = value
        result[key] = {"configured": True, "preview": preview, "source": source}
    return result


def update_secrets(patch: dict) -> None:
    """
    Apply partial update:
      - Empty string / missing key → leave existing value untouched.
      - None → remove the key.
      - Non-empty string → replace.
    """
    for key in KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if value is None:
            _delete_one(key)
        elif isinstance(value, str) and value.strip() == "":
            continue
        else:
            _upsert_one(key, value.strip() if isinstance(value, str) else value)


def invalidate_caches() -> None:
    """Clear in-memory caches in API clients so new credentials take effect."""
    try:
        from pax8_client import _token_cache
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0
    except Exception:
        pass
