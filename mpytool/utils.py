"""Utility functions for mpytool"""

import os as _os
import sys as _sys

from serial.tools.list_ports import comports as _comports

from mpytool.conn import ConnError as _ConnError


def setup_utf8_encoding():
    """Reconfigure stdout/stderr to UTF-8 on Windows.

    Windows subprocess/pipe uses cp1252 by default which can't handle
    Unicode characters (e.g. tree drawing chars). This ensures UTF-8
    is always used.
    """
    if _sys.platform == 'win32' and hasattr(_sys.stdout, 'reconfigure'):
        _sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        _sys.stderr.reconfigure(encoding='utf-8', errors='replace')


# Known USB Vendor IDs for MicroPython devices
# Native USB-CDC (direct USB connection to microcontroller)
VID_RASPBERRY_PI = 0x2E8A  # RP2040/RP2350 (Pico)
VID_ESPRESSIF = 0x303A     # ESP32-S2/S3/C3/C6 USB-JTAG-Serial
VID_MICROPYTHON = 0xF055   # Official MicroPython (pyboard, STM32)

# USB-UART bridge chips (external USB-to-serial converter)
VID_SILICON_LABS = 0x10C4  # CP210x
VID_QINHENG = 0x1A86       # CH340/CH341
VID_FTDI = 0x0403          # FT232/FT2232
VID_PROLIFIC = 0x067B      # PL2303

# Priority groups for port sorting (lower = higher priority)
_MICROPYTHON_VIDS = {VID_RASPBERRY_PI, VID_ESPRESSIF, VID_MICROPYTHON}
_USB_UART_VIDS = {VID_SILICON_LABS, VID_QINHENG, VID_FTDI, VID_PROLIFIC}


def is_remote_path(path: str) -> bool:
    """Check if path is remote (starts with :)"""
    return path.startswith(":")


def parse_remote_path(path: str) -> str:
    """Parse remote path, strip leading :

    Args:
        path: path with : prefix

    Returns:
        path without : prefix (empty string for ':' means CWD)
    """
    if not is_remote_path(path):
        raise ValueError(f"Not a remote path: {path}")
    return path[1:]


def split_commands(args: list[str], separator: str = "--") -> list[list[str]]:
    """Split command arguments by separator

    Args:
        args: list of arguments
        separator: separator string (default: --)

    Returns:
        list of command groups

    Example:
        split_commands(["cp", "a", ":/", "--", "reset"])
        => [["cp", "a", ":/"], ["reset"]]
    """
    if separator not in args:
        return [args] if args else []

    groups = []
    current = []
    for arg in args:
        if arg == separator:
            if current:
                groups.append(current)
                current = []
        else:
            current.append(arg)
    if current:
        groups.append(current)
    return groups


def format_size(size):
    """Format size in bytes to human readable format (like ls -h)"""
    if size < 1024:
        return f"{int(size)}B"
    for unit in ('K', 'M', 'G', 'T'):
        size /= 1024
        if size < 10:
            return f"{size:.2f}{unit}"
        if size < 100:
            return f"{size:.1f}{unit}"
        if size < 1024 or unit == 'T':
            return f"{size:.0f}{unit}"
    return f"{size:.0f}T"


def _port_sort_key(port_info):
    """Sort key for serial ports - prioritize known MicroPython VIDs"""
    vid = port_info.vid
    if vid is None:
        return (3, port_info.device)  # Unknown - lowest priority
    if vid in _MICROPYTHON_VIDS:
        return (0, port_info.device)  # Native MicroPython USB - highest
    if vid in _USB_UART_VIDS:
        return (1, port_info.device)  # USB-UART bridges - second
    return (2, port_info.device)      # Other USB devices - third


# Known VID:PID combinations for MicroPython devices
_VIDPID_NAMES = {
    # Raspberry Pi
    (VID_RASPBERRY_PI, 0x0003): "Raspberry Pi Pico",
    (VID_RASPBERRY_PI, 0x0005): "Raspberry Pi Pico W",
    (VID_RASPBERRY_PI, 0x000A): "Raspberry Pi Pico 2",
    (VID_RASPBERRY_PI, 0x000B): "Raspberry Pi Pico 2 W",
    (VID_RASPBERRY_PI, 0x000F): "Raspberry Pi RP2350",
    # Espressif (built-in USB-JTAG-Serial)
    (VID_ESPRESSIF, 0x0002): "Espressif ESP32-S2",
    # 0x1001 is the shared USB-Serial-JTAG block — identical descriptor across
    # all chips with built-in USB-JTAG (C3/C5/C6/H2/P4/...), so the exact chip
    # is indistinguishable via USB. Use `ports -i` to identify the real model.
    (VID_ESPRESSIF, 0x1001): "Espressif ESP32 (USB-JTAG/serial)",
    (VID_ESPRESSIF, 0x1002): "Espressif ESP32-S3",
    # MicroPython official (Pyboard)
    (VID_MICROPYTHON, 0x9800): "Pyboard",
    (VID_MICROPYTHON, 0x9801): "Pyboard (CDC)",
    (VID_MICROPYTHON, 0x9802): "Pyboard (CDC+MSC)",
}

