import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.deals_tracker.controller import DealsController
from app.agent.deals_tracker.models import DealAlert, DealPriceHistory, DealProduct  # noqa: F401
from app.agent.deals_tracker.service import DealsService, sse_payload
from app.core.sync_db import get_sync_session

router = APIRouter(tags=["deals"])


class DealsSearchRequest(BaseModel):
    query: str | None = None
    url: str | None = None


class TrackPriceRequest(BaseModel):
    product_id: str
    target_price: float


@router.post("/api/deals/search")
async def deals_search(payload: DealsSearchRequest, session: Session = Depends(get_sync_session)):
    service = DealsService(session)
    controller = DealsController(service)
    try:
        result = await controller.search(query=payload.query, url=payload.url)
        return result if "status" in result else {"status": "success", **result}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/deals/search/stream")
async def deals_search_stream(payload: DealsSearchRequest, session: Session = Depends(get_sync_session)):
    service = DealsService(session)

    async def events():
        try:
            async for event in service.stream_search(query=payload.query, url=payload.url):
                yield sse_payload(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield sse_payload({"event": "error", "message": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/api/deals/history/{product_id}")
def deals_history(product_id: str, session: Session = Depends(get_sync_session)):
    service = DealsService(session)
    try:
        return {"status": "success", "history": service.get_history(product_id)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/deals/track")
def deals_track(payload: TrackPriceRequest, session: Session = Depends(get_sync_session)):
    service = DealsService(session)
    try:
        return {"status": "success", "alert": service.set_alert(payload.product_id, payload.target_price)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
