"""
Application configuration via Pydantic Settings v2.

Milestone: M1-Step11
Loads all settings from environment variables / .env file.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All application settings. Source of truth: .env.example"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    secret_key: str = ""

    # Database
    database_url: str = ""
    database_url_sync: str = ""
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_celery_broker_url: str = "redis://localhost:6379/1"
    redis_celery_result_backend: str = "redis://localhost:6379/2"

    # JWT
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 30

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None  # None = real AWS; set for LocalStack

    # S3 Buckets
    s3_documents_bucket: str = "fdh-documents-dev"
    s3_exports_bucket: str = "fdh-exports-dev"
    s3_assets_bucket: str = "fdh-assets-dev"

    # Email
    email_backend: str = "console"
    email_from_address: str = "noreply@yourdomain.com"
    email_from_name: str = "Financial Data Hub"
    email_rate_limit_per_hour: int = 10
    aws_ses_region: str = "us-east-1"
    resend_api_key: str = ""

    # Claude AI
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    claude_max_tokens: int = 8192
    claude_requests_per_minute: int = 10

    # Celery
    celery_worker_concurrency: int = 4
    celery_task_soft_time_limit: int = 300
    celery_task_time_limit: int = 360

    # Rate limiting
    rate_limit_unauthenticated: int = 20
    rate_limit_free_tier: int = 60
    rate_limit_pro_tier: int = 300
    rate_limit_enterprise_tier: int = 1000

    # External sources
    edgar_user_agent: str = "FinancialDataHub contact@yourdomain.com"
    edgar_requests_per_second: int = 10
    edgar_base_url: str = "https://efts.sec.gov"

    # Export
    export_signed_url_expiry_seconds: int = 86400
    export_file_retention_days: int = 30
    document_file_retention_days: int = 730

    # Monitoring
    sentry_dsn: str = ""
    prometheus_metrics_port: int = 9090

    # CORS — stored as comma-separated string; use .cors_origins property for list
    allowed_origins: str = "http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        """Returns allowed origins as a list for use in CORSMiddleware."""
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance — import this everywhere."""
    return Settings()
