"""
PAX8 API Client
OAuth 2.0 client credentials flow, token cached in memory for 24h.
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta

from secrets_store import get_secret

# Per-process product cache: productId → {sku, name, vendorName}
_product_cache: dict = {}
# Per-process invoice-items cache: (year, month) → list of raw PAX8 invoice items
_invoice_items_cache: dict = {}

logger = logging.getLogger(__name__)

PAX8_TOKEN_URL = "https://api.pax8.com/v1/token"
PAX8_AUDIENCE = "https://api.pax8.com"
PAX8_BASE_URL = "https://api.pax8.com/v1"

# In-memory token cache
_token_cache = {
    "access_token": None,
    "expires_at": 0,
}


class PAX8Error(Exception):
    """Raised when the PAX8 API returns an error."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class PAX8Client:
    def __init__(self):
        self.client_id = get_secret("pax8_client_id")
        self.client_secret = get_secret("pax8_client_secret")
        if not self.client_id or not self.client_secret:
            raise PAX8Error("PAX8 client ID and secret must be configured in Iestatījumi")
        self.session = requests.Session()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_token(self) -> str:
        """Return a valid OAuth access token, fetching a new one if needed."""
        now = time.time()
        if _token_cache["access_token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

        logger.info("Fetching new PAX8 OAuth token")
        resp = self.session.post(
            PAX8_TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "audience": PAX8_AUDIENCE,
            },
            timeout=30,
        )
        if not resp.ok:
            raise PAX8Error(
                f"PAX8 token fetch failed: {resp.status_code} {resp.text}",
                status_code=resp.status_code,
            )
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        # PAX8 tokens are valid 24 h; fall back to 23 h if not specified
        expires_in = data.get("expires_in", 82800)
        _token_cache["expires_at"] = now + expires_in
        return _token_cache["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{PAX8_BASE_URL}{path}"
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        if not resp.ok:
            raise PAX8Error(
                f"PAX8 GET {path} failed: {resp.status_code} {resp.text}",
                status_code=resp.status_code,
            )
        return resp.json()

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------

    def list_companies(self, page: int = 0, size: int = 200) -> dict:
        """GET /companies — paginated list of partner's managed companies."""
        return self._get("/companies", params={"page": page, "size": size})

    def get_company(self, company_id: str) -> dict:
        """GET /companies/{id}"""
        return self._get(f"/companies/{company_id}")

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def list_subscriptions(self, company_id: str, page: int = 0, size: int = 200) -> dict:
        """GET /subscriptions?companyId=X (PAX8 v1 flat endpoint)."""
        return self._get(
            "/subscriptions",
            params={"companyId": company_id, "page": page, "size": size},
        )

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def get_product(self, product_id: str) -> dict:
        """GET /products/{id}, cached per process."""
        if product_id in _product_cache:
            return _product_cache[product_id]
        try:
            data = self._get(f"/products/{product_id}")
            info = {
                "sku": data.get("sku") or "",
                "name": data.get("name") or "",
                "vendorName": data.get("vendorName") or "",
            }
        except PAX8Error as e:
            logger.warning(f"Product lookup failed for {product_id}: {e}")
            info = {"sku": "", "name": "", "vendorName": ""}
        _product_cache[product_id] = info
        return info

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    def get_usage_summary(
        self,
        company_id: str,
        resource_group: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        GET /usage-summaries
        start_date / end_date: ISO date strings, e.g. '2026-04-01'
        """
        return self._get(
            "/usage-summaries",
            params={
                "companyId": company_id,
                "resourceGroup": resource_group,
                "startDate": start_date,
                "endDate": end_date,
            },
        )

    def get_detailed_usage_summary(
        self,
        company_id: str,
        start_date: str,
        end_date: str,
        page: int = 0,
        size: int = 200,
    ) -> dict:
        """
        GET /usage-summaries with detailed breakdown.
        Used to fetch all usage items for a company in a date range.
        """
        return self._get(
            "/usage-summaries",
            params={
                "companyId": company_id,
                "startDate": start_date,
                "endDate": end_date,
                "page": page,
                "size": size,
            },
        )

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------

    def list_invoices(
        self,
        start_date: str,
        end_date: str,
        page: int = 0,
        size: int = 200,
    ) -> dict:
        """
        GET /invoices — partner-level invoice list.
        start_date / end_date: ISO date strings.
        """
        return self._get(
            "/invoices",
            params={
                "startDate": start_date,
                "endDate": end_date,
                "page": page,
                "size": size,
            },
        )

    def get_invoice(self, invoice_id: str) -> dict:
        """GET /invoices/{id}"""
        return self._get(f"/invoices/{invoice_id}")

    def get_invoice_items(self, invoice_id: str, page: int = 0, size: int = 200) -> dict:
        """GET /invoices/{id}/items — paginated invoice line items."""
        return self._get(
            f"/invoices/{invoice_id}/items",
            params={"page": page, "size": size},
        )

    def get_all_invoice_items(self, invoice_id: str) -> list:
        """Fetch all items for an invoice, handling pagination."""
        items = []
        page = 0
        size = 200
        while True:
            data = self.get_invoice_items(invoice_id, page=page, size=size)
            chunk = data.get("content", data.get("data", []))
            items.extend(chunk)
            total_pages = data.get("page", {}).get("totalPages", 1) if isinstance(data.get("page"), dict) else 1
            if page + 1 >= total_pages or len(chunk) < size:
                break
            page += 1
        return items

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_all_companies(self) -> list:
        """Fetch all companies, handling pagination automatically."""
        companies = []
        page = 0
        size = 200
        while True:
            data = self.list_companies(page=page, size=size)
            items = data.get("content", data.get("data", []))
            companies.extend(items)
            # PAX8 pagination: check if there are more pages
            total_pages = data.get("totalPages", data.get("page", {}).get("totalPages", 1))
            if page + 1 >= total_pages or len(items) < size:
                break
            page += 1
        return companies

    def get_all_subscriptions(self, company_id: str) -> list:
        """Fetch all subscriptions for a company, handling pagination."""
        subscriptions = []
        page = 0
        size = 200
        while True:
            data = self.list_subscriptions(company_id, page=page, size=size)
            items = data.get("content", data.get("data", []))
            subscriptions.extend(items)
            total_pages = data.get("totalPages", data.get("page", {}).get("totalPages", 1))
            if page + 1 >= total_pages or len(items) < size:
                break
            page += 1
        return subscriptions

    def get_invoice_items_for_month(self, year: int, month: int) -> list:
        """
        Fetch every PAX8 invoice item billed for a given month, across all companies.

        PAX8 issues partner invoices on the 1st of each month. We pick any invoice
        whose invoiceDate falls within the target month and pull all its items.

        Result is cached per (year, month) in-process so repeated per-company calls
        don't refetch.
        """
        cache_key = (year, month)
        if cache_key in _invoice_items_cache:
            return _invoice_items_cache[cache_key]

        from calendar import monthrange
        last_day = monthrange(year, month)[1]
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{year}-{month:02d}-{last_day:02d}"

        all_items: list = []
        try:
            invoices_resp = self.list_invoices(start_date, end_date, size=50)
            invoices = invoices_resp.get("content", invoices_resp.get("data", []))
        except PAX8Error as e:
            logger.warning(f"Failed to list invoices for {year}-{month:02d}: {e}")
            return []

        for inv in invoices:
            invoice_date = inv.get("invoiceDate", "")
            if not (invoice_date.startswith(f"{year}-{month:02d}")):
                continue
            try:
                items = self.get_all_invoice_items(inv["id"])
                all_items.extend(items)
            except PAX8Error as e:
                logger.warning(f"Failed to fetch items for invoice {inv.get('id')}: {e}")

        _invoice_items_cache[cache_key] = all_items
        return all_items

    def get_billing_for_month(self, company_id: str, year: int, month: int) -> list:
        """
        Returns billing line items for a company for a given month, sourced from
        the PAX8 partner invoice line items (the authoritative partner cost data).

        Each returned line matches a row on the PAX8 invoice:
          sku, productName, quantity, cost (unit partner cost), costTotal,
          term (Monthly/Annual/Usage), startPeriod, endPeriod, type
          (subscription | prorate | usage | ...), vendorName.
        """
        items = self.get_invoice_items_for_month(year, month)
        billing_lines = []

        for item in items:
            if item.get("companyId") != company_id:
                continue

            sku = item.get("sku") or item.get("productId") or "UNKNOWN"
            # costTotal is what PAX8 charges us for this line; fall back to unit * qty.
            quantity = float(item.get("quantity") or 0)
            unit_cost = float(item.get("cost") or 0)
            total_cost = float(item.get("costTotal") or (quantity * unit_cost) or 0)

            billing_lines.append({
                "type": item.get("type", "subscription"),
                "sku": sku,
                "name": item.get("productName") or item.get("description") or sku,
                "quantity": quantity,
                "unit_cost": unit_cost,
                "total_cost": round(total_cost, 4),
                "billing_term": item.get("term", "Monthly"),
                "start_period": item.get("startPeriod", ""),
                "end_period": item.get("endPeriod", ""),
                "subscription_id": item.get("subscriptionId"),
                "vendor": item.get("vendorName", ""),
                "description": item.get("description", ""),
            })

        return billing_lines
