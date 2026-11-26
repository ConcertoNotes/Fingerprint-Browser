"""Symmetric encryption for secrets at rest (proxy credentials and cloud nodes).

Uses Fernet keyed by ``CPM_SECRET_KEY``. When no key is configured the standard
``encrypt``/``decrypt`` functions are the identity transform, so local-only
setups keep working. Decryption also tolerates plaintext values written before
a key was configured, easing migration.

Cloud-delivered node configurations use ``encrypt_with_salt`` /
``decrypt_with_salt`` (``enc2:`` prefix), deriving a per-tenant Fernet key via
HKDF-SHA256 from ``CPM_SECRET_KEY`` and the cloud-issued ``seal_salt``.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from loguru import logger

from camoufox_pm.config import get_settings

_PREFIX = "enc:"
_PREFIX_V2 = "enc2:"
_CLOUD_NODE_INFO = b"prism-cloud-node-v2"


def _fernet() -> Fernet | None:
    key = get_settings().secret_key
    if not key:
        return None
    return Fernet(key.encode() if isinstance(key, str) else key)


def _fernet_with_salt(salt: str) -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise ValueError("CPM_SECRET_KEY is required for salted cloud encryption.")
    clean_salt = (salt or "").strip()
    if not clean_salt:
        raise ValueError("A non-empty seal_salt is required for salted cloud encryption.")
    ikm = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=clean_salt.encode("utf-8"),
        info=_CLOUD_NODE_INFO,
    ).derive(ikm)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value: str) -> str:
    """Encrypt ``value``. Returns it unchanged when no key is configured."""
    fernet = _fernet()
    if fernet is None:
        return value
    token = fernet.encrypt(value.encode()).decode()
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Decrypt ``value``. Plaintext and unkeyed values pass through unchanged."""
    if not value.startswith(_PREFIX):
        return value
    fernet = _fernet()
    if fernet is None:
        logger.warning(
            "Found an encrypted value but CPM_SECRET_KEY is not set; returning it "
            "undecrypted. Set CPM_SECRET_KEY to the key used to encrypt it."
        )
        return value
    try:
        return fernet.decrypt(value[len(_PREFIX) :].encode()).decode()
    except InvalidToken:
        logger.warning(
            "Failed to decrypt a stored secret (wrong CPM_SECRET_KEY or corrupted "
            "data); returning the raw value."
        )
        return value


def encrypt_with_salt(plaintext: str, salt: str) -> str:
    """Encrypt ``plaintext`` with an HKDF-SHA256 derived Fernet key (``enc2:``)."""
    fernet = _fernet_with_salt(salt)
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX_V2 + token


def decrypt_with_salt(value: str, salt: str) -> str:
    """Decrypt an ``enc2:`` ciphertext derived from ``CPM_SECRET_KEY`` and ``salt``."""
    if not isinstance(value, str) or not value.startswith(_PREFIX_V2):
        raise ValueError("Expected an 'enc2:' ciphertext.")
    fernet = _fernet_with_salt(salt)
    try:
        return fernet.decrypt(value[len(_PREFIX_V2) :].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ValueError(
            "Failed to decrypt cloud node secret (invalid key, wrong salt, or corrupted data)."
        ) from None


seal_cloud_config = encrypt_with_salt
unseal_cloud_config = decrypt_with_salt
