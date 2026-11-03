"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# API keys copied from examples and docs. Anyone can guess them, so a server using one is treated as a
# server without a key (its API answers only the computer it runs on).
PLACEHOLDER_API_KEYS = frozenset({
    "change-me", "changeme", "change_me", "your-api-key", "your_api_key", "api-key", "apikey",
    "secret", "password", "openring", "test", "xxx",
})


def is_placeholder_key(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDER_API_KEYS


@dataclass
class Settings:
    # Bearer token required by the REST API. Empty (or an example value): the API answers only this computer.
    api_key: str = field(default_factory=lambda: _env("OPENRING_API_KEY"))
    # Initial dashboard password, for servers nobody opens from their own browser (Docker, VPS).
    # Used only while no password is set; afterwards it is changed from the dashboard.
    password: str = field(default_factory=lambda: _env("OPENRING_PASSWORD"))
    # Public https URL where telephony providers reach this server (e.g. an ngrok URL).
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL").rstrip("/"))
    db_path: str = field(default_factory=lambda: _env("OPENRING_DB_PATH", "data/openring.db"))
    # Assistant used when a call arrives on a number with no assistant attached.
    default_assistant_id: str = field(default_factory=lambda: _env("DEFAULT_ASSISTANT_ID"))

    twilio_account_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    validate_twilio_signature: bool = field(
        default_factory=lambda: _env("VALIDATE_TWILIO_SIGNATURE", "true").lower() != "false"
    )

    # Telnyx: the API key lets OpenRing hang up calls; the public key (Mission Control > Keys & Credentials
    # > Public Key) proves each webhook comes from Telnyx.
    telnyx_api_key: str = field(default_factory=lambda: _env("TELNYX_API_KEY"))
    telnyx_public_key: str = field(default_factory=lambda: _env("TELNYX_PUBLIC_KEY"))
    validate_telnyx_signature: bool = field(
        default_factory=lambda: _env("VALIDATE_TELNYX_SIGNATURE", "true").lower() != "false"
    )

    # Telnyx outgoing calls go through a TeXML application; a number can name its own with `texmlApplicationId`.
    telnyx_application_id: str = field(default_factory=lambda: _env("TELNYX_APPLICATION_ID"))

    # Plivo: the auth id and token place calls and check each webhook's signature.
    plivo_auth_id: str = field(default_factory=lambda: _env("PLIVO_AUTH_ID"))
    plivo_auth_token: str = field(default_factory=lambda: _env("PLIVO_AUTH_TOKEN"))
    validate_plivo_signature: bool = field(
        default_factory=lambda: _env("VALIDATE_PLIVO_SIGNATURE", "true").lower() != "false"
    )

    # Vonage: the application's id and private key (the file Vonage lets you download, or its text) place calls;
    # the account's signature secret checks each webhook.
    vonage_application_id: str = field(default_factory=lambda: _env("VONAGE_APPLICATION_ID"))
    vonage_private_key: str = field(default_factory=lambda: _env("VONAGE_PRIVATE_KEY"))
    vonage_signature_secret: str = field(default_factory=lambda: _env("VONAGE_SIGNATURE_SECRET"))
    validate_vonage_signature: bool = field(
        default_factory=lambda: _env("VALIDATE_VONAGE_SIGNATURE", "true").lower() != "false"
    )

    # Genesys Cloud: the API key and client secret of its Audio Connector integration. Genesys sends the key
    # and signs every connection with the secret.
    genesys_api_key: str = field(default_factory=lambda: _env("GENESYS_API_KEY"))
    genesys_client_secret: str = field(default_factory=lambda: _env("GENESYS_CLIENT_SECRET"))

    # SIP trunks (sip.py): nothing listens until a trunk is saved. The carrier must reach this UDP port and the RTP
    # ports. SIP_PUBLIC_IP is the address it should send to, when this computer doesn't have it (behind a NAT).
    sip_host: str = field(default_factory=lambda: _env("SIP_HOST", "0.0.0.0"))
    sip_port: int = field(default_factory=lambda: int(_env("SIP_PORT", "5060")))
    sip_public_ip: str = field(default_factory=lambda: _env("SIP_PUBLIC_IP"))
    sip_rtp_ports: str = field(default_factory=lambda: _env("SIP_RTP_PORTS", "40000-40999"))

    @property
    def public_ws_url(self) -> str:
        if self.public_url.startswith("https://"):
            return "wss://" + self.public_url[len("https://"):]
        if self.public_url.startswith("http://"):
            return "ws://" + self.public_url[len("http://"):]
        return self.public_url


def get_settings() -> Settings:
    return Settings()
