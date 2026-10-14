from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    lark_app_id: str
    lark_app_secret: SecretStr
    ark_api_key: SecretStr
    ark_model: str
    ark_reasoning_effort: Literal["low", "medium", "high", "max", "none", "minimal", "xhigh"] = (
        "low"
    )
    model_output_token_limit: int = Field(default=16384, ge=1024, le=65536)
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    bitable_base_token: SecretStr
    bitable_table_id: str
    bitable_view_id: str | None = None
    bitable_date_field: str = "记账日期"
    bitable_amount_field: str = "金额"
    bitable_description_field: str = "具体"
    lark_cli_path: str = "lark-cli"
    lark_cli_identity: Literal["user", "bot"] = "user"
    timezone: str = "Asia/Shanghai"
    allowed_user_ids: str = ""
    allow_groups: bool = False
    model_request_limit: int = Field(default=12, ge=1, le=50)
    model_timeout_seconds: float = Field(default=90, gt=0)
    task_timeout_seconds: float = Field(default=300, gt=0)
    cli_timeout_seconds: float = Field(default=30, gt=0)
    runtime_dir: Path = Path(".runtime")
    project_dir: Path = Path.cwd()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("ark_base_url")
    @classmethod
    def secure_model_endpoint(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("ARK_BASE_URL 必须使用 HTTPS")
        return value.rstrip("/")

    @field_validator("lark_app_secret", "ark_api_key", "bitable_base_token")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("凭证不能为空")
        return value

    @property
    def user_allowlist(self) -> list[str]:
        return [item.strip() for item in self.allowed_user_ids.split(",") if item.strip()]

    @property
    def private_dir(self) -> Path:
        path = self.runtime_dir
        return path if path.is_absolute() else self.project_dir / path

    @property
    def writable_fields(self) -> tuple[str, ...]:
        return (
            self.bitable_date_field,
            "一级分类",
            "二级分类",
            "三级分类",
            self.bitable_description_field,
            self.bitable_amount_field,
        )

    @property
    def secrets(self) -> list[str]:
        return [
            self.ark_api_key.get_secret_value(),
            self.lark_app_secret.get_secret_value(),
            self.bitable_base_token.get_secret_value(),
        ]


def load_settings(project_dir: Path, env_file: Path | None = None) -> Settings:
    project_dir = project_dir.resolve()
    selected = env_file or project_dir / "env.txt"
    return Settings(_env_file=selected, _env_file_encoding="utf-8", project_dir=project_dir)
