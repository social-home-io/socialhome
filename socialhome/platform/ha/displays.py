"""Home Assistant's ESPHome wall panels (the HA :class:`DisplayDirectory`).

Shared by :class:`~socialhome.platform.ha.HaAdapter` and
:class:`~socialhome.platform.haos.HaosAdapter` (both talk to HA Core
through the same :class:`~socialhome.platform.ha.client.HaClient`).

HA contract — verified against home-assistant/core 2026.10.1, not inferred:

* **Finding the panels.** WS ``config/device_registry/list``
  (``homeassistant/components/config/device_registry.py``
  ``websocket_list_devices``; no admin needed) answers every device as
  ``DeviceEntry.dict_repr`` (``homeassistant/helpers/device_registry.py``):
  ``id``, ``name``, ``name_by_user``, ``manufacturer``, ``model``,
  ``sw_version``, ``connections`` (``[[type, value]]``), ``disabled_by``…
  The ESPHome integration fills them in
  ``homeassistant/components/esphome/manager.py``
  (``_async_setup_device_registry`` + ``async_get_manufacturer_model``):
  the firmware's ``esphome.project`` name
  ``social-home-io.esphome-dashboard`` is split on ``.`` into
  ``manufacturer="social-home-io"`` / ``model="esphome-dashboard"``;
  ``sw_version`` is ``"<project version> (ESPHome <version>)"``; the main
  device has ``connections={("mac", <mac>)}`` (normalised lower-case by
  ``format_mac``) and no ``identifiers``; ``name`` is the friendly name
  (else the node name).
* **The connect action.** ESPHome registers a firmware ``api: actions:``
  entry as the HA action ``esphome.<node name, "-" → "_">_<action>``
  (``manager.py`` ``build_service_name`` / ``_async_register_service``),
  with every variable required. The device registry carries only the
  friendly name, so the firmware publishes its node name in a diagnostic
  text sensor named :data:`NODE_SENSOR_NAME`. Social Home finds it in WS
  ``config/entity_registry/list``
  (``homeassistant/components/config/entity_registry.py``
  ``websocket_list_entities``; rows are ``RegistryEntry.as_partial_dict``
  in ``homeassistant/helpers/entity_registry.py``: ``entity_id``,
  ``device_id``, ``platform``, ``original_name``, ``disabled_by``…;
  ESPHome entities set ``has_entity_name`` so ``original_name`` is the
  firmware's entity name, unprefixed; an ESPHome text sensor is an HA
  ``sensor.*`` entity — ``esphome/sensor.py`` ``EsphomeTextSensor``) and
  reads its state with REST
  ``GET /api/states/{entity_id}`` (developers.home-assistant.io/docs/api/rest).
  ``unavailable`` / ``unknown`` mean HA has lost the panel.
* **Calling it.** REST ``POST /api/services/esphome/<action>`` with the
  variables as the JSON body (``homeassistant/components/api/__init__.py``
  ``APIDomainServicesView``: ``400`` for an unknown action, ``500`` when
  the call fails). Through Supervisor the same API is
  ``http://supervisor/core/api/…`` (developers.home-assistant.io/docs/apps/communication).
  HA fires ``call_service`` with the full service data and the recorder
  stores it (``homeassistant/core.py``, ``recorder/core.py``) — so the
  claim sent here is single-use and short-lived, and never logged.

Every failed HA call is logged at WARNING by the client; this module maps
"HA didn't answer" to :class:`DisplayDiscoveryFailedError`, never to an
empty list.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Any

from ...domain.household_dashboard import (
    DisplayDiscoveryFailedError,
    ExternalDisplayPanel,
    normalize_mac,
)

__all__ = [
    "CONNECT_ACTION",
    "ESPHOME_DOMAIN",
    "NODE_SENSOR_NAME",
    "PANEL_MANUFACTURER",
    "PANEL_MODEL",
    "HaDisplayDirectory",
    "connect_action_for",
    "panels_from_registries",
]

log = logging.getLogger(__name__)

#: ``esphome.project`` ``social-home-io.esphome-dashboard``, as HA splits it.
PANEL_MANUFACTURER = "social-home-io"
PANEL_MODEL = "esphome-dashboard"
#: HA's integration domain for ESPHome devices and their actions.
ESPHOME_DOMAIN = "esphome"
#: The firmware's ``api: actions:`` name (variables ``url``, ``claim``).
CONNECT_ACTION = "connect_social_home"
#: The firmware's diagnostic text sensor whose state is its node name.
NODE_SENSOR_NAME = "Social Home node"
#: States HA gives an entity it can't read.
_GONE_STATES = frozenset({"unavailable", "unknown"})
#: An ESPHome node name (``esphome: name:`` — lower-case letters, digits,
#: ``-``; ``_`` tolerated) — anything else never becomes an action name.
_NODE_NAME = re.compile(r"[a-z0-9_-]{1,63}")
_FALLBACK_NAME = "Wall panel"


def connect_action_for(node_name: object) -> str | None:
    """The HA action name for a node's connect action
    (``sh-dash-kitchen`` → ``sh_dash_kitchen_connect_social_home``);
    ``None`` for anything that isn't a node name."""
    if not isinstance(node_name, str) or _NODE_NAME.fullmatch(node_name) is None:
        return None
    return f"{node_name.replace('-', '_')}_{CONNECT_ACTION}"


