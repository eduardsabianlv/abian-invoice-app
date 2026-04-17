"""
Manual invoice links — user-picked Moneo invoice + optional amount override
for a PAX8 company for a given month (when one invoice spans multiple months).

Backend: Azure Table "manualLinks" if available, else api/manual_links.json.
Schema:
  PartitionKey = "YYYY-MM"
  RowKey       = pax8 company id
  Fields: invoice_nr, invoice_date, amount, original_total, payment_status
"""

import json
import os
import logging
from typing import Optional

import storage

logger = logging.getLogger(__name__)

STORE_FILE = os.path.join(os.path.dirname(__file__), "manual_links.json")
TABLE_NAME = "manualLinks"


# ---------- file helpers --------------------------------------------------

def _load_file() -> dict:
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"manual_links.json parse error: {e}")
        return {}


def _save_file(data: dict) -> None:
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _period_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


# ---------- public API ----------------------------------------------------

def get_for_period(year: int, month: int) -> dict:
    """Return { pax8_id: link_dict } for a given month."""
    pk = _period_key(year, month)
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        result = {}
        try:
            for e in table.query_entities(f"PartitionKey eq '{pk}'"):
                rk = e.get("RowKey")
                if not rk:
                    continue
                result[rk] = {
                    "invoice_nr": e.get("invoice_nr", ""),
                    "invoice_date": e.get("invoice_date", ""),
                    "amount": float(e.get("amount") or 0),
                    "original_total": float(e.get("original_total") or 0),
                    "payment_status": e.get("payment_status", "unpaid"),
                }
        except Exception as ex:
            logger.error(f"Failed to query manualLinks for {pk}: {ex}")
        return result
    return _load_file().get(pk, {})


def set_link(year: int, month: int, pax8_id: str, link: dict) -> None:
    pk = _period_key(year, month)
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        entity = {
            "PartitionKey": pk,
            "RowKey": pax8_id,
            "invoice_nr": str(link.get("invoice_nr", "")),
            "invoice_date": str(link.get("invoice_date", "")),
            "amount": float(link.get("amount") or 0),
            "original_total": float(link.get("original_total") or link.get("amount") or 0),
            "payment_status": str(link.get("payment_status", "unpaid")),
        }
        table.upsert_entity(entity)
        return
    data = _load_file()
    data.setdefault(pk, {})[pax8_id] = link
    _save_file(data)


def delete_link(year: int, month: int, pax8_id: str) -> bool:
    pk = _period_key(year, month)
    table = storage.get_table(TABLE_NAME)
    if table is not None:
        try:
            table.delete_entity(partition_key=pk, row_key=pax8_id)
            return True
        except Exception:
            return False
    data = _load_file()
    if pk in data and pax8_id in data[pk]:
        del data[pk][pax8_id]
        if not data[pk]:
            del data[pk]
        _save_file(data)
        return True
    return False
