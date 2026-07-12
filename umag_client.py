"""Thin client around the (undocumented) UMAG REST API.

Endpoints below were confirmed by inspecting real network traffic from
web.umag.kz on 2026-07-12. Auth is HTTP Basic (phone:password) against a GET
endpoint that returns a sessionToken, which is then sent back as a raw
`Authorization` header value (no "Bearer " prefix) on every later call.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

import httpx

BASE_URL = "https://api.umag.kz/rest/cabinet"
CLIENT_VER = "angular_cabinet_20.0.10"
API_VER = "1.4"


class UmagError(RuntimeError):
    pass


class UmagClient:
    def __init__(self, phone: str, password: str):
        self._phone = phone
        self._password = password
        self._session_token: str | None = None
        self.store_id: int | None = None
        self.company_id: int | None = None
        self.store_group_id: int | None = None
        self._http = httpx.Client(base_url=BASE_URL, timeout=20.0)

    # ---------------------------------------------------------------- auth
    def login(self) -> None:
        basic = base64.b64encode(f"{self._phone}:{self._password}".encode()).decode()
        resp = self._http.get(
            "/org/login/signin",
            headers=self._headers(auth=f"Basic {basic}"),
        )
        if resp.status_code != 200:
            raise UmagError(f"Login failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self._session_token = data["sessionToken"]

    def ensure_store(self) -> None:
        """Populate store_id/company_id from the first available store."""
        resp = self._get("/org/store/list")
        stores = resp if isinstance(resp, list) else resp.get("data", resp)
        if not stores:
            raise UmagError("No stores available on this account")
        store = stores[0]
        self.store_id = store["id"]
        self.company_id = store.get("companyId")
        self.store_group_id = store.get("storeGroupId")

    def _headers(self, auth: str | None = None) -> dict[str, str]:
        h = {
            "client-ver": CLIENT_VER,
            "api-ver": API_VER,
            "Content-Type": "application/json",
        }
        if auth:
            h["Authorization"] = auth
        elif self._session_token:
            h["Authorization"] = self._session_token
        return h

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._http.get(path, params=params, headers=self._headers())
        return self._unwrap(resp)

    def _post(self, path: str, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        resp = self._http.post(path, json=json if json is not None else {}, params=params, headers=self._headers())
        return self._unwrap(resp)

    def _unwrap(self, resp: httpx.Response) -> Any:
        if resp.status_code == 401:
            # session expired mid-run; caller should re-login and retry once
            raise UmagError("Wrong/expired session")
        if resp.status_code >= 400:
            raise UmagError(f"{resp.request.method} {resp.request.url} -> {resp.status_code}: {resp.text}")
        if not resp.content:
            return None
        return resp.json()

    # ------------------------------------------------------------ products
    def search_product(self, query: str) -> list[dict]:
        data = self._post(
            "/nom/product/search",
            json={"fullSearchCriteria": query, "types": [0, 1, 2]},
            params={"storeId": self.store_id},
        )
        return data.get("content", [])

    def list_categories(self) -> list[dict]:
        """Top-level categories: [{id, name, ...}]."""
        data = self._get(
            "/nom/category/find-categories",
            params={"first": 0, "pageSize": 500, "childs": 0, "storeId": self.store_id},
        )
        return data.get("categories", [])

    def find_category_by_name(self, name: str) -> dict | None:
        name_lower = name.strip().lower()
        for cat in self.list_categories():
            if cat["name"].strip().lower() == name_lower:
                return cat
        for cat in self.list_categories():
            if name_lower in cat["name"].strip().lower():
                return cat
        return None

    def next_barcode(self) -> int:
        return self._get("/nom/product-v1/findNextInnerBarcode", params={"storeId": self.store_id})["barcode"]

    def create_product(
        self,
        name: str,
        arrival_cost: float,
        selling_price: float,
        category_id: int,
        barcode: int | None = None,
    ) -> None:
        """Creates a new product ("Товар").

        This endpoint is form-urlencoded (not JSON) with each field itself
        being a JSON string -- confirmed by inspecting real network traffic
        from the "Создать товар" form on 2026-07-12.
        """
        if barcode is None:
            barcode = self.next_barcode()

        product_json = {
            "name": name,
            "measure": "0",  # "шт."
            "type": "2",  # "Внутренний"
            "barcode": str(barcode),
            "categoryId": str(category_id),
        }
        price_json = {
            "arrivalCost": str(arrival_cost),
            "sellingPrice": str(selling_price),
            "wholesalePrice": "0",
        }
        local_code_json = {
            "countryCode": "KZ",
            "storeGroupId": self.store_group_id,
            "barcode": str(barcode),
            "localCode": "",
        }
        form = {
            "productJson": json.dumps(product_json, ensure_ascii=False),
            "productStorePriceJson": json.dumps(price_json, ensure_ascii=False),
            "productList": "[]",
            "additionalCodes": "[]",
            "productLocalCodeJson": json.dumps(local_code_json, ensure_ascii=False),
            "productUnits": "[]",
        }
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = self._http.post(
            "/nom/product/create",
            data=form,
            params={"storeId": self.store_id},
            headers=headers,
        )
        self._unwrap(resp)

    # -------------------------------------------------------- decommission
    def create_decommission(self) -> dict:
        return self._post("/opr/decommissions/create", params={"storeId": self.store_id})

    def add_decommission_products(self, doc_id: int, products: list[dict]) -> list[dict]:
        """products: [{barcode, quantity, price, comment, type}], type=1 is "Испорченный"."""
        return self._post(
            f"/opr/decommissions/{doc_id}/add-products",
            json={"products": products},
            params={"storeId": self.store_id},
        )

    def provide_decommission(self, doc_id: int) -> dict:
        """Finalizes ("Провести") the draft -> stock is decremented."""
        return self._post(f"/opr/decommissions/{doc_id}/provide", params={"storeId": self.store_id})

    def unprovide_decommission(self, doc_id: int) -> dict:
        return self._post(f"/opr/decommissions/{doc_id}/unprovide", params={"storeId": self.store_id})

    def list_decommissions(self, date_from: datetime, date_to: datetime) -> list[dict]:
        params = self._date_range_params(date_from, date_to)
        params.update({"first": 0, "pageSize": 50, "subQuery": "", "statusIds": "[0,1]", "statuses": ["PRV", "DRF"], "storeId": self.store_id})
        return self._get("/opr/decommissions/list", params=params).get("data", [])

    # -------------------------------------------------------------- debits
    # Confirmed live 2026-07-12: create -> add-products -> provide, same
    # verbs as decommissions, but add-products only needs barcode+quantity
    # (price defaults to the product's arrival cost server-side).
    def create_debit(self) -> dict:
        return self._post("/opr/debits/create", params={"storeId": self.store_id})

    def add_debit_products(self, doc_id: int, products: list[dict]) -> list[dict]:
        """products: [{barcode, quantity}] -- price/comment are not accepted here."""
        return self._post(
            f"/opr/debits/{doc_id}/add-products",
            json={"products": products},
            params={"storeId": self.store_id},
        )

    def provide_debit(self, doc_id: int) -> dict:
        return self._post(f"/opr/debits/{doc_id}/provide", params={"storeId": self.store_id})

    def list_debits(self, date_from: datetime, date_to: datetime) -> list[dict]:
        params = self._date_range_params(date_from, date_to)
        params.update({"first": 0, "pageSize": 50, "subQuery": "", "statusIds": "[0,1]", "states": ["PRV", "DRF"], "storeId": self.store_id})
        return self._get("/opr/debits/list", params=params).get("data", [])

    # ------------------------------------------------------------ cashbox
    def list_z_reports(self, date_from: datetime, date_to: datetime) -> list[dict]:
        """Кассовые отчёты по сменам (Z-отчёты)."""
        params = {
            "first": 0,
            "pageSize": 50,
            "fromTime": int(date_from.timestamp() * 1000),
            "toTime": int(date_to.timestamp() * 1000),
            "storeId": self.store_id,
        }
        return self._get("/opr/pos-z-report/list", params=params).get("data", [])

    @staticmethod
    def _date_range_params(date_from: datetime, date_to: datetime) -> dict[str, Any]:
        f = date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        t = date_to.strftime("%Y-%m-%dT%H:%M:%S.999Z")
        return {"dateFrom": f, "dateTo": t, "fromTime": f, "toTime": t}
