import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("TIDE_DB_PATH", "/data/tide.db")
    api_title: str = "潮位通航窗口 API"


settings = Settings()
