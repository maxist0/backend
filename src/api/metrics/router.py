# src/api/metrics/router.py
"""
Metrics API endpoints.
"""
import time
from fastapi import APIRouter, Response, Request
from src.common.metrics import get_metrics, get_metrics_content_type
from src.common.responses import ok_response, SuccessResponse

router = APIRouter()


@router.get("/")
async def metrics():
    """
    Prometheus metrics endpoint.
    
    Returns metrics in Prometheus format for scraping by monitoring systems.
    """
    metrics_data = get_metrics()
    return Response(
        content=metrics_data,
        media_type=get_metrics_content_type()
    )


@router.get("/health", response_model=SuccessResponse)
async def health_check(request: Request):
    """
    Health check endpoint with standardized response.
    
    Simple endpoint to verify the API is running.
    """
    start_time = time.time()
    
    health_data = {
        "status": "healthy", 
        "service": "fastapi-api",
        "metrics_available": True
    }
    
    return ok_response(data=health_data, request=request, start_time=start_time)