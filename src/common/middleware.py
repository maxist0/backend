# src/common/middleware.py
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.common.logging import get_access_logger


class AccessLogMiddleware(BaseHTTPMiddleware):
    """
    Middleware to log HTTP access information including:
    - Client IP address
    - HTTP method and path
    - User agent
    - Response status code
    - Processing time
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        access_logger = get_access_logger()
        
        # Record start time
        start_time = time.perf_counter()
        
        # Get client information
        client_ip = self._get_client_ip(request)
        method = request.method
        path = request.url.path
        query_string = request.url.query
        full_path = f"{path}?{query_string}" if query_string else path
        user_agent = request.headers.get("user-agent", "-")
        referer = request.headers.get("referer", "-")
        
        # Process the request
        response: Response = await call_next(request)
        
        # Calculate processing time
        process_time = time.perf_counter() - start_time
        process_time_ms = round(process_time * 1000, 2)
        
        # Get response info
        status_code = response.status_code
        content_length = response.headers.get("content-length", "-")
        
        # Log in a structured format similar to Apache/Nginx combined log format
        log_message = (
            f'{client_ip} - "{method} {full_path}" '
            f'{status_code} {content_length} '
            f'{process_time_ms}ms '
            f'"{referer}" "{user_agent}"'
        )
        
        access_logger.info(log_message)
        
        # Add processing time header to response
        response.headers["X-Process-Time"] = f"{process_time_ms}ms"
        
        return response

    def _get_client_ip(self, request: Request) -> str:
        """
        Get the client IP address, considering proxy headers.
        
        Checks in order:
        1. X-Forwarded-For (first IP in the chain)
        2. X-Real-IP
        3. Direct client host
        """
        # Check X-Forwarded-For header (may contain multiple IPs)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # Take the first IP (original client)
            return forwarded_for.split(",")[0].strip()
        
        # Check X-Real-IP header
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
        
        # Fall back to direct client
        if request.client:
            return request.client.host
        
        return "-"
