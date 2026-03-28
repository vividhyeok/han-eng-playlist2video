"""
Configuration Manager for Lyric Video Maker
Saves and loads user preferences
"""

import json
import os
from typing import Any, Dict, Optional

from app.config.paths import CONFIG_FILE_PATH, ensure_data_dirs

DEFAULT_CONFIG = {
    "translation_model": "gpt-4o-mini",
    "last_playlist_url": "",
    "playlist_lyrics_policy": "allow_plain",
    "output_mode": "video",
}


class ConfigManager:
    """Manages application configuration"""
    
    def __init__(self):
        self.config: Dict[str, Any] = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from file"""
        try:
            if os.path.exists(CONFIG_FILE_PATH):
                with open(CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    # Merge with defaults to ensure all keys exist
                    return {**DEFAULT_CONFIG, **loaded}
        except Exception as e:
            print(f"[WARN] Failed to load config: {e}")
        
        return DEFAULT_CONFIG.copy()
    
    def save_config(self) -> None:
        """Save configuration to file"""
        try:
            ensure_data_dirs()
            os.makedirs(os.path.dirname(CONFIG_FILE_PATH), exist_ok=True)
            with open(CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[ERROR] Failed to save config: {e}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value"""
        return self.config.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        """Set configuration value and save"""
        self.config[key] = value
        self.save_config()
    
    def get_translation_model(self) -> str:
        """Get selected translation model"""
        return self.config.get("translation_model", "gpt-4o-mini")
    
    def set_translation_model(self, model_id: str) -> None:
        """Set translation model"""
        self.set("translation_model", model_id)


# Global config instance
_config_manager: Optional[ConfigManager] = None


def get_config() -> ConfigManager:
    """Get global config manager instance"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager
