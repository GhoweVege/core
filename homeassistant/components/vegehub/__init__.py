"""The Vegetronix VegeHub integration."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
import logging
from typing import Any

from aiohttp.hdrs import METH_POST
from aiohttp.web import Request, Response
from vegehub import VegeHub

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.webhook import (
    async_generate_id as webhook_generate_id,
    async_generate_url as webhook_generate_url,
    async_register as webhook_register,
    async_unregister as webhook_unregister,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_IP_ADDRESS,
    CONF_MAC,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from .const import DOMAIN, MANUFACTURER, MODEL, NAME, PLATFORMS
from .coordinator import VegeHubCoordinator

_LOGGER = logging.getLogger(__name__)

type VegeHubConfigEntry = ConfigEntry[VegeHub]


@dataclass
class VegeHubData:
    """Define a data class."""

    coordinator: VegeHubCoordinator
    hub: VegeHub


# The integration is only set up through the UI (config flow)
async def async_setup_entry(hass: HomeAssistant, entry: VegeHubConfigEntry) -> bool:
    """Set up VegeHub from a config entry."""

    # Register the device in the device registry
    device_registry = dr.async_get(hass)

    device_mac = entry.data[CONF_MAC]
    device_ip = entry.data[CONF_IP_ADDRESS]

    assert entry.unique_id

    hub = VegeHub(device_ip, device_mac, entry.unique_id)

    webhook_id = webhook_generate_id()
    webhook_url = webhook_generate_url(
        hass,
        webhook_id,
        allow_external=False,
        allow_ip=True,
    )

    # Send the webhook address to the hub as its server target
    try:
        await hub.setup("", webhook_url, retries=1)
    except ConnectionError as err:
        raise ConfigEntryError("Error connecting to device") from err
    except TimeoutError as err:
        raise ConfigEntryNotReady("Device is not responding") from err

    # Initialize runtime data
    entry.runtime_data = VegeHubData(
        coordinator=VegeHubCoordinator(hass=hass, device_id=entry.unique_id), hub=hub
    )

    # Register the device
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(CONNECTION_NETWORK_MAC, device_mac)},
        identifiers={(DOMAIN, device_mac)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=entry.data[CONF_HOST],
        sw_version=hub.sw_version,
        configuration_url=hub.url,
    )

    async def unregister_webhook(_: Any) -> None:
        webhook_unregister(hass, webhook_id)

    async def register_webhook() -> None:
        webhook_name = f"{NAME} {device_mac}"

        webhook_register(
            hass,
            DOMAIN,
            webhook_name,
            webhook_id,
            get_webhook_handler(
                device_mac, entry.entry_id, entry.runtime_data.coordinator
            ),
            allowed_methods=[METH_POST],
        )

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, unregister_webhook)
        )

    # Now add in all the entities for this device.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_create_background_task(
        hass, register_webhook(), "vegehub_register_webhook"
    )

    # Ask the hub for an update, so that we have its initial data
    await hub.request_update()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: VegeHubConfigEntry) -> bool:
    """Unload a VegeHub config entry."""

    # Unload platforms
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def get_webhook_handler(
    device_mac: str, entry_id: str, coordinator: VegeHubCoordinator
) -> Callable[[HomeAssistant, str, Request], Awaitable[Response | None]]:
    """Return webhook handler."""

    async def async_webhook_handler(
        hass: HomeAssistant, webhook_id: str, request: Request
    ) -> Response | None:
        # Handle http post calls to the path.
        if not request.body_exists:
            return HomeAssistantView.json(
                result="No Body", status_code=HTTPStatus.BAD_REQUEST
            )
        data = await request.json()

        sensor_data = {}
        # Process sensor data
        if "sensors" in data:
            for sensor in data["sensors"]:
                slot = sensor.get("slot")
                latest_sample = sensor["samples"][-1]
                value = latest_sample["v"]
                entity_id = f"vegehub_{device_mac}_{slot}".lower()

                # Build a dict of the data we want so that we can pass it to the coordinator
                sensor_data[entity_id] = value

        if coordinator and sensor_data:
            await coordinator.async_update_data(sensor_data)

        return HomeAssistantView.json(result="OK", status_code=HTTPStatus.OK)

    return async_webhook_handler


async def _update_sensor_entity(
    hass: HomeAssistant, value: float, entity_id: str, entry_id: str
):
    """Update the corresponding Home Assistant entity with the latest sensor value."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        _LOGGER.error("Entry %s not found", entry_id)
        return

    # Find the sensor entity and update its state
    entity = None
    try:
        entity = entry.runtime_data.hub.entities.get(entity_id)
        if not entity:
            _LOGGER.error("Sensor entity %s not found", entity_id)
        else:
            await entity.async_update_sensor(value)
    except Exception as e:
        _LOGGER.error("Sensor entity %s not found: %s", entity_id, e)
        raise
