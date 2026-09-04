"""Config flow for SpoolTap: collect the Bambuddy URL (+ optional API key).

Two entry points share one probe:
- ``user``        — first-time setup (creates the entry).
- ``reconfigure`` — change the Bambuddy URL / API key of the EXISTING entry
  (e.g. Bambuddy moved off the HA add-on to another host). The entry id is
  preserved, so the slot->tag registry (a Store keyed by entry id) survives.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bambuddy.rest_client import BambuddyApiError, BambuddyRestClient
from .const import CONF_API_TOKEN, CONF_HOST, DEFAULT_HOST, DOMAIN


def _schema(defaults: dict[str, Any] | None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)): str,
            vol.Optional(CONF_API_TOKEN): str,
        }
    )


class SpoolTapConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SpoolTap."""

    VERSION = 1

    async def _probe(self, host: str, token: str | None) -> str | None:
        """Probe Bambuddy; return an error key or None when reachable.

        ``invalid_auth`` = Bambuddy answered 401/403 (its auth is ON and the key is
        missing, wrong, or lacks the ``can_read_status`` scope). Anything else that
        fails is ``cannot_connect`` — a networking / URL problem.
        """
        session = async_get_clientsession(self.hass)
        rest = BambuddyRestClient(session, host, api_key=token)
        try:
            await rest.health()
        except BambuddyApiError as err:
            return "invalid_auth" if err.status in (401, 403) else "cannot_connect"
        except Exception:  # noqa: BLE001
            return "cannot_connect"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).rstrip("/")
            token = user_input.get(CONF_API_TOKEN) or None
            error = await self._probe(host, token)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"SpoolTap ({host})",
                    data={CONF_HOST: host, CONF_API_TOKEN: token},
                )

        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-point the existing entry at a new Bambuddy URL / key (entry id kept)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).rstrip("/")
            token = user_input.get(CONF_API_TOKEN) or None
            error = await self._probe(host, token)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=host,
                    title=f"SpoolTap ({host})",
                    data_updates={CONF_HOST: host, CONF_API_TOKEN: token},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or dict(entry.data)),
            errors=errors,
        )

    async def async_step_import(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """YAML import fallback (trusts the configured host, skips the probe)."""
        host = str(user_input.get(CONF_HOST, DEFAULT_HOST)).rstrip("/")
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"SpoolTap ({host})",
            data={
                CONF_HOST: host,
                CONF_API_TOKEN: user_input.get(CONF_API_TOKEN),
            },
        )
