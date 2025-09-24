# backtest/datasets.py
from __future__ import annotations
import pandas as pd
from loguru import logger
from decimal import Decimal as D

def load_dataset(path: str) -> pd.DataFrame:
    """
    從 CSV 檔案載入歷史行情數據。

    支援的 CSV 欄位：
    - 必要：timestamp (支援 ISO-8601 或 epoch 秒), cex_bid_price, cex_ask_price, dex_price
    - 可選：gas_price_gwei
    """
    logger.info(f"正在從 {path} 載入數據集...")
    try:
        df = pd.read_csv(path)

        # --- 1) 檢查必要欄位 ---
        required_columns = ["timestamp", "cex_bid_price", "cex_ask_price", "dex_price"]
        missing = [c for c in required_columns if c not in df.columns]
        if missing:
            raise ValueError(f"數據集缺少必要欄位：{missing}；需要：{required_columns}")

        # --- 2) 解析 timestamp：同時支援 ISO-8601 與 epoch(秒) ---
        ts = df["timestamp"]
        if pd.api.types.is_numeric_dtype(ts):
            # 直接視為 epoch 秒（UTC）
            parsed = pd.to_datetime(ts, unit="s", utc=True, errors="coerce")
        else:
            # 先嘗試 ISO-8601
            parsed = pd.to_datetime(ts, utc=True, errors="coerce")
            # 若大多數失敗，再退回嘗試把字串當作數字 epoch 秒
            if parsed.isna().mean() > 0.5:
                as_num = pd.to_numeric(ts, errors="coerce")
                parsed = pd.to_datetime(as_num, unit="s", utc=True, errors="coerce")

        if parsed.isna().any():
            bad_idx = parsed[parsed.isna()].index[:5].tolist()
            raise ValueError(
                f"timestamp 解析失敗（前幾筆無法轉換的索引：{bad_idx}）。"
                f"請確認格式為 ISO-8601（如 2025-09-18T00:00:00Z）或 epoch 秒。"
            )

        df["timestamp"] = parsed

        # --- 3) 嚴格轉型數值欄位 ---
        for col in ["cex_bid_price", "cex_ask_price", "dex_price"]:
            df[col] = pd.to_numeric(df[col], errors="raise")
            # 重要：用字串包一層避免二進位浮點誤差
            df[col] = df[col].astype(str).map(D)

        # gas_price_gwei 為可選；若存在則轉型，不存在就略過
        if "gas_price_gwei" in df.columns:
            df["gas_price_gwei"] = pd.to_numeric(df["gas_price_gwei"], errors="coerce")
            df["gas_price_gwei"] = df["gas_price_gwei"].astype(str).map(lambda x: D(x) if x != "nan" else None)

        # --- 4) 設定索引並排序（UTC-aware）---
        df = df.set_index("timestamp").sort_index()

        logger.success(f"成功載入 {len(df)} 筆數據。")
        return df

    except FileNotFoundError:
        logger.error(f"數據集檔案未找到: {path}")
        raise
    except Exception as e:
        logger.error(f"載入數據集時發生錯誤: {e}")
        raise
