"""Who may reach the gateway: the private network only, the paired phone only.

- The phone's listener binds to this Mac's Tailscale address, or to loopback for tests.
  Any other address is refused, and there's no fallback to all interfaces.
- The phone and the gateway prove to each other that they hold the same 256-bit token.
  The token never crosses the connection after pairing.
- Pairing takes a short-lived code, read off the Mac's screen and typed into the phone. The phone
  also hands over the public half of a key it made in its own secure hardware, which signs only
  after a fingerprint. A pairing lasts PAIRING_DAYS.
- The token lets the phone see the list of sessions, and nothing else. Talking to a session takes a
  fingerprint at every Start: the phone signs that choice, and both the gateway and the session's
  mod check the signature against the phone's public key.
- The mod's socket lives in a directory only this user can enter.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import secrets
import stat
import subprocess
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

TAILNET = ipaddress.ip_network("100.64.0.0/10")
PAIR_SECONDS = 120
PAIR_TRIES = 3
FAILURE_LIMIT = 5       # failed logins in FAILURE_WINDOW close the door for FAILURE_LOCKOUT
FAILURE_WINDOW = 60
FAILURE_LOCKOUT = 60
PAIRING_DAYS = 30       # then the phone pairs again, at the Mac
PHONE_FILE = "phone.json"
PHONE_KEY_FILE = "phone.pub"  # the phone's public key, which the mod reads to check a choice


class SecurityError(Exception):
    pass


def default_state_dir():
    return os.environ.get("WALKIE_TALKIE_STATE") or os.path.expanduser("~/.local/state/walkie-talkie")


def connect_config_path():
    """Which logins may connect, and which folders never may. The mod reads it from this one place."""
    return os.path.expanduser("~/.config/walkie-talkie/connect.json")


def private_dir(path):
    """Create path as this user's private directory, or refuse one that isn't."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SecurityError("%s must be a directory owned by you, not a link." % path)
    os.chmod(path, 0o700)
    return path


def is_loopback(address):
    return ipaddress.ip_address(address).is_loopback


def is_tailnet(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.version == 4 and ip in TAILNET


def tailscale_address():
    """This Mac's Tailscale IPv4 address. Reads it only: `tailscale ip` changes nothing."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SecurityError("Can't read the Tailscale address: %s" % e) from e
    address = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    if out.returncode != 0 or not address or not is_tailnet(address):
        raise SecurityError("Tailscale has no address here. Connect this Mac to the tailnet first.")
    return address


def check_bind(address):
    """Refuse any listening address but the tailnet's or loopback."""
    try:
        if is_tailnet(address) or is_loopback(address):
            return address
    except ValueError:
        pass
    raise SecurityError("Refusing to listen on %s: only a Tailscale or loopback address is allowed." % address)


def phone_mac(token, server_nonce, client_nonce):
    return _mac(token, "phone", server_nonce, client_nonce)


def gateway_mac(token, server_nonce, client_nonce):
    return _mac(token, "gateway", server_nonce, client_nonce)


def _mac(token, role, server_nonce, client_nonce):
    message = "|".join((role, server_nonce, client_nonce)).encode()
    return hmac.new(bytes.fromhex(token), message, hashlib.sha256).hexdigest()


def new_nonce():
    return secrets.token_hex(16)


def phone_key(text):
    """The phone's public key, base64 of its X.509 form: P-256 only. Raises ValueError for anything else."""
    try:
        key = serialization.load_der_public_key(base64.b64decode(text, validate=True))
    except (TypeError, ValueError) as e:
        raise ValueError("not a public key") from e
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("not a P-256 key")
    return key


def start_message(nonce, run, instance, mod_nonce):
    """What the phone signs at Start: this gateway's challenge, this run, and that session's own challenge."""
    return "|".join(("walkie-start", nonce, run, instance, mod_nonce))


def confirm_message(nonce, spoken):
    """What the phone signs to confirm a read-back: the gateway's challenge and the words the owner heard."""
    return "|".join(("walkie-confirm", nonce, hashlib.sha256(spoken.encode()).hexdigest()))


def signed(key_text, message, signature):
    """Whether signature, base64 of a DER ECDSA signature, signs message under the phone's key."""
    if not key_text or not isinstance(message, str) or not isinstance(signature, str):
        return False
    try:
        phone_key(key_text).verify(base64.b64decode(signature, validate=True), message.encode(),
                                   ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, TypeError, ValueError):
        return False


def write_private(path, text):
    fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(path + ".new", path)


class PhoneStore:
    """The paired phone: its token, its address, its public key, and when it paired, in private files.

    A pairing from before phones had keys counts as no pairing, so that phone pairs again.
    """

    def __init__(self, state_dir, clock=time.time):
        self.path = os.path.join(state_dir, PHONE_FILE)
        self.key_path = os.path.join(state_dir, PHONE_KEY_FILE)
        self.clock = clock
        self.token = self.address = self.key = None
        self.paired_at = 0.0
        try:
            with open(self.path) as f:
                saved = json.load(f)
            phone_key(saved["key"])
            self.token, self.address, self.key, self.paired_at = saved["token"], saved["address"], saved["key"], float(saved["paired_at"])
        except FileNotFoundError:
            pass
        except (KeyError, TypeError, ValueError):
            self.token = self.address = self.key = None
            print("The saved pairing is from an older version. Pair the phone again with --pair.", flush=True)

    @property
    def paired(self):
        return self.token is not None

    def pair(self, address, key):
        """A new token for the phone at address, with its public key. An earlier phone stops working."""
        phone_key(key)
        token = secrets.token_hex(32)
        now = self.clock()
        write_private(self.path, json.dumps({"token": token, "address": address, "key": key, "paired_at": now}))
        write_private(self.key_path, key + "\n")
        self.token, self.address, self.key, self.paired_at = token, address, key, now
        return token

    def forget(self):
        """No phone is trusted from now on, until one pairs again."""
        self.token = self.address = self.key = None
        for path in (self.path, self.key_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def days_left(self):
        """Whole days until the pairing runs out: 0 or less means it has."""
        return math.ceil((self.paired_at + PAIRING_DAYS * 86400 - self.clock()) / 86400)

    def expired(self):
        return self.clock() >= self.paired_at + PAIRING_DAYS * 86400

    def signed(self, message, signature):
        return signed(self.key, message, signature)

    def allows(self, address):
        """The one address allowed to try logging in, once paired."""
        return self.address is not None and address == self.address


class PairingWindow:
    """One short-lived code. It closes on success, on expiry, or after a few wrong tries."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.code = "%08d" % secrets.randbelow(10 ** 8)
        self.closes = clock() + PAIR_SECONDS
        self.tries = 0

    def open(self):
        return self.code is not None and self.clock() < self.closes and self.tries < PAIR_TRIES

    def attempt(self, code):
        if not self.open():
            return False
        self.tries += 1
        if isinstance(code, str) and hmac.compare_digest(code.encode(), self.code.encode()):
            self.code = None
            return True
        return False


class FailureLimiter:
    """Slows anyone guessing. Counts failed logins and closes the door for a while."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.failures = []
        self.closed_until = 0.0

    def closed(self):
        return self.clock() < self.closed_until

    def failed(self):
        now = self.clock()
        self.failures = [t for t in self.failures if now - t < FAILURE_WINDOW] + [now]
        if len(self.failures) >= FAILURE_LIMIT:
            self.closed_until = now + FAILURE_LOCKOUT
            self.failures = []
