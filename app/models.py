from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Role(str, Enum):
    admin = "admin"
    manager = "manager"
    collector = "collector"


class Mode(str, Enum):
    cash = "Cash"
    cheque = "Cheque"
    upi = "UPI"
    rtgs = "RTGS"


class Ageing(BaseModel):
    """Outstanding split into ageing buckets (in rupees)."""
    age_0_30: float = 0
    age_31_60: float = 0
    age_61_90: float = 0
    age_90p: float = 0


# ---- auth ----
class BootstrapIn(BaseModel):
    name: str
    pin: str = Field(min_length=4, max_length=8)


class LoginIn(BaseModel):
    name: str
    pin: str


# ---- users ----
class UserIn(BaseModel):
    name: str
    pin: str = Field(min_length=4, max_length=8)
    role: Role = Role.collector
    price_list_access: bool = False
    can_collect: bool = False


class UserPatch(BaseModel):
    name: Optional[str] = None
    pin: Optional[str] = Field(default=None, min_length=4, max_length=8)
    role: Optional[Role] = None
    price_list_access: Optional[bool] = None
    can_collect: Optional[bool] = None


# ---- dealers ----
class DealerIn(BaseModel):
    name: str
    area: Optional[str] = None
    phone: Optional[str] = None
    credit_limit: float = 0
    collector_id: Optional[str] = None
    ageing: Ageing = Field(default_factory=Ageing)


class DealerPatch(BaseModel):
    name: Optional[str] = None
    area: Optional[str] = None
    phone: Optional[str] = None
    credit_limit: Optional[float] = None
    collector_id: Optional[str] = None
    ageing: Optional[Ageing] = None


# ---- stock ----
class StockIn(BaseModel):
    name: str
    price: float = 0
    qty: int = 0


class StockPatch(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    qty: Optional[int] = None


# ---- payments ----
class CollectIn(BaseModel):
    dealer_id: str
    amount: float = Field(gt=0)
    mode: Mode = Mode.cash
    cheque: Optional[str] = None


class ChequeUpdate(BaseModel):
    cleared: bool  # True -> cleared, False -> bounced (restores outstanding)


class DepositIn(BaseModel):
    collector_id: str


class ApproveIn(BaseModel):
    approved: bool


class ReconcileIn(BaseModel):
    reconciled: bool


# ---- products / price list ----
class ProductIn(BaseModel):
    category: str
    model: str
    description: Optional[str] = ""
    mrp: Optional[float] = None
    dp: Optional[float] = None
    nlc: Optional[float] = None


class ProductBulk(BaseModel):
    products: list[ProductIn]


# ---- price lists (faithful, multi-sheet, any columns) ----
class PriceSheet(BaseModel):
    name: str
    columns: list[str]
    rows: list[list]


class PriceListIn(BaseModel):
    name: str
    allowed_users: list[str] = []
    sheets: list[PriceSheet]


class PriceListPatch(BaseModel):
    name: Optional[str] = None
    allowed_users: Optional[list[str]] = None


# ---- price lists (multi-brand) ----
class PriceListIn(BaseModel):
    name: str
    allowed_user_ids: list[str] = []


class PriceListPatch(BaseModel):
    name: Optional[str] = None
    allowed_user_ids: Optional[list[str]] = None


# ---- bills / ledger ----
class BillIn(BaseModel):
    bill_no: str
    date: str          # YYYY-MM-DD
    amount: float = Field(gt=0)


class SeedPayment(BaseModel):
    ref: Optional[str] = ""
    date: Optional[str] = None
    amount: float


class SeedIn(BaseModel):
    opening: float = 0
    opening_date: Optional[str] = None
    bills: list[BillIn] = []
    payments: list[SeedPayment] = []


class BulkBillRow(BaseModel):
    dealer_id: Optional[str] = None
    dealer_name: Optional[str] = None
    bill_no: str
    date: str
    amount: float


class BulkBills(BaseModel):
    bills: list[BulkBillRow]
