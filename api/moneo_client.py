"""
Moneo API Client
API key auth. Base URL: https://api.moneo.lv/{COMPANY_ID}/api/v2/{TABLE}
"""

import os
import logging
import requests
from typing import Any, Optional

logger = logging.getLogger(__name__)

MONEO_BASE = "https://api.moneo.lv"


class MoneoError(Exception):
    """Raised when the Moneo API returns an error."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class MoneoClient:
    def __init__(self):
        self.api_key = os.environ.get("MONEO_API_KEY")
        self.company_id = os.environ.get("MONEO_COMPANY_ID")
        if not self.api_key or not self.company_id:
            raise MoneoError("MONEO_API_KEY and MONEO_COMPANY_ID must be set")
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
        resp = self.session.post(url, headers=self._headers(), json=payload, timeout=30)
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
        """
        payload = {
            "filter": filter or {},
            "fields": fields or [],
            "limit": limit,
        }
        result = self._post(table, "/", payload)
        # Moneo returns {"data": [...]} or a list directly
        if isinstance(result, list):
            return result
        return result.get("data", result.get("rows", []))

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
        Query contacts.contacts where CustomerFlag=1.
        Returns all active customers.
        """
        return self.query(
            "contacts.contacts",
            filter={"CustomerFlag": 1},
            fields=["custcode", "companyname", "email", "vatno", "address"],
            limit=limit,
        )

    def get_customer(self, customer_code: str) -> Optional[dict]:
        """Fetch a single customer by code."""
        results = self.query(
            "contacts.contacts",
            filter={"custcode": customer_code, "CustomerFlag": 1},
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
        Enrich invoice list with payment status.
        Moneo invoices have fields like: invnr, invdate, total, paid, custcode, ...
        Status logic:
          - paid >= total → 'paid'
          - paid > 0 and paid < total → 'partial'
          - paid == 0 → 'unpaid'
        Returns the same list with 'payment_status' field added.
        """
        enriched = []
        for inv in invoices:
            total = float(inv.get("total", inv.get("invsum", 0)) or 0)
            paid = float(inv.get("paid", inv.get("paidsum", 0)) or 0)
            if total <= 0:
                status = "unpaid"
            elif paid >= total - 0.01:  # small epsilon for float rounding
                status = "paid"
            elif paid > 0:
                status = "partial"
            else:
                status = "unpaid"
            enriched.append({**inv, "payment_status": status})
        return enriched
