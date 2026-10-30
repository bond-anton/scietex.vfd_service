"""Tests for the VFD service configuration models and loader."""

from pathlib import Path

import msgspec
import pytest

from scietex.vfd_service.config import (
    VFD_CONFIG_FILE,
    VFD_CONFIG_SUBDIR,
    VFD_SETTINGS_DEFAULTS,
    ModbusGatewaySettings,
    VFDServiceSettings,
    VFDSettings,
    read_vfd_config,
)


def _config_path(conf_dir: Path) -> Path:
    """The namespaced path the loader reads and writes."""
    return conf_dir / VFD_CONFIG_SUBDIR / VFD_CONFIG_FILE


def test_yaml_roundtrip_preserves_drive_list() -> None:
    """Drive entries survive a YAML round-trip preserving every field."""
    settings = VFDServiceSettings(
        drives=[
            VFDSettings(
                label="pump",
                driver="intek_spe_b_modbus",
                unit_id=7,
                poll_interval=0.5,
                max_frequency_hz=60.0,
            )
        ]
    )

    decoded = msgspec.yaml.decode(msgspec.yaml.encode(settings), type=VFDServiceSettings)

    assert decoded.drives[0].label == "pump"
    assert decoded.drives[0].driver == "intek_spe_b_modbus"
    assert decoded.drives[0].unit_id == 7
    assert decoded.drives[0].poll_interval == 0.5
    assert decoded.drives[0].max_frequency_hz == 60.0


def test_defaults_constant_is_l0_base() -> None:
    """The exported L0 base is a concrete default struct instance."""
    assert isinstance(VFD_SETTINGS_DEFAULTS, VFDServiceSettings)
    assert VFD_SETTINGS_DEFAULTS == VFDServiceSettings()


def test_gateway_defaults() -> None:
    """ModbusGatewaySettings defaults to the local gateway endpoint."""
    gateway = ModbusGatewaySettings()
    assert gateway.host == "127.0.0.1"
    assert gateway.port == 502
    assert gateway.timeout == 1.0


def test_drive_defaults() -> None:
    """A drive entry defaults to unit id 1, a 1 s poll and no maximum override."""
    drive = VFDSettings(label="pump", driver="intek_spe_b_modbus")
    assert drive.unit_id == 1
    assert drive.poll_interval == 1.0
    assert drive.max_frequency_hz is None


def test_service_defaults() -> None:
    """The service defaults to no drives, a 3-failure threshold and 10000 entries."""
    settings = VFDServiceSettings()
    assert settings.drives == []
    assert settings.failure_threshold == 3
    assert settings.stream_maxlen == 10000


def test_unknown_field_rejected() -> None:
    """``forbid_unknown_fields`` rejects a typo instead of silently ignoring it."""
    with pytest.raises(msgspec.ValidationError):
        msgspec.convert({"drives": [{"label": "a", "driver": "d", "bogus": 1}]}, VFDServiceSettings)


def test_loader_creates_default_file(tmp_path: Path) -> None:
    """A missing file is created with the default settings and returned as a patch."""
    patch = read_vfd_config(tmp_path)

    assert _config_path(tmp_path).exists()
    assert patch == msgspec.to_builtins(VFDServiceSettings())


def test_loader_reads_existing_file(tmp_path: Path) -> None:
    """An existing file is parsed and returned as a raw mapping."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("drives:\n  - label: pump\n    driver: intek_spe_b_modbus\n")

    patch = read_vfd_config(tmp_path)

    assert patch["drives"] == [{"label": "pump", "driver": "intek_spe_b_modbus"}]


def test_loader_missing_file_without_create_raises(tmp_path: Path) -> None:
    """``create_default=False`` on a missing file raises instead of writing one."""
    with pytest.raises(RuntimeError, match="does not exist"):
        read_vfd_config(tmp_path, create_default=False)


def test_loader_unparseable_file_raises(tmp_path: Path) -> None:
    """An unparseable file raises RuntimeError and is left untouched."""
    path = _config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("unterminated: [flow\n")

    with pytest.raises(RuntimeError, match="Failed to parse"):
        read_vfd_config(tmp_path)

    assert path.read_text() == "unterminated: [flow\n"


def test_loader_none_conf_dir_raises() -> None:
    """A ``None`` config dir raises rather than resolving to the cwd."""
    with pytest.raises(RuntimeError, match="was not set"):
        read_vfd_config(None)


def test_loader_non_directory_raises(tmp_path: Path) -> None:
    """A service subdir that is a file, not a directory, raises."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / VFD_CONFIG_SUBDIR).write_text("not a directory")

    with pytest.raises(RuntimeError, match="not a directory"):
        read_vfd_config(conf_dir)
