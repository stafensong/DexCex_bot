import os
import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from typing import List, Dict, Literal

# --- Helper function to load YAML with environment variable substitution ---
def load_yaml_with_env(path: str) -> dict:
    with open(path, 'r') as f:
        content = f.read()
    
    # Substitute ${VAR_NAME} with environment variables
    expanded_content = os.path.expandvars(content)
    return yaml.safe_load(expanded_content)

# --- Pydantic Models for Configuration ---

class NetworkConfig(BaseModel):
    default_chain: str
    rpc_urls: Dict[str, str] = Field(default_factory=dict)
    max_pending_seconds: int
    gas_estimation_chain: str
    priority_fee_gwei: float
    max_fee_gwei: float

    @field_validator('rpc_urls', mode='before')
    def load_rpc_urls(cls, v):
        return {
            "ethereum": os.getenv("ETH_RPC_URL", ""),
            "arbitrum": os.getenv("ARBITRUM_RPC_URL", ""),
            "bsc": os.getenv("BSC_RPC_URL", ""),
        }

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

class SecretsConfig(BaseModel):
    binance_api_key: SecretStr
    binance_api_secret: SecretStr
    dex_wallet_private_key: SecretStr

    @classmethod
    def load(cls):
        return cls(
            binance_api_key=os.getenv("BINANCE_API_KEY", ""),
            binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
            dex_wallet_private_key=os.getenv("DEX_WALLET_PRIVATE_KEY", ""),
        )

class AppConfig(BaseModel):
    env: str
    network: NetworkConfig
    dex: DexConfig
    cex: CexConfig
    risk: RiskConfig
    inventory: InventoryConfig
    observability: ObservabilityConfig
    pairs: List[PairConfig]
    secrets: SecretsConfig = Field(default_factory=SecretsConfig.load)


def load_config(
    default_path: str = "config/default.yaml",
    pairs_path: str = "config/pairs.yaml"
) -> AppConfig:
    """Loads configuration from YAML files and environment variables."""
    # Load YAML files
    default_cfg_data = load_yaml_with_env(default_path)
    pairs_cfg_data = load_yaml_with_env(pairs_path)

    # Combine data
    full_config_data = {**default_cfg_data, **pairs_cfg_data}

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
