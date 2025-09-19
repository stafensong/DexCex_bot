from __future__ import annotations
from pydantic import BaseModel, Field
from decimal import Decimal
from typing import Literal, Optional, List

__all__ = [
    "MarketPair",
    "Quote",
    "Opportunity",
    "CexOrder",
    "OrderUpdate",
    "ExecutionLeg",
    "ExecutionSummary",
    "DexSwapParams",
    "DexTxReceipt",
]

# --- Core Data Structures ---

class MarketPair(BaseModel):
    base: str
    quote: str
    symbol: str  # e.g., "ETH/USDT"
    base_precision: int = 8
    quote_precision: int = 8

class Quote(BaseModel):
    pair: MarketPair
    price: Decimal
    size: Decimal
    venue: Literal["CEX", "DEX"]
    timestamp: float
    side: Optional[Literal["buy", "sell"]] = None
    bid_price: Optional[Decimal] = None
    ask_price: Optional[Decimal] = None
    bid_size: Optional[Decimal] = None
    ask_size: Optional[Decimal] = None

class Opportunity(BaseModel):
    pair: MarketPair
    direction: Literal["CEX_to_DEX", "DEX_to_CEX"]
    size: Decimal
    cex_price: Decimal
    dex_price: Decimal
    edge_bps: Decimal
    slippage_bps: Decimal
    gas_cost_quote: Decimal
    cex_fee_quote: Decimal
    expected_pnl_quote: Decimal
    valid_until: float

# --- DEX Specific Types ---

class DexSwapParams(BaseModel):
    token_in: str
    token_out: str
    fee: int
    amount_in_wei: int
    recipient: str
    deadline: int
    amount_out_min_wei: int

class DexTxReceipt(BaseModel):
    tx_hash: str
    block_number: Optional[int] = None
    gas_used: Optional[int] = None
    status: Optional[int] = None
    timestamp: Optional[float] = None

# --- CEX & Execution Data Structures (User Provided) ---

class CexOrder(BaseModel):
    order_id: str
    pair: "MarketPair"
    side: Literal["buy", "sell"]
    price: Decimal
    size: Decimal
    tif: Literal["IOC", "FOK", "GTC"] = "IOC"
    status: Literal["new","partially_filled","filled","canceled","rejected"] = "new"
    ts: float

class OrderUpdate(BaseModel):
    order_id: str
    status: Literal["partially_filled","filled","canceled","rejected"]
    filled_size: Decimal
    avg_fill_price: Optional[Decimal] = None
    reason: Optional[str] = None
    ts: float

class ExecutionLeg(BaseModel):
    venue: Literal["CEX","DEX"]
    side: Literal["buy","sell"]
    price_quote: Decimal
    size: Decimal
    fees_quote: Decimal
    tx_hash: Optional[str] = None
    order_id: Optional[str] = None

class ExecutionSummary(BaseModel):
    pair: "MarketPair"
    direction: Literal["CEX_to_DEX","DEX_to_CEX"]
    size: Decimal
    legs: List[ExecutionLeg]
    gas_quote: Decimal
    pnl_quote: Decimal
    edge_bps: Decimal
    hedged: bool
    started_ts: float
    completed_ts: float