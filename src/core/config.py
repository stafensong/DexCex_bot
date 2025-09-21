import os
import yaml
from decimal import Decimal
from typing import List, Dict, Literal, Optional

from pydantic import BaseModel, Field, SecretStr, model_validator
from dotenv import load_dotenv
from loguru import logger

# --- Helper function to load YAML with environment variable substitution ---
def load_yaml_with_env(path: str) -> dict:
    with open(path, 'r') as f:
        content = f.read()
    
    # Substitute ${VAR_NAME} with environment variables
    expanded_content = os.path.expandvars(content)
    return yaml.safe_load(expanded_content)

# --- Pydantic Models for Configuration ---
# All component models must be defined before the main AppConfig model

class TokenDetails(BaseModel):
    address: str
    decimals: int

class NetworkConfig(BaseModel):
    default_chain: str
    rpc_urls: Dict[str, str] = {}
    native_token: Dict[str, str] = {}
    max_pending_seconds: int
    gas_estimation_chain: str
    priority_fee_gwei: float
    max_fee_gwei: float

    @model_validator(mode='after')
    def load_rpc_urls_from_env(self) -> 'NetworkConfig':
        raw_urls = {
            "ethereum": os.getenv("ETH_RPC_URL", ""),
            "arbitrum": os.getenv("ARBITRUM_RPC_URL", ""),
            "bsc": os.getenv("BSC_RPC_URL", ""),
            "base": os.getenv("BASE_RPC_URL", ""),
        }
        # 過濾掉值為空的 URL
        self.rpc_urls = {chain: url for chain, url in raw_urls.items() if url}
        return self

class DexContracts(BaseModel):
    router: str
    quoter_v2: str
    weth: str

class DexConfig(BaseModel):
    uniswap_v3: Dict[str, DexContracts]

class CexConfig(BaseModel):
    name: str
    base_url: str
    ws_url: str
    api_key_env: str
    api_secret_env: str
    recv_window_ms: int

class RiskConfig(BaseModel):
    max_notional_per_leg_quote: float
    max_position_per_asset: float
    circuit_breaker_bps: int
    cancel_all_on_start: bool
    cancel_all_on_shutdown: bool

class RebalanceConfig(BaseModel):
    enable: bool
    target_ratio: float
    trigger_bps: int
    method: Literal["on_cex", "on_dex", "cross"]

class InventoryConfig(BaseModel):
    rebalance: RebalanceConfig

class ObservabilityConfig(BaseModel):
    metrics_port: int
    log_level: str
    redact_keys: List[str]

class PairConfig(BaseModel):
    base: str
    quote: str
    cex_symbol: str
    min_edge_bps: int
    max_slippage_bps: int
    max_size_quote: int
    dex_chain: str
    dex_pool_fee: int
    price_floor_quote: Optional[Decimal] = None
    price_ceiling_quote: Optional[Decimal] = None
    max_edge_bps: Optional[int] = None

class SecretsConfig(BaseModel):
    binance_api_key: SecretStr
    binance_api_secret: SecretStr
    dex_wallet_private_key: SecretStr

    @classmethod
    def load(cls):
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
        private_key = os.getenv("DEX_WALLET_PRIVATE_KEY", "")

        if not private_key:
            raise ValueError("關鍵錯誤：DEX_WALLET_PRIVATE_KEY 未在您的 .env 檔案中設定或值為空。")
        if not api_key:
            logger.warning("BINANCE_API_KEY 未設定，CEX 功能將受限。")
        if not api_secret:
            logger.warning("BINANCE_API_SECRET 未設定，CEX 功能將受限。")

        return cls(
            binance_api_key=api_key,
            binance_api_secret=api_secret,
            dex_wallet_private_key=private_key,
        )

# Main AppConfig model
class AppConfig(BaseModel):
    env: str
    network: NetworkConfig
    dex: DexConfig
    cex: CexConfig
    risk: RiskConfig
    inventory: InventoryConfig
    observability: ObservabilityConfig
    pairs: List[PairConfig]
    tokens: Dict[str, Dict[str, TokenDetails]]
    secrets: SecretsConfig = Field(default_factory=SecretsConfig.load)


def load_config(
    default_path: str = "config/default.yaml",
    pairs_path: str = "config/pairs.yaml",
    tokens_path: str = "config/tokens.yaml"
) -> AppConfig:
    """Loads configuration from YAML files and environment variables."""
    load_dotenv()
    # Load YAML files
    default_cfg_data = load_yaml_with_env(default_path)
    pairs_cfg_data = load_yaml_with_env(pairs_path)
    tokens_cfg_data = load_yaml_with_env(tokens_path)

    # Combine data
    full_config_data = {**default_cfg_data, **pairs_cfg_data, **tokens_cfg_data}

    # Parse with Pydantic
    return AppConfig(**full_config_data)

# Example usage:
if __name__ == '__main__':
    # This requires .env file to be present in the root directory
    from dotenv import load_dotenv
    load_dotenv()
    
    config = load_config()
    print("Config loaded successfully!")
    print(f"Environment: {config.env}")
    print(f"Default Chain: {config.network.default_chain}")
    print(f"CEX: {config.cex.name}")
    print(f"Monitored Pairs: {[p.cex_symbol for p in config.pairs]}")
    # Accessing secrets
    print(f"Binance Key (first 5 chars): {config.secrets.binance_api_key.get_secret_value()[:5]}...")
