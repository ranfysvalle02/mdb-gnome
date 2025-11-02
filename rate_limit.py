"""
Rate limiting configuration for protecting endpoints from brute force and DDoS attacks.
Uses slowapi (Flask-Limiter wrapper for FastAPI) to implement rate limiting.
"""
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from fastapi.responses import JSONResponse, HTMLResponse
from starlette.status import HTTP_429_TOO_MANY_REQUESTS


# Initialize rate limiter with default key function (IP address)
limiter = Limiter(key_func=get_remote_address)


def get_rate_limit_key(request: Request) -> str:
    """
    Custom key function for rate limiting.
    Uses IP address for identification, with fallback for development.
    """
    return get_remote_address(request)


# Rate limit configurations
# All limits are per IP address

# Strict limits for sensitive endpoints (prevent brute force)
LOGIN_POST_LIMIT = "5 per minute"  # 5 login attempts per minute
LOGIN_GET_LIMIT = "20 per minute"   # 20 page loads per minute

REGISTER_POST_LIMIT = "3 per minute"  # 3 registration attempts per minute
REGISTER_GET_LIMIT = "20 per minute"   # 20 page loads per minute

# Moderate limits for export endpoints (prevent DDoS)
EXPORT_LIMIT = "10 per minute"      # 10 export requests per minute per slug
EXPORT_FILE_LIMIT = "30 per minute"  # 30 file downloads per minute


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """
    Custom handler for rate limit exceeded errors.
    Returns appropriate response based on route type (API vs HTML).
    """
    # Check if this is an API route
    if request.url.path.startswith("/admin/api/") or request.url.path.startswith("/api/"):
        response = JSONResponse(
            status_code=HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "Rate limit exceeded",
                "detail": f"Too many requests. Limit: {exc.detail}",
                "retry_after": getattr(exc, 'retry_after', None)
            }
        )
    else:
        # For HTML routes (login, register), return HTML error page
        from fastapi.templating import Jinja2Templates
        from pathlib import Path
        from config import TEMPLATES_DIR
        
        templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
        
        # Try to use error template if available, otherwise simple HTML
        error_template_path = Path(TEMPLATES_DIR / "error.html")
        if error_template_path.exists():
            response = templates.TemplateResponse(
                "error.html",
                {
                    "request": request,
                    "status_code": HTTP_429_TOO_MANY_REQUESTS,
                    "detail": "Too many requests. Please try again later.",
                    "retry_after": getattr(exc, 'retry_after', None)
                },
                status_code=HTTP_429_TOO_MANY_REQUESTS
            )
        else:
            # Fallback to simple HTML response
            retry_after = getattr(exc, 'retry_after', 'unknown')
            response = HTMLResponse(
                content=f"""
                <!DOCTYPE html>
                <html>
                <head><title>Rate Limit Exceeded</title></head>
                <body>
                    <h1>Rate Limit Exceeded</h1>
                    <p>Too many requests. Please try again later.</p>
                    <p>Retry after: {retry_after} seconds</p>
                </body>
                </html>
                """,
                status_code=HTTP_429_TOO_MANY_REQUESTS
            )
    
    # Inject rate limit headers if available
    try:
        if hasattr(request.app.state, 'limiter') and hasattr(request.state, 'view_rate_limit'):
            response = request.app.state.limiter._inject_headers(
                response, request.state.view_rate_limit
            )
    except Exception:
        # If header injection fails, continue without it
        pass
    
    return response

