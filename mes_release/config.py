import os
from pathlib import Path
from typing import Any, Dict
import yaml
from dotenv import load_dotenv


class ConfigLoader:
    _instance = None
    _config: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._config:
            load_dotenv()
            self._load_config()

    def _load_config(self):
        config_path = Path(os.environ.get("MES_RELEASE_CONFIG", "config.yaml"))
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        with open(config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)

    def get(self, key_path: str, default: Any = None) -> Any:
        keys = key_path.split(".")
        value = self._config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def __getitem__(self, key_path: str) -> Any:
        value = self.get(key_path)
        if value is None:
            raise KeyError(f"配置键不存在: {key_path}")
        return value

    @property
    def raw(self) -> Dict[str, Any]:
        return self._config


config = ConfigLoader()
