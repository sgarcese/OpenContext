"""Pydantic configuration schema for Socrata plugin."""

from pydantic import Field, field_validator

from core.config_base import BasePluginConfig


class SocrataPluginConfig(BasePluginConfig):
    """Configuration schema for Socrata plugin.

    This schema validates Socrata plugin configuration from config.yaml.
    It reuses the shared ``enabled``/``city_name``/``timeout`` fields and
    :meth:`BasePluginConfig.validate_url` from the base config, adding only
    the Socrata-specific ``base_url``/``portal_url``/``app_token`` fields.
    """

    base_url: str = Field(
        ..., description="Portal URL (e.g., https://data.cityofboston.gov)"
    )
    portal_url: str = Field(
        ..., description="Public portal URL (e.g., https://data.cityofboston.gov)"
    )
    app_token: str | None = Field(
        None,
        description=(
            "Socrata App Token (optional, recommended). Without one, requests "
            "share the portal's per-IP pool and may be throttled; portals such "
            "as data.cdc.gov serve untokened requests but reject an invalid "
            "token (403), so leave this unset rather than guessing. Must be an "
            "App Token from Developer Settings -> App Tokens (or "
            "dev.socrata.com/register) — sent bare as the X-App-Token "
            "header. Do NOT use the Key ID from an API Key pair (Developer "
            "Settings -> API Keys): that credential type is for HTTP Basic "
            "Auth on authenticated requests, which this plugin does not "
            "implement, and a bare Key ID fails with 'Invalid app_token "
            "specified' (403) on query_dataset while search_datasets/"
            "get_dataset appear to keep working."
        ),
    )

    @field_validator("app_token")
    @classmethod
    def validate_app_token(cls, v: str | None) -> str | None:
        """Normalize the app token: a blank value means "no token" (no header)."""
        if v is None or not v.strip():
            return None
        return v.strip()

    _validate_urls = field_validator("base_url", "portal_url")(
        BasePluginConfig.validate_url
    )
