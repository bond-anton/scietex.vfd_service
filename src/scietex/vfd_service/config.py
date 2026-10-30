"""Configuration models and YAML loader for the VFD service.

`VFDServiceSettings` is the concrete constructor (L0) base and the validation
target for the service's remote configuration. The drive list is a field-level
map merged onto L0 and overlaid by the remote `vfd` section (L3).

This module is deliberately **not** registered as an L1 bootstrap provider. The
worker reads `vfd.yml` directly at startup (before `super().initialize()`) and
uses it only to seed the initial in-memory drive list; the config section is
registered with `bootstrap=None`, so L1 never enters the layer stack and the
remote L3 config cleanly replaces it. Trade-off: `config:show` will not display
it as a layer.

The structs are kept deliberately thin: the drive ``driver`` is an entry point
name resolved at runtime (e.g. ``intek_spe_b``), so the YAML schema only
constrains types and unknown fields.
"""

from pathlib import Path

import msgspec

#: Remote-config section name the settings are registered under.
VFD_SECTION: str = "vfd"

#: Subdirectory under the shared config dir that namespaces this service's
#: files. The service-owned ``vfd.yml`` lives here, so services sharing one
#: config dir cannot collide.
VFD_CONFIG_SUBDIR: str = "vfd"

#: Filename of the service-owned YAML config file in the config subdirectory.
VFD_CONFIG_FILE: str = "vfd.yml"


class ModbusGatewaySettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Modbus/TCP gateway connection settings.

    The service drives VFDs through the Scietex Modbus gateway, so only the TCP
    endpoint and the transaction timeout are configurable here.
    """

    host: str = "127.0.0.1"
    port: int = 502
    timeout: float = 1.0


class VFDSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Settings for a single variable frequency drive driven by the service.

    ``label`` and ``driver`` are required. ``driver`` is the entry point name of
    a `scietex.hal.vfd` driver, resolved at runtime, e.g. ``intek_spe_b``.

    ``max_frequency_hz`` is optional and only meaningful for drives whose
    protocol does not expose their configured maximum output frequency: the
    driver uses it to derive ``frequency_percent`` and to bound
    ``set_frequency``. ``None`` leaves the driver's own default in place.
    """

    label: str
    driver: str
    unit_id: int = 1
    poll_interval: float = 1.0
    max_frequency_hz: float | None = None


class VFDServiceSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Top-level VFD service settings.

    ``failure_threshold`` and ``stream_maxlen`` are service-wide rather than
    per-drive: every drive on the bus shares the same RS485 segment, so the
    noise tolerance is a property of the segment, and the readings stream is a
    retention concern that does not vary by drive.
    """

    gateway: ModbusGatewaySettings = ModbusGatewaySettings()
    drives: list[VFDSettings] = msgspec.field(default_factory=list)
    #: Consecutive failed reads before a drive is reported as disconnected.
    #: One failed read is common on an RS485 segment, so a single glitch must
    #: not flip the status icon.
    failure_threshold: int = 3
    #: Entries retained per drive's readings stream.
    stream_maxlen: int = 10000


#: Concrete L0 base for the service's configuration merge. A field absent from
#: every layer, or explicitly cleared with ``null``, resolves to the value here.
VFD_SETTINGS_DEFAULTS: VFDServiceSettings = VFDServiceSettings()


def read_vfd_config(conf_dir: Path | None, *, create_default: bool = True) -> dict[str, object]:
    """Read the service config from ``vfd/vfd.yml`` under the config directory.

    The service's files are namespaced in a ``vfd/`` subdirectory so they do not
    collide with other services sharing the framework's single config dir. The
    file (and, when missing, its directory) is only created when
    ``create_default=True`` (the bootstrap path). A ``None`` or non-directory
    ``conf_dir``, a missing file/directory with ``create_default=False``, or an
    unparseable file each raise `RuntimeError`. An existing-but-invalid file is
    left untouched regardless of ``create_default``.

    The return value is the raw ``vfd.yml`` mapping (an L1-style patch). When
    the file is just created, the builtins of the default struct are returned so
    the patch is equivalent to an empty override.

    Args:
        conf_dir: Path to the configuration directory.
        create_default: Whether to create the directory and write a default
            ``vfd.yml`` when missing. Default ``True``.

    Returns:
        The parsed ``vfd.yml`` mapping as a dict, or the builtins of the default
        `VFDServiceSettings` when the file was just created.

    Raises:
        RuntimeError: If ``conf_dir`` is ``None`` or not a directory, the file
            is missing with ``create_default=False``, or the file cannot be
            parsed.
    """
    if not isinstance(conf_dir, Path):
        raise RuntimeError("Configuration dir was not set!")
    service_dir = conf_dir / VFD_CONFIG_SUBDIR
    if not service_dir.exists():
        if create_default:
            try:
                service_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                raise RuntimeError(f"Failed to create configuration directory {service_dir}!") from exc
        else:
            raise RuntimeError(
                f"Configuration directory {service_dir} does not exist and create_default=False (no default generated)."
            )
    elif not service_dir.is_dir():
        raise RuntimeError(f"Provided configuration directory path {service_dir} is not a directory!")
    vfd_yml = service_dir.joinpath(VFD_CONFIG_FILE)
    if not vfd_yml.exists():
        if create_default:
            settings = VFDServiceSettings()
            with open(vfd_yml, "wb") as f:
                f.write(msgspec.yaml.encode(settings))
            return msgspec.to_builtins(settings)
        raise RuntimeError(
            f"VFD configuration file {vfd_yml} does not exist and create_default=False "
            "(pass create_default=True to generate defaults)."
        )
    try:
        with open(vfd_yml, "rb") as f:
            patch = msgspec.yaml.decode(f.read(), type=dict)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse VFD configuration file {vfd_yml}. Fix the file or remove it to regenerate defaults."
        ) from exc
    return patch