# VID to manufacturer name fallback (when pyserial has no info)
_VID_NAMES = {
    VID_RASPBERRY_PI: "Raspberry Pi",
    VID_ESPRESSIF: "Espressif",
    VID_MICROPYTHON: "MicroPython",
    VID_SILICON_LABS: "Silicon Labs CP210x",
    VID_QINHENG: "QinHeng CH340",
    VID_FTDI: "FTDI",
    VID_PROLIFIC: "Prolific PL2303",
}


def _port_description(port_info):
    """Get human-readable description for a serial port"""
    # First try VID:PID lookup for known devices
    if port_info.vid and port_info.pid:
        name = _VIDPID_NAMES.get((port_info.vid, port_info.pid))
        if name:
            if port_info.serial_number:
                return f"{name} [{port_info.serial_number}]"
            return name
    # Then try pyserial info
    if port_info.product:
        if port_info.serial_number:
            return f"{port_info.product} [{port_info.serial_number}]"
        return port_info.product
    if port_info.manufacturer:
        return port_info.manufacturer
    # Fallback to VID name
    if port_info.vid:
        name = _VID_NAMES.get(port_info.vid)
        if name:
            return name
        return f"USB {port_info.vid:04X}:{port_info.pid:04X}"
    return ""


def _filter_sort_ports():
    """Filter and sort serial ports, return ListPortInfo objects"""
    ports = []
    for p in _comports():
        # Skip non-USB devices (Bluetooth, debug console, etc.)
        if p.vid is None:
            continue
        # macOS: prefer cu.* over tty.* (doesn't wait for DCD signal)
        if _sys.platform == "darwin" and p.device.startswith("/dev/tty."):
            continue
        ports.append(p)
    # Sort by priority (known MicroPython VIDs first)
    ports.sort(key=_port_sort_key)
    return ports


def detect_serial_ports_info() -> list[tuple[str, str]]:
    """Detect available serial ports with descriptions

    Returns:
        list of (device, description) tuples sorted by likelihood
        of being MicroPython device
    """
    return [(p.device, _port_description(p)) for p in _filter_sort_ports()]


def detect_serial_ports() -> list[str]:
    """Detect available serial ports for MicroPython devices

    Returns:
        list of port paths sorted by likelihood of being MicroPython device
        (known MicroPython VIDs first, then USB-UART bridges, then others)
    """
    return [p.device for p in _filter_sort_ports()]


def get_port_info(port: str):
    """Get USB info for a serial port

    Args:
        port: port path (e.g. /dev/ttyACM0, COM3, /dev/serial/by-id/...)

    Returns:
        ListPortInfo object with vid, pid, manufacturer, product, etc.
        or None if port not found
    """
    # Resolve symlinks (e.g. /dev/serial/by-id/... -> /dev/ttyUSB0)
    try:
        resolved = _os.path.realpath(port)
    except OSError:
        resolved = port
    for p in _comports():
        if p.device == port or p.device == resolved:
            return p
    return None


# Recognized keys of a TLS configuration dict. Unknown keys are rejected
# rather than ignored - a typo like {'insecrue': True} would otherwise
# silently keep verification enabled.
_TLS_KEYS = frozenset({
    'profile', 'cert', 'key', 'ca', 'insecure', 'check_hostname',
    'key_password'})

# Fixed file names inside a certificate profile directory
_PROFILE_CERT = 'cert.pem'
_PROFILE_KEY = 'key.pem'
_PROFILE_CA = 'ca.pem'


def config_dir() -> str:
    """Get mpytool configuration directory (not created, may not exist)

    Order: $MPYTOOL_CONFIG_DIR, $XDG_CONFIG_HOME/mpytool,
    %APPDATA%\\mpytool (Windows), ~/.config/mpytool
    """
    env_dir = _os.environ.get('MPYTOOL_CONFIG_DIR')
    if env_dir:
        return env_dir
    xdg_dir = _os.environ.get('XDG_CONFIG_HOME')
    if xdg_dir:
        return _os.path.join(xdg_dir, 'mpytool')
    if _sys.platform == 'win32':
        app_data = _os.environ.get('APPDATA')
        if app_data:
            return _os.path.join(app_data, 'mpytool')
    return _os.path.expanduser('~/.config/mpytool')


