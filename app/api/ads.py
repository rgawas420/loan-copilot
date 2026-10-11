"""Sponsored cards: click / hide / report for users, and ad management for admins."""
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.api.deps import admin_user, current_user
from app.services import accounts, ads

router = APIRouter(prefix="/api")


class AdIn(BaseModel):
    advertiser: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=90)
    body: str = Field(default="", max_length=220)
    link: str = Field(min_length=8, max_length=500)
    cta: str = Field(default="", max_length=30)
    banks: List[str] = Field(default_factory=list, max_length=30)
    products: List[str] = Field(default_factory=list, max_length=30)
    keywords: List[str] = Field(default_factory=list, max_length=30)
    starts: Optional[str] = Field(default=None, max_length=25)
    ends: Optional[str] = Field(default=None, max_length=25)
    priority: int = Field(default=0, ge=0, le=100)
    active: bool = True


class ActiveIn(BaseModel):
    active: bool


class ReportIn(BaseModel):
    reason: Literal["misleading", "offensive", "irrelevant", "other"] = "other"
    note: str = Field(default="", max_length=300)


def _call(fn, *args):
    try:
        return fn(*args)
    except ads.AdError as e:
        raise HTTPException(e.code, str(e))


# ------------------------------------------------------------------ users
@router.get("/ads/{ad_id}/click")
def ad_click(ad_id: int, user=Depends(current_user)):
    """Count the click, then go to the advertiser's page."""
    link = ads.record(ad_id, user["id"], "click")
    if link is None:
        raise HTTPException(404, "Ad not found.")
    return RedirectResponse(link, status_code=302)


@router.get("/ads/{ad_id}/image")
def ad_image(ad_id: int, _=Depends(current_user)):
    p = ads.image_path(ad_id)
    if p is None:
        raise HTTPException(404, "No image.")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/ads/{ad_id}/hide")
def ad_hide(ad_id: int, user=Depends(current_user)):
    if ads.record(ad_id, user["id"], "hide") is None:
        raise HTTPException(404, "Ad not found.")
    return {"hidden": ad_id}


@router.post("/ads/{ad_id}/report")
def ad_report(ad_id: int, body: ReportIn, user=Depends(current_user)):
    detail = body.reason + (f": {body.note.strip()}" if body.note.strip() else "")
    if ads.record(ad_id, user["id"], "report", detail) is None:
        raise HTTPException(404, "Ad not found.")
    ads.record(ad_id, user["id"], "hide")             # a reported ad is not shown to that user again
    return {"reported": ad_id}


# ------------------------------------------------------------------ admins
@router.get("/admin/ads")
def admin_ads(days: Optional[int] = None, _=Depends(admin_user)):
    from app.core.config import get_settings
    s = get_settings()
    return {"ads": ads.list_ads(days), "enabled": s.ads_enabled, "every_n_answers": s.ads_every_n_answers}


@router.post("/admin/ads")
def admin_ad_create(body: AdIn, admin=Depends(admin_user)):
    ad = _call(ads.create, body.model_dump())
    accounts.log(admin["id"], "ad_created", f"{ad['advertiser']}: {ad['title']}"[:200])
    return ad


@router.put("/admin/ads/{ad_id}")
def admin_ad_update(ad_id: int, body: AdIn, admin=Depends(admin_user)):
    ad = _call(ads.update, ad_id, body.model_dump())
    accounts.log(admin["id"], "ad_updated", f"{ad['advertiser']}: {ad['title']}"[:200])
    return ad


@router.patch("/admin/ads/{ad_id}")
def admin_ad_toggle(ad_id: int, body: ActiveIn, admin=Depends(admin_user)):
    ad = _call(ads.update, ad_id, {"active": body.active})
    accounts.log(admin["id"], "ad_updated", f"{'resumed' if body.active else 'paused'} {ad['title']}"[:200])
    return ad


@router.post("/admin/ads/{ad_id}/image")
async def admin_ad_image(ad_id: int, file: UploadFile = File(...), admin=Depends(admin_user)):
    data = await file.read(ads.MAX_IMAGE_BYTES + 1)
    from fastapi.concurrency import run_in_threadpool
    return await run_in_threadpool(_call, ads.set_image, ad_id, data)


@router.delete("/admin/ads/{ad_id}")
def admin_ad_delete(ad_id: int, admin=Depends(admin_user)):
    if not ads.delete(ad_id):
        raise HTTPException(404, "Ad not found.")
    accounts.log(admin["id"], "ad_deleted", str(ad_id))
    return {"deleted": ad_id}


@router.get("/admin/ads/{ad_id}/report")
def admin_ad_report(ad_id: int, days: int = 30, _=Depends(admin_user)):
    ad = ads.get(ad_id)
    if ad is None:
        raise HTTPException(404, "Ad not found.")
    return {"ad": ad, "daily": ads.daily(ad_id, min(max(days, 1), 365)), "reports": ads.reports(ad_id)}
