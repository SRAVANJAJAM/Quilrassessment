from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUILR_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    jwt_secret: str = Field(min_length=32)
    jwt_issuer: str = "quilr-assessment"
    jwt_audience: str = "mcp-gateway"
    # API key -> tenant, configured by the operator, never by request headers.
    tenant_keys: dict[str, str] = Field(min_length=1)
    downstream_url: str = "http://127.0.0.1:9001/mcp"
    downstream_token: str = ""
    primary_url: str = "http://127.0.0.1:9001/primary/v1/chat/completions"
    secondary_url: str = "http://127.0.0.1:9001/secondary/v1/chat/completions"
    primary_key: str = ""
    secondary_key: str = ""
    primary_model: str = "demo-primary"
    secondary_model: str = "demo-secondary"
    database_path: str = "data/usage.db"
    token_limit: int = Field(default=50_000, gt=0)
    window_seconds: float = Field(default=60.0, gt=0)
    primary_timeout: float = Field(default=3.0, gt=0)
    secondary_timeout: float = Field(default=3.0, gt=0)
    stream_idle_timeout: float = Field(default=10.0, gt=0)
    stream_total_timeout: float = Field(default=30.0, gt=0)