def _is_panel(device: dict[str, Any]) -> bool:
    return (
        device.get("manufacturer") == PANEL_MANUFACTURER
        and device.get("model") == PANEL_MODEL
        and device.get("disabled_by") is None
        # ESPHome sub-devices (``devices:`` in the firmware) carry the same
        # manufacturer / model but hang off the main device (``manager.py``
        # ``_async_setup_device_registry``) — they aren't panels.
        and device.get("via_device_id") is None
        and isinstance(device.get("id"), str)
        and bool(device["id"])
    )


def _mac(device: dict[str, Any]) -> str | None:
    for connection in device.get("connections") or ():
        if (
            isinstance(connection, (list, tuple))
            and len(connection) == 2
            and connection[0] == "mac"
        ):
            mac = normalize_mac(connection[1])
            if mac is not None:
                return mac
    return None


def _name(device: dict[str, Any]) -> str:
    for key in ("name_by_user", "name"):
        value = device.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _FALLBACK_NAME


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def panels_from_registries(
    devices: list[Any], entities: list[Any]
) -> list[tuple[ExternalDisplayPanel, str | None]]:
    """The Social Home panels in HA's device registry, each with the
    ``entity_id`` of its :data:`NODE_SENSOR_NAME` sensor (``None`` when it
    has none, or it's disabled). Rows that aren't registry objects are
    ignored."""
    node_sensors: dict[str, str] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        device_id = entity.get("device_id")
        entity_id = entity.get("entity_id")
        if (
            entity.get("platform") == ESPHOME_DOMAIN
            and entity.get("original_name") == NODE_SENSOR_NAME
            and entity.get("disabled_by") is None
            and isinstance(device_id, str)
            and isinstance(entity_id, str)
            and entity_id.startswith("sensor.")
        ):
            node_sensors[device_id] = entity_id
    panels: list[tuple[ExternalDisplayPanel, str | None]] = []
    for device in devices:
        if not isinstance(device, dict) or not _is_panel(device):
            continue
        panel = ExternalDisplayPanel(
            id=device["id"],
            name=_name(device),
            mac=_mac(device),
            sw_version=_text(device.get("sw_version")),
        )
        panels.append((panel, node_sensors.get(device["id"])))
    return panels


class HaDisplayDirectory:
    """:class:`~socialhome.platform.adapter.DisplayDirectory` over Home
    Assistant's device registry and ESPHome actions.

    Holds the adapter (like the other HA providers) and reaches its
    ``_client`` lazily — the client exists only after ``on_startup``.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def _client(self) -> Any:
        try:
            return self._adapter._client
        except RuntimeError as exc:
            raise DisplayDiscoveryFailedError() from exc

    async def _registry(self, command: str) -> list[Any]:
        reply = await self._client().ws_command(command)
        result = reply.get("result") if reply is not None else None
        if not isinstance(result, list):
            # The client logged the transport / auth failure already.
            log.warning("wall panels: Home Assistant's %s didn't answer", command)
            raise DisplayDiscoveryFailedError()
        return result

    async def list_panels(self) -> list[ExternalDisplayPanel]:
        """Every Social Home panel adopted in Home Assistant. Raises
        :class:`DisplayDiscoveryFailedError` when a registry couldn't be
        read — never an empty list for a failed call."""
        devices = await self._registry("config/device_registry/list")
        found = [d for d in devices if isinstance(d, dict) and _is_panel(d)]
        if not found:
            return []
        entities = await self._registry("config/entity_registry/list")
        client = self._client()
        pairs = panels_from_registries(found, entities)
        states = iter(
            await asyncio.gather(
                *(client.get_state(s) for _, s in pairs if s is not None)
            )
        )
        panels: list[ExternalDisplayPanel] = []
        for panel, sensor in pairs:
            if sensor is None:
                panels.append(panel)
                continue
            state = next(states)
            value = state.get("state") if isinstance(state, dict) else None
            if value is None or value in _GONE_STATES:
                # Unavailable — or no state / a failed read (the client
                # logged it): HA can't reach the panel now. Never "old
                # firmware", which would send the admin the wrong way.
                panels.append(replace(panel, online=False))
                continue
            panels.append(replace(panel, connect_action=connect_action_for(value)))
        return panels

    async def connect(
        self, panel: ExternalDisplayPanel, *, url: str, claim: str
    ) -> None:
        """Call the panel's ``connect_social_home`` action with ``url`` and
        ``claim``. Raises :class:`DisplayDiscoveryFailedError` when HA
        refused or didn't answer (the client logs the HTTP status at
        WARNING — never the data, which carries the claim)."""
        action = panel.connect_action
        if action is None:
            raise DisplayDiscoveryFailedError()
        result = await self._client().call_service(
            ESPHOME_DOMAIN, action, {"url": url, "claim": claim}
        )
        if result is None:
            log.warning(
                "wall panels: Home Assistant didn't run %s.%s for panel %s"
                " — is it online in Home Assistant?",
                ESPHOME_DOMAIN,
                action,
                panel.id,
            )
            raise DisplayDiscoveryFailedError()
