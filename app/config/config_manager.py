"""Small JSON-backed settings store used by both the web and legacy UIs."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from app.config.paths import CONFIG_FILE_PATH, ensure_data_dirs

DEFAULT_CONFIG = {
    "translation_model": "gpt-5.4-mini",
    "last_input": "",
    "playlist_lyrics_policy": "allow_plain",
    "output_mode": "video",
    "render_engine": "fast_ass",
}


class ConfigManager:
    def __init__(self) -> None:
        self.config: Dict[str, Any] = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        try:
            if os.path.exists(CONFIG_FILE_PATH):
                with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as file:
                    loaded = json.load(file)
                if isinstance(loaded, dict):
                    return {**DEFAULT_CONFIG, **loaded}
        except Exception as exc:
            print(f"[WARN] Failed to load config: {exc}")
        return DEFAULT_CONFIG.copy()

    def save_config(self) -> None:
        ensure_data_dirs()
        try:
            with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[ERROR] Failed to save config: {exc}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.config[key] = value
        self.save_config()

    def get_translation_model(self) -> str:
        return str(self.config.get("translation_model", DEFAULT_CONFIG["translation_model"]))

    def set_translation_model(self, model_id: str) -> None:
        self.set("translation_model", model_id)


_config_manager: Optional[ConfigManager] = None


def get_config() -> ConfigManager:
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager
