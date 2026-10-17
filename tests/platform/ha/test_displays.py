"""Tests for socialhome.platform.ha.displays — HA's ESPHome wall panels.

The fixtures are shaped exactly like Home Assistant 2026.10.1 answers:

* device rows = ``DeviceEntry.dict_repr``
  (``homeassistant/helpers/device_registry.py``) as WS
  ``config/device_registry/list`` sends them, filled the way
  ``homeassistant/components/esphome/manager.py``
  ``_async_setup_device_registry`` fills them for a firmware whose
  ``esphome.project`` is ``social-home-io.esphome-dashboard``;
* entity rows = ``RegistryEntry.as_partial_dict``
  (``homeassistant/helpers/entity_registry.py``) as WS
  ``config/entity_registry/list`` sends them;
* states = REST ``GET /api/states/{entity_id}`` objects.

The HA client is replaced at the boundary by a recording double.
"""

from __future__ import annotations

import json
import logging

import pytest

from socialhome.domain.household_dashboard import (
    DisplayDiscoveryFailedError,
    ExternalDisplayPanel,
)
from socialhome.platform.adapter import DisplayDirectory
from socialhome.platform.ha.displays import (
    KEY_SENSOR_NAME,
    NODE_SENSOR_NAME,
    HaDisplayDirectory,
    connect_action_for,
    panels_from_registries,
)

KITCHEN = "4f1c0e8a9b2d4c6e8f0a1b2c3d4e5f60"
HALL = "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b"


def _device(device_id: str, **over) -> dict:
    """A ``config/device_registry/list`` row for an ESPHome panel."""
    row = {
        "area_id": None,
        "configuration_url": None,
        "config_entries": ["01JABCDEFGHJKMNPQRSTVWXYZ0"],
        "config_entries_subentries": {"01JABCDEFGHJKMNPQRSTVWXYZ0": [None]},
        "config_entry_id": "01JABCDEFGHJKMNPQRSTVWXYZ0",
        "config_subentry_id": None,
        "connections": [["mac", "a0:b7:65:12:34:56"]],
        "created_at": 1_760_000_000.0,
        "disabled_by": None,
        "entry_type": None,
        "hw_version": None,
        "id": device_id,
        "identifiers": [],
        "labels": [],
        "manufacturer": "social-home-io",
        "model": "esphome-dashboard",
        "model_id": None,
        "modified_at": 1_760_000_000.0,
        "name_by_user": None,
        "name": "Social Home Dashboard",
        "next_name_part": None,
        "parent_device_id": None,
        "primary_config_entry": "01JABCDEFGHJKMNPQRSTVWXYZ0",
        "serial_number": None,
        "sw_version": "2026.10.13 (ESPHome 2026.9.1)",
        "via_device_id": None,
    }
    row.update(over)
    return row


def _entity(device_id: str, entity_id: str, original_name: str, **over) -> dict:
    """A ``config/entity_registry/list`` row for an ESPHome entity."""
    row = {
        "area_id": None,
        "categories": {},
        "config_entry_id": "01JABCDEFGHJKMNPQRSTVWXYZ0",
        "config_subentry_id": None,
        "created_at": 1_760_000_000.0,
        "device_id": device_id,
        "disabled_by": None,
        "entity_category": "diagnostic",
        "entity_id": entity_id,
        "has_entity_name": True,
        "hidden_by": None,
        "icon": None,
        "id": f"reg-{entity_id}",
        "labels": [],
        "modified_at": 1_760_000_000.0,
        "name": None,
        "next_name_part": None,
        "options": {},
        "original_name": original_name,
        "platform": "esphome",
        "translation_key": None,
        "unique_id": f"A0B765123456-text_sensor-{original_name}",
    }
    row.update(over)
    return row


def _state(entity_id: str, state: str) -> dict:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {"friendly_name": "Social Home Dashboard Social Home node"},
        "last_changed": "2026-10-13T08:00:00+00:00",
        "last_updated": "2026-10-13T08:00:00+00:00",
    }


