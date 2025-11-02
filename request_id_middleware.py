"""
Request ID Middleware for tracing and debugging.

Generates a unique request ID for each HTTP request and includes it in logs
and response headers for better traceability.
"""
import uuid
import logging
import contextvars
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from fastapi import Request

logger = logging.getLogger(__name__)

# Context variable to store request ID for logging
_request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar('request_id', default=None)


class RequestIDLoggingFilter(logging.Filter):
    """
    Logging filter that adds request ID to log records.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        request_id = _request_id_context.get(None)
        if request_id:
            record.request_id = request_id
        else:
            record.request_id = "no-request-id"
        return True


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware that generates a unique request ID for each request.
    
    - Generates UUID v4 request ID
    - Adds request ID to request.state for use in route handlers
    - Adds request ID to response headers (X-Request-ID)
    - Uses contextvars to make request ID available in logs
    """
    
    async def dispatch(self, request: Request, call_next: ASGIApp):
        # Generate unique request ID (UUID v4)
        request_id = str(uuid.uuid4())[:8]  # Use shorter ID for readability (first 8 chars)
        
        # Store in request.state for use in route handlers
        request.state.request_id = request_id
        
        # Set context variable for logging
        token = _request_id_context.set(request_id)
        
        try:
            # Process request
            response = await call_next(request)
            
            # Add request ID to response headers
            response.headers["X-Request-ID"] = request_id
            
            # Log request completion (use request ID from context)
            logger.debug(f"[{request_id}] Request completed: {request.method} {request.url.path}")
            
            return response
        finally:
            # Reset context variable
            _request_id_context.reset(token)

