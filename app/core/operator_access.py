from sqlalchemy import select

from app.core.config import get_settings
from app.models.models import User, UserRole


async def resolve_operator(user: User | None, db) -> bool:
    if user is None:
        return False
    if user.role == UserRole.operator:
        return True
    s = get_settings()
    email_match = s.is_operator_email(user.email or "")
    if not (email_match or s.is_operator_username(user.username or "")):
        return False
    proven = email_match and bool(getattr(user, "email_verified", False))
    if not proven:
        other = (await db.execute(
            select(User.id).where(User.role == UserRole.operator, User.id != user.id).limit(1)
        )).scalar_one_or_none()
        if other:
            return False
    user.role = UserRole.operator
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return True