KITCHEN_DEVICE = _device(KITCHEN, name_by_user="Kitchen")
HALL_DEVICE = _device(
    HALL, name="Hall panel", connections=[["mac", "A0:B7:65:00:00:02"]]
)
OTHER_DEVICE = _device(
    "0000000000000000000000000000beef",
    manufacturer="espressif",
    model="esp32-s3-box-3",
    name="Voice",
)
KITCHEN_NODE = "sensor.kitchen_social_home_node"
HALL_NODE = "sensor.hall_panel_social_home_node"
KITCHEN_KEY = "sensor.kitchen_social_home_key"
HALL_KEY = "sensor.hall_panel_social_home_key"
#: Two panels' X25519 public keys as their ``Social Home key`` sensors
#: publish them (43-character base64url).
KITCHEN_PUB = "UWBHFos4Xvjpuc3ZQbHdy6Qqt9vdxwxbykT_x9MjFH0"
HALL_PUB = "KIqQOwZFquk4ySsZiWu5niDasjjTL_cmMR3osnf1rQk"
ENTITIES = [
    _entity(KITCHEN, KITCHEN_NODE, "Social Home node"),
    _entity(KITCHEN, "sensor.kitchen_social_home_status", "Social Home status"),
    _entity(KITCHEN, KITCHEN_KEY, "Social Home key"),
    _entity(HALL, HALL_NODE, "Social Home node"),
    _entity(HALL, HALL_KEY, "Social Home key"),
]


class _Client:
    """The bits of :class:`HaClient` the directory uses."""

    def __init__(self) -> None:
        self.ws: dict[str, list | None] = {
            "config/device_registry/list": [OTHER_DEVICE, KITCHEN_DEVICE, HALL_DEVICE],
            "config/entity_registry/list": ENTITIES,
        }
        self.states: dict[str, dict] = {
            KITCHEN_NODE: _state(KITCHEN_NODE, "sh-dash-kitchen"),
            HALL_NODE: _state(HALL_NODE, "hall"),
            KITCHEN_KEY: _state(KITCHEN_KEY, KITCHEN_PUB),
            HALL_KEY: _state(HALL_KEY, HALL_PUB),
        }
        self.state_reads: list[str] = []
        self.ws_calls: list[str] = []
        self.service_calls: list[tuple[str, str, dict]] = []
        self.service_reply: object = []

    async def ws_command(self, type_: str, **fields):
        self.ws_calls.append(type_)
        result = self.ws.get(type_)
        return None if result is None else {"result": result}

    async def get_state(self, entity_id: str):
        self.state_reads.append(entity_id)
        return self.states.get(entity_id)

    async def call_service(self, domain, service, data=None, *, return_response=False):
        self.service_calls.append((domain, service, data))
        return self.service_reply


class _Adapter:
    def __init__(self, client: _Client | None) -> None:
        self.client = client

    @property
    def _client(self) -> _Client:
        if self.client is None:
            raise RuntimeError("HA client not initialised; call on_startup() first")
        return self.client


@pytest.fixture
def client() -> _Client:
    return _Client()


@pytest.fixture
def directory(client) -> HaDisplayDirectory:
    return HaDisplayDirectory(_Adapter(client))


def test_satisfies_the_protocol(directory):
    assert isinstance(directory, DisplayDirectory)


def test_sensor_names_match_the_firmware():
    """The firmware's ``text_sensor`` names (``common/api.yaml``) — HA's
    ``original_name`` for them, unprefixed."""
    assert NODE_SENSOR_NAME == "Social Home node"
    assert KEY_SENSOR_NAME == "Social Home key"


@pytest.mark.parametrize(
    ("node", "action"),
    [
        # ``manager.py`` ``build_service_name``: ``-`` → ``_``.
        ("sh-dash-kitchen", "sh_dash_kitchen_connect_social_home"),
        ("socialhome-dashboard", "socialhome_dashboard_connect_social_home"),
        ("panel_2", "panel_2_connect_social_home"),
        ("Kitchen Panel", None),
        ("../x", None),
        ("", None),
        ("a" * 64, None),
        (None, None),
    ],
)
def test_connect_action_for(node, action):
    assert connect_action_for(node) == action


