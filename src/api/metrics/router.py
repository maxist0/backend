# src/api/metrics/router.py
"""
Metrics API endpoints.
"""
from fastapi import APIRouter, Response
from src.common.metrics import get_metrics, get_metrics_content_type

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


@router.get("/health")
async def health_check():
    """
    Health check endpoint.
    
    Simple endpoint to verify the API is running.
    """
    return {"status": "healthy", "service": "fastapi-api"}