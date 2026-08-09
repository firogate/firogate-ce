from fastapi import HTTPException

PERMISSION_CHOICES = [
    "create_invoice",
    "read_invoice",
    "read_analytics",
    "wallet_connect",
]


async def check_api_permission(api_key_row, permission: str, db) -> None:
    scopes = (api_key_row.scopes or "*").strip()
    if scopes == "*":
        return
    granted = {s.strip() for s in scopes.split(",") if s.strip()}
    if permission not in granted:
        raise HTTPException(403, f"API key does not have the '{permission}' permission")