async def test_lists_only_social_home_panels(directory, client):
    panels = await directory.list_panels()
    assert panels == [
        ExternalDisplayPanel(
            id=KITCHEN,
            name="Kitchen",  # the user's rename wins
            mac="a0:b7:65:12:34:56",
            sw_version="2026.10.13 (ESPHome 2026.9.1)",
            connect_action="sh_dash_kitchen_connect_social_home",
            panel_key=KITCHEN_PUB,
        ),
        ExternalDisplayPanel(
            id=HALL,
            name="Hall panel",
            mac="a0:b7:65:00:00:02",
            sw_version="2026.10.13 (ESPHome 2026.9.1)",
            connect_action="hall_connect_social_home",
            panel_key=HALL_PUB,
        ),
    ]
    assert client.ws_calls == [
        "config/device_registry/list",
        "config/entity_registry/list",
    ]
    # Each sensor read once.
    assert sorted(client.state_reads) == sorted(
        [KITCHEN_NODE, KITCHEN_KEY, HALL_NODE, HALL_KEY]
    )


async def test_panel_without_a_key_sensor_needs_an_update(directory, client):
    """Firmware from before the sealed connect (no ``Social Home key``
    sensor, or it is disabled): listed, online, but not connectable."""
    for entities in (
        [e for e in ENTITIES if e["entity_id"] != KITCHEN_KEY],
        [
            _entity(KITCHEN, KITCHEN_KEY, "Social Home key", disabled_by="user")
            if e["entity_id"] == KITCHEN_KEY
            else e
            for e in ENTITIES
        ],
    ):
        client.ws["config/entity_registry/list"] = entities
        kitchen, hall = await directory.list_panels()
        assert kitchen.online and kitchen.panel_key is None
        assert kitchen.connect_action == "sh_dash_kitchen_connect_social_home"
        assert hall.panel_key == HALL_PUB


@pytest.mark.parametrize(
    "value",
    [
        "",
        KITCHEN_PUB[:42],
        KITCHEN_PUB + "=",
        KITCHEN_PUB[:42] + "B",  # non-canonical last character
        "not a key at all, but 43 characters long!!",
    ],
)
async def test_a_malformed_key_is_no_key(directory, client, value):
    client.states[KITCHEN_KEY] = _state(KITCHEN_KEY, value)
    kitchen = (await directory.list_panels())[0]
    assert kitchen.online and kitchen.panel_key is None


@pytest.mark.parametrize("gone", ["unavailable", "unknown", None])
async def test_a_key_sensor_that_cant_be_read_is_offline(directory, client, gone):
    if gone is None:
        del client.states[KITCHEN_KEY]
    else:
        client.states[KITCHEN_KEY] = _state(KITCHEN_KEY, gone)
    kitchen = (await directory.list_panels())[0]
    assert kitchen.online is False and kitchen.panel_key is None


async def test_no_panels_skips_the_entity_registry(directory, client):
    client.ws["config/device_registry/list"] = [OTHER_DEVICE]
    assert await directory.list_panels() == []
    assert client.ws_calls == ["config/device_registry/list"]


async def test_disabled_and_malformed_devices_are_skipped(directory, client):
    client.ws["config/device_registry/list"] = [
        _device(KITCHEN, disabled_by="user"),
        _device("", name="no id"),
        "not a row",
        HALL_DEVICE,
    ]
    assert [p.id for p in await directory.list_panels()] == [HALL]


async def test_a_state_that_cant_be_read_is_offline_not_old_firmware(directory, client):
    """No state (404) or a failed read (``get_state`` → ``None``): Home
    Assistant can't reach the panel now — saying "update the firmware"
    would send the admin the wrong way."""
    del client.states[KITCHEN_NODE]
    kitchen, hall = await directory.list_panels()
    assert kitchen.online is False and kitchen.connect_action is None
    assert hall.connect_action == "hall_connect_social_home"


async def test_esphome_sub_devices_are_not_panels(directory, client):
    """``manager.py`` gives sub-devices the main device's manufacturer /
    model, identifiers instead of a MAC, and ``via_device_id``."""
    sub = _device(
        "5" * 32,
        connections=[],
        identifiers=[["esphome", "a0b765123456_1"]],
        via_device_id=KITCHEN,
        name="Kitchen relay",
    )
    client.ws["config/device_registry/list"] = [KITCHEN_DEVICE, sub]
    assert [p.id for p in await directory.list_panels()] == [KITCHEN]


