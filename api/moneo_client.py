"""
Moneo API Client
API key auth. Base URL: https://api.moneo.lv/{COMPANY_ID}/api/v2/{TABLE}
"""

import os
import logging
import requests
from typing import Any, Optional

from secrets_store import get_secret

logger = logging.getLogger(__name__)

MONEO_BASE = "https://api.moneo.lv"


class MoneoError(Exception):
    """Raised when the Moneo API returns an error."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class MoneoClient:
    def __init__(self):
        self.api_key = get_secret("moneo_api_key")
        self.company_id = get_secret("moneo_company_id")
        if not self.api_key or not self.company_id:
            raise MoneoError("Moneo API key and company ID must be configured in Iestatījumi")
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, table: str, suffix: str = "") -> str:
        """Build a Moneo endpoint URL."""
        return f"{MONEO_BASE}/{self.company_id}/api/v2/{table}{suffix}"

    def _headers(self) -> dict:
        return {
            "Authorization": self.api_key,
            "x-moneo-comp": self.company_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, table: str, suffix: str, payload: dict) -> dict:
        url = self._url(table, suffix)
        body = dict(payload)
        body.setdefault("request", {"compuid": self.company_id})
        resp = self.session.post(url, headers=self._headers(), json=body, timeout=30)
        if not resp.ok:
            raise MoneoError(
                f"Moneo POST {table}{suffix} failed: {resp.status_code} {resp.text}",
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def query(
        self,
        table: str,
        filter: dict = None,
        fields: list = None,
        limit: int = 100,
    ) -> list:
        """
        POST to table (Moneo query endpoint).
        Returns list of records.
        Moneo response shape: {"result": {"records": [...]}} — or a plain list / {"data": [...]}.
        """
        payload = {
            "filter": filter or {},
            "fields": fields or [],
            "limit": limit,
        }
        result = self._post(table, "/", payload)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            if isinstance(result.get("result"), dict):
                return result["result"].get("records") or result["result"].get("data") or []
            return result.get("data") or result.get("rows") or result.get("records") or []
        return []

    def create(self, table: str, fieldlist: list, data: list) -> dict:
        """
        POST to table/create/
        fieldlist: list of field names
        data: list of rows (each row is a list of values matching fieldlist)
        """
        payload = {
            "fieldlist": fieldlist,
            "data": data,
        }
        return self._post(table, "/create/", payload)

    def update(
        self,
        table: str,
        pk: Any,
        fieldlist: list,
        data: list,
        clear_trailing_rows: bool = None,
    ) -> dict:
        """
        POST to table/update/{pk}
        """
        payload: dict = {
            "fieldlist": fieldlist,
            "data": data,
        }
        if clear_trailing_rows is not None:
            payload["clearTrailingRows"] = clear_trailing_rows
        return self._post(table, f"/update/{pk}", payload)

    # ------------------------------------------------------------------
    # Business-level helpers
    # ------------------------------------------------------------------

    def get_invoices(
        self,
        customer_code: str = None,
        date_from: str = None,
        date_to: str = None,
        limit: int = 200,
    ) -> list:
        """
        Query sales.invoices with optional filters.
        date_from / date_to: 'YYYY-MM-DD'
        Returns list of invoice records.
        """
        filter_dict = {}
        if customer_code:
            filter_dict["custcode"] = customer_code
        if date_from:
            filter_dict["invdate__gte"] = date_from
        if date_to:
            filter_dict["invdate__lte"] = date_to
        return self.query("sales.invoices", filter=filter_dict, limit=limit)

    def get_invoice_items(self, invoice_nr: str) -> list:
        """Query sales.invoices_items_rows for a given invoice number."""
        return self.query(
            "sales.invoices_items_rows",
            filter={"invnr": invoice_nr},
            limit=500,
        )

    def get_customers(self, limit: int = 500) -> list:
        """
        Query contacts.contacts where customerflag=1.
        Returns all active customers with their raw Moneo fields.
        """
        return self.query(
            "contacts.contacts",
            filter={"customerflag": 1},
            fields=["code", "name", "email", "vatno"],
            limit=limit,
        )

    def get_customer(self, customer_code: str) -> Optional[dict]:
        """Fetch a single customer by code."""
        results = self.query(
            "contacts.contacts",
            filter={"code": customer_code, "customerflag": 1},
            limit=1,
        )
        return results[0] if results else None

    def create_invoice(
        self,
        customer_code: str,
        inv_date: str,
        lines: list,
        comment: str = "",
    ) -> dict:
        """
        Create a Moneo invoice with line items in a single request.

        lines: list of dicts with keys:
            itemcode, itemname, quant, price, rowsum

        Moneo create invoice format (nested multi-table create):
        {
          "data": {
            "sales.invoices": {
              "fieldlist": [...],
              "data": [[...]]
            },
            "sales.invoices_items_rows": {
              "fieldlist": [...],
              "data": [[...], ...]
            }
          },
          "request": {"compuid": COMPANY_ID}
        }

        IMPORTANT: Always use PAX8 SKU as itemcode (with -M or -Y suffix),
        NOT Moneo internal codes, as per company policy.
        """
        invoice_fieldlist = ["custcode", "invdate", "comment"]
        invoice_data = [[customer_code, inv_date, comment]]

        items_fieldlist = ["itemcode", "itemname", "quant", "price", "rowsum"]
        items_data = [
            [
                line["itemcode"],
                line["itemname"],
                line["quant"],
                round(float(line["price"]), 4),
                round(float(line["rowsum"]), 2),
            ]
            for line in lines
        ]

        payload = {
            "data": {
                "sales.invoices": {
                    "fieldlist": invoice_fieldlist,
                    "data": invoice_data,
                },
                "sales.invoices_items_rows": {
                    "fieldlist": items_fieldlist,
                    "data": items_data,
                },
            },
            "request": {"compuid": self.company_id},
        }

        url = self._url("sales.invoices", "/create/")
        resp = self.session.post(url, headers=self._headers(), json=payload, timeout=60)
        if not resp.ok:
            raise MoneoError(
                f"Moneo create invoice failed: {resp.status_code} {resp.text}",
                status_code=resp.status_code,
            )
        return resp.json()

    def get_invoice_payment_status(self, invoices: list) -> list:
        """
        Enrich invoice list with payment status and normalized fields.
        Moneo sales.invoices fields:
          sernr (invoice number), invdate, custcode, custname,
          totsum (incl. VAT), sum (excl. VAT), vatsum, totunpaidsum
        Adds:
          payment_status ('paid' | 'unpaid' | 'partial')
          invnr, total, paid  (normalized aliases for the UI)
        """
        enriched = []
        for inv in invoices:
            total = float(
                inv.get("totsum")
                or inv.get("total")
                or inv.get("invsum")
                or 0
            )
            unpaid = float(inv.get("totunpaidsum") or 0)
            paid = max(total - unpaid, 0)
            if total <= 0:
                status = "unpaid"
            elif unpaid <= 0.01:
                status = "paid"
            elif paid > 0:
                status = "partial"
            else:
                status = "unpaid"
            enriched.append({
                **inv,
                "invnr": inv.get("sernr") or inv.get("invnr") or "",
                "total": round(total, 2),
                "paid": round(paid, 2),
                "payment_status": status,
            })
        return enriched
