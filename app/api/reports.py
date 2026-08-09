"""
Reports / Support tickets API.

Merchants (or logged-out visitors) open a ticket; the operator reviews it from
the operator panel and can change its status and leave a note. This is a simple,
self-contained ticket system no funds, no payment data.

Endpoints:
  POST   /api/reports/            create a ticket
  GET    /api/reports/mine        list the current user's tickets
  GET    /api/reports/panel       (operator) list all tickets, filterable
  PATCH  /api/reports/panel/{id}  (operator) update status / add a note
"""
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import rate_limit_strict
from app.core.validators import sanitize_str
from app.core.security import verify_access_token
from app.models.models import User, UserRole, Report, ReportType, REPORT_STATUSES

router = APIRouter(prefix="/api/reports", tags=["reports"])

_TYPES = {t.value for t in ReportType}


def _now():
    return datetime.now(timezone.utc)


def _clean(s: str, n: int) -> str:
    """Plain-text only: strip tags + dangerous chars, collapse, cap length."""
    if not s:
        return ""
    s = sanitize_str(s, n)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[<>]", "", s)
    s = re.sub(r"javascript:|data:|vbscript:", "", s, flags=re.IGNORECASE)
    return s.strip()[:n]


async def _current_user(request: Request, db: AsyncSession):
    """Return the logged-in user or None (tickets allow anonymous senders)."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        uid = verify_access_token(token)
    except Exception:
        return None
    if not uid:
        return None
    res = await db.execute(select(User).where(User.id == uid))
    return res.scalar_one_or_none()


async def _require_operator(request: Request, db: AsyncSession) -> User:
    user = await _current_user(request, db)
    if not user:
        raise HTTPException(401, "Authentication required")
    from app.core.config import get_settings
    ops = {e.strip().lower() for e in (get_settings().OPERATOR_EMAILS or "").split(",") if e.strip()}
    is_operator = (user.role == UserRole.operator) or ((user.email or "").lower() in ops)
    if not is_operator:
        raise HTTPException(403, "Operator access required")
    return user


def _serialize(r: Report) -> dict:
    return {
        "id":                 r.id,
        "type":               r.type.value if hasattr(r.type, "value") else r.type,
        "subject":            r.subject,
        "message":            r.message,
        "requested_app_name": r.requested_app_name,
        "status":             r.status,
        "operator_notes":        r.operator_notes,
        "created_at":         r.created_at.isoformat() if r.created_at else None,
        "reviewed_at":        r.reviewed_at.isoformat() if r.reviewed_at else None,
    }


class ReportIn(BaseModel):
    type:    str = Field(default="other")
    subject: str = Field(default="")
    message: str = Field(default="")
    requested_app_name: str | None = None
    email:   str | None = None     # only used for anonymous senders


@router.post("/", dependencies=[Depends(rate_limit_strict)])
async def create_report(body: ReportIn, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _current_user(request, db)

    rtype = body.type if body.type in _TYPES else "other"
    subject = _clean(body.subject, 160)
    message = _clean(body.message, 4000)
    if len(message) < 5:
        raise HTTPException(422, "Please describe your issue (at least 5 characters).")

    req_name = None
    if rtype == "change_app_name":
        req_name = _clean(body.requested_app_name or "", 64) or None

    email = None
    if user is None:
        email = _clean(body.email or "", 254)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""):
            raise HTTPException(422, "A valid email is required to open a ticket.")

    rep = Report(
        user_id            = user.id if user else None,
        email              = (user.email if user else email),
        type               = ReportType(rtype),
        subject            = subject or None,
        message            = message,
        requested_app_name = req_name,
        status             = "pending",
        ip_address         = (request.client.host if request.client else None),
        user_agent         = (request.headers.get("user-agent") or "")[:300] or None,
        created_at         = _now(),
    )
    db.add(rep)
    await db.commit()
    return {"ok": True, "id": rep.id, "status": rep.status}


@router.get("/mine")
async def my_reports(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _current_user(request, db)
    if not user:
        # Logged-out users have no "my tickets" view; return an empty list so
        # the dashboard widget renders cleanly instead of erroring.
        return {"reports": []}
    res = await db.execute(
        select(Report).where(Report.user_id == user.id).order_by(desc(Report.created_at)).limit(50)
    )
    return {"reports": [_serialize(r) for r in res.scalars().all()]}


@router.get("/panel")
async def panel_list(request: Request, db: AsyncSession = Depends(get_db),
                     status: str | None = None, type: str | None = None):
    await _require_operator(request, db)
    q = select(Report).order_by(desc(Report.created_at)).limit(200)
    if status and status in REPORT_STATUSES:
        q = q.where(Report.status == status)
    if type and type in _TYPES:
        q = q.where(Report.type == ReportType(type))
    res = await db.execute(q)
    rows = res.scalars().all()
    out = []
    for r in rows:
        d = _serialize(r)
        d["email"] = r.email
        d["user_id"] = r.user_id
        out.append(d)
    return {"reports": out, "count": len(out)}


class ReportUpdate(BaseModel):
    status:      str | None = None
    operator_notes: str | None = None


@router.patch("/panel/{report_id}")
async def panel_update(report_id: str, body: ReportUpdate, request: Request,
                       db: AsyncSession = Depends(get_db)):
    operator = await _require_operator(request, db)
    res = await db.execute(select(Report).where(Report.id == report_id))
    rep = res.scalar_one_or_none()
    if not rep:
        raise HTTPException(404, "Ticket not found")

    if body.status is not None:
        if body.status not in REPORT_STATUSES:
            raise HTTPException(422, "Invalid status")
        rep.status = body.status
        rep.reviewed_at = _now()
        rep.reviewed_by = operator.id
    if body.operator_notes is not None:
        rep.operator_notes = _clean(body.operator_notes, 2000) or None

    db.add(rep)
    await db.commit()
    return {"ok": True, "id": rep.id, "status": rep.status}