async def test_offline_panel(directory, client):
    for gone in ("unavailable", "unknown"):
        client.states[KITCHEN_NODE] = _state(KITCHEN_NODE, gone)
        kitchen = (await directory.list_panels())[0]
        assert kitchen.online is False and kitchen.connect_action is None


async def test_panel_without_a_usable_node_sensor_cannot_connect(directory, client):
    """Older firmware (no sensor), a disabled sensor, a sensor with no
    state, or a state that isn't a node name → no action, still listed."""
    cases = [
        [e for e in ENTITIES if e["entity_id"] != KITCHEN_NODE],
        [
            _entity(KITCHEN, KITCHEN_NODE, "Social Home node", disabled_by="user"),
            *ENTITIES[1:],
        ],
        [
            _entity(KITCHEN, KITCHEN_NODE, "Social Home node", platform="mqtt"),
            *ENTITIES[1:],
        ],
    ]
    for entities in cases:
        client.ws["config/entity_registry/list"] = entities
        kitchen = (await directory.list_panels())[0]
        assert kitchen.connect_action is None and kitchen.online
    client.ws["config/entity_registry/list"] = ENTITIES
    client.states[KITCHEN_NODE] = _state(KITCHEN_NODE, "Kitchen; drop")
    assert (await directory.list_panels())[0].connect_action is None


def test_panels_from_registries_mac_and_name_fallbacks():
    devices = [
        _device(KITCHEN, connections=[["zigbee", "x"], ["mac", "A0B765123456"]]),
        _device(HALL, connections=[], name="  ", name_by_user=None),
    ]
    (kitchen, k_node, k_key), (hall, h_node, h_key) = panels_from_registries(
        devices, []
    )
    assert kitchen.mac == "a0:b7:65:12:34:56" and k_node is None and k_key is None
    assert hall.mac is None and hall.name == "Wall panel"
    assert h_node is None and h_key is None


def test_panels_from_registries_pairs_each_device_with_its_sensors():
    (kitchen, k_node, k_key), (hall, h_node, h_key) = panels_from_registries(
        [KITCHEN_DEVICE, HALL_DEVICE, "not a row"], [*ENTITIES, "not a row"]
    )
    assert (kitchen.id, k_node, k_key) == (KITCHEN, KITCHEN_NODE, KITCHEN_KEY)
    assert (hall.id, h_node, h_key) == (HALL, HALL_NODE, HALL_KEY)


@pytest.mark.parametrize(
    "command", ["config/device_registry/list", "config/entity_registry/list"]
)
async def test_a_registry_that_doesnt_answer_is_a_failure(
    directory, client, command, caplog
):
    client.ws[command] = None
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(DisplayDiscoveryFailedError),
    ):
        await directory.list_panels()
    assert command in caplog.text


async def test_a_client_not_started_is_a_failure():
    directory = HaDisplayDirectory(_Adapter(None))
    with pytest.raises(DisplayDiscoveryFailedError):
        await directory.list_panels()


SEALED = json.dumps(
    {"ct": "Y3Q", "epk": KITCHEN_PUB, "nonce": "AAAAAAAAAAAAAAAA", "suite": "s"}
)


async def test_connect_calls_the_panels_esphome_action(directory, client):
    """One variable, ``sealed`` — never the claim or the address."""
    kitchen = (await directory.list_panels())[0]
    await directory.connect(kitchen, sealed=SEALED)
    assert client.service_calls == [
        ("esphome", "sh_dash_kitchen_connect_social_home", {"sealed": SEALED})
    ]


async def test_connect_failure_never_logs_the_sealed_value(directory, client, caplog):
    kitchen = (await directory.list_panels())[0]
    client.service_reply = None
    with caplog.at_level(logging.DEBUG), pytest.raises(DisplayDiscoveryFailedError):
        await directory.connect(kitchen, sealed=SEALED)
    assert SEALED not in caplog.text and "Y3Q" not in caplog.text
    assert "sh_dash_kitchen_connect_social_home" in caplog.text


async def test_connect_without_an_action_is_a_failure(directory, client):
    panel = ExternalDisplayPanel(id=KITCHEN, name="K", mac=None)
    with pytest.raises(DisplayDiscoveryFailedError):
        await directory.connect(panel, sealed=SEALED)
    assert client.service_calls == []
