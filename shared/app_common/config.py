from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class BaseServiceSettings(BaseSettings):
    app_name: str
    service_slug: str
    rest_host: str = "0.0.0.0"
    rest_port: int
    grpc_host: str = "0.0.0.0"
    grpc_port: int
    database_url: str
    auth_grpc_target: str = "localhost:50051"
    periods_grpc_target: str = "localhost:50052"
    academics_grpc_target: str = "localhost:50053"
    grades_grpc_target: str = "localhost:50054"
    attendance_grpc_target: str = "localhost:50055"
    notifications_grpc_target: str = "localhost:50056"
    reports_grpc_target: str = "localhost:50057"
    cors_origins: str = "*"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def resolved_cors_origins(self) -> list[str]:
        return [value.strip() for value in self.cors_origins.split(",") if value.strip()]

