# src/app/api/metrics/router.py
"""
Metrics API endpoints.
"""

from datetime import datetime
from fastapi import APIRouter, Response, Request
from app.common.metrics import get_metrics, get_metrics_content_type
from app.common.dependencies import SettingsDep
from app.common.responses import ok_response, SuccessResponse
import os

router = APIRouter()


@router.get("/")
async def metrics(request: Request):
    """
    Prometheus metrics endpoint.

    Returns metrics in Prometheus format for scraping by monitoring systems.
    """
    metrics_data = get_metrics()
    return Response(content=metrics_data, media_type=get_metrics_content_type())


@router.get("/health", response_model=SuccessResponse)
async def health_check(request: Request, settings: SettingsDep):
    """
    Health check endpoint with standardized response.

    Simple endpoint to verify the API is running.
    """
    start_time = (
        request.app.state.start_time
        if hasattr(request.app.state, "start_time")
        else datetime.now()
    )

    health_data = {
        "status": "healthy",
        "service": settings.APP_NAME,
        "metrics_available": True,
        "pod_name": os.getenv("MY_POD_NAME"),
        "node_name": os.getenv("MY_NODE_NAME"),
        "uptime_seconds": datetime.now().timestamp() - start_time.timestamp()
        if start_time
        else None,
    }

    return ok_response(data=health_data, request=request, start_time=start_time)
