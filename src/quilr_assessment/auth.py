import hmac
import time

import jwt

from .config import Settings
from .errors import GatewayError


def bearer(header: str | None) -> str:
    parts = (header or "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise GatewayError("unauthorized", "A valid bearer token is required", 401)
    return parts[1]


def mcp_role(header: str | None, settings: Settings) -> str:
    token = bearer(header)
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "role"]},
        )
        if claims["role"] not in ("admin", "viewer") or not claims["sub"]:
            raise ValueError("Invalid claims")
        return claims["role"]
    except (jwt.PyJWTError, ValueError, TypeError):
        raise GatewayError("unauthorized", "A valid bearer token is required", 401) from None


def tenant_for(header: str | None, settings: Settings) -> str:
    key = bearer(header)
    for configured, tenant in settings.tenant_keys.items():
        if hmac.compare_digest(key.encode(), configured.encode()):
            return tenant
    raise GatewayError("unauthorized", "A valid API key is required", 401)


def issue_demo_token(role: str, settings: Settings, ttl: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "demo-user",
            "role": role,
            "iat": now,
            "exp": now + ttl,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
