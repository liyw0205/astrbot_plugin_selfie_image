"""Plugin constants."""

PLUGIN_NAME = "astrbot_plugin_selfie_image"
LEGACY_PLUGIN_NAME = "astrbot_plugin_aicat"
PLUGIN_DISPLAY_NAME = "Selfie Image 生图自拍"
PLUGIN_AUTHOR = "Selfie Image"
PLUGIN_VERSION = "1.0.0"
PLUGIN_CONFIG_FILENAME = "selfie_image_config.json"
LEGACY_CONFIG_FILENAME = "aicat_config.json"

ASPECT_RATIOS = ["自动", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
RESOLUTIONS = ["1K", "2K", "4K"]
PROVIDER_TYPES = ["openai", "gemini", "gemini_openai", "z_image_gitee", "jimeng2api", "grok", "agnes"]