def certs_dir() -> str:
    """Get certificate store directory (not created, may not exist)"""
    return _os.path.join(config_dir(), 'certs')


def list_cert_profiles() -> list[str]:
    """List TLS profile names available in the certificate store

    Returns:
        sorted list of names (profile directories and .pem bundles)
    """
    store = certs_dir()
    try:
        entries = _os.listdir(store)
    except OSError:
        return []
    names = set()
    for entry in entries:
        if _os.path.isdir(_os.path.join(store, entry)):
            names.add(entry)
        elif entry.endswith('.pem'):
            names.add(entry[:-len('.pem')])
    return sorted(names)


def _resolve_profile_dir(path: str, name: str):
    """Resolve a profile directory to (cert, key, ca) paths"""
    cert = _os.path.join(path, _PROFILE_CERT)
    if not _os.path.isfile(cert):
        raise _ConnError(
            f"TLS profile '{name}' has no {_PROFILE_CERT} in {path}")
    key = _os.path.join(path, _PROFILE_KEY)
    ca = _os.path.join(path, _PROFILE_CA)
    return (
        cert,
        key if _os.path.isfile(key) else None,
        ca if _os.path.isfile(ca) else None)


def resolve_cert_profile(name: str):
    """Resolve a TLS profile name to certificate paths

    A name is looked up in the certificate store, first as a directory
    holding cert.pem / key.pem / ca.pem, then as a combined name.pem
    bundle. A name containing a path separator is used as a direct path
    to such a directory or bundle instead.

    Arguments:
        name: profile name or path

    Returns:
        (cert, key, ca) tuple, key and ca may be None (key None means the
        private key is expected inside the certificate file)
    """
    is_path = _os.sep in name or (_os.altsep and _os.altsep in name)
    if is_path:
        path = _os.path.expanduser(name)
        if _os.path.isdir(path):
            return _resolve_profile_dir(path, name)
        if _os.path.isfile(path):
            return path, None, None
        raise _ConnError(f"TLS profile not found: {name}")
    store = certs_dir()
    profile_dir = _os.path.join(store, name)
    if _os.path.isdir(profile_dir):
        return _resolve_profile_dir(profile_dir, name)
    bundle = _os.path.join(store, f'{name}.pem')
    if _os.path.isfile(bundle):
        return bundle, None, None
    available = list_cert_profiles()
    hint = ', '.join(available) if available else 'none'
    raise _ConnError(
        f"TLS profile '{name}' not found in {store} (available: {hint})")


def _check_key_permissions(path: str, insecure: bool, log=None):
    """Reject a private key readable by group or others

    Mirrors ssh: a key file other users can read is refused outright, so a
    wrong chmod cannot go unnoticed. Skipped on Windows, where the mode
    bits carry no meaning.
    """
    if _sys.platform == 'win32':
        return
    try:
        mode = _os.stat(path).st_mode & 0o777
    except OSError:
        return  # missing/unreadable file is reported later by load_cert_chain
    if not mode & 0o077:
        return
    message = (
        f"private key {path} is accessible by other users ({mode:04o}), "
        f"run: chmod 600 {path}")
    if insecure:
        if log:
            log.warning(message)
        return
    raise _ConnError(f"{message} (or use --tls-insecure)")


def resolve_tls(tls, log=None) -> dict:
    """Normalize a TLS configuration dict

    Expands a 'profile' name into cert / key / ca paths from the
    certificate store and validates private key permissions. Explicitly
    given paths always win over the ones coming from a profile.

    Arguments:
        tls: True for defaults, or a dict with any of: profile, cert, key,
            ca, insecure, check_hostname, key_password
        log: logger for warnings

    Returns:
        new dict without the 'profile' key (input is not modified)
    """
    if tls is True:
        tls = {}
    if not isinstance(tls, dict):
        raise ValueError(
            f"tls must be a dict or True, not {type(tls).__name__}")
    unknown = set(tls) - _TLS_KEYS
    if unknown:
        raise ValueError(
            f"unknown tls option(s): {', '.join(sorted(unknown))} "
            f"(expected: {', '.join(sorted(_TLS_KEYS))})")
    tls = dict(tls)
    profile = tls.pop('profile', None)
    if profile:
        cert, key, ca = resolve_cert_profile(profile)
        tls.setdefault('cert', cert)
        if key:
            tls.setdefault('key', key)
        if ca:
            tls.setdefault('ca', ca)
    # Without a separate key file the private key lives in the cert file
    key_file = tls.get('key') or tls.get('cert')
    if key_file:
        _check_key_permissions(key_file, tls.get('insecure', False), log)
    return tls
