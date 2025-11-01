"""FastAPI middleware classes for experiment scoping, proxy awareness, and HTTPS enforcement."""
import os
import re
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from fastapi import Request

logger = logging.getLogger(__name__)


class ExperimentScopeMiddleware(BaseHTTPMiddleware):
    """Middleware that sets experiment scope information in request.state."""
    async def dispatch(self, request: Request, call_next: ASGIApp):
        request.state.slug_id = None
        request.state.read_scopes = None
        path = request.url.path
        if path.startswith("/experiments/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 2:
                slug = parts[1]
                exp_cfg = getattr(request.app.state, "experiments", {}).get(slug)
                if exp_cfg:
                    request.state.slug_id = slug
                    request.state.read_scopes = exp_cfg.get("resolved_read_scopes", [slug])
        response = await call_next(request)
        return response


class ProxyAwareHTTPSMiddleware(BaseHTTPMiddleware):
    """
    Proxy-aware middleware: Detects proxy headers and rewrites request.url
    to reflect the actual client scheme/host BEFORE route handlers execute.
    This ensures FastAPI's url_for() generates correct HTTPS URLs when behind proxies.
    
    Handles multiple proxy header formats:
    - X-Forwarded-Proto (Render.com, AWS ELB, etc.)
    - X-Forwarded-Ssl (older proxies)
    - Forwarded (RFC 7239)
    - X-Forwarded-Host
    """
    async def dispatch(self, request: Request, call_next: ASGIApp):
        # Store original values for debugging
        original_scheme = request.url.scheme
        original_host = request.url.hostname
        
        # Detect if we're in localhost/development (no proxy)
        is_localhost = original_host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
        has_proxy_headers = any((
            request.headers.get("X-Forwarded-Proto"),
            request.headers.get("X-Forwarded-Host"),
            request.headers.get("Forwarded"),
            request.headers.get("X-Forwarded-Ssl")
        ))
        
        # Detect actual scheme from proxy headers (priority order)
        detected_scheme = original_scheme
        detected_host = original_host
        detected_port = request.url.port
        if not detected_port:
            server = request.scope.get("server")
            if server and len(server) == 2:
                detected_port = server[1]
        
        # On localhost without proxy headers, respect the original scheme
        if is_localhost and not has_proxy_headers:
            detected_scheme = original_scheme
            logger.debug(f"Localhost request without proxy headers - respecting original scheme: {detected_scheme}")
        else:
            # We're behind a proxy or on a real server - check proxy headers
            forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower()
            if forwarded_proto in ("https", "http"):
                detected_scheme = forwarded_proto
                logger.debug(f"Detected scheme from X-Forwarded-Proto: {detected_scheme}")
            
            if request.headers.get("X-Forwarded-Ssl", "").lower() == "on":
                detected_scheme = "https"
                logger.debug(f"Detected HTTPS from X-Forwarded-Ssl header")
            
            forwarded_header = request.headers.get("Forwarded", "")
            if forwarded_header:
                if "proto=https" in forwarded_header.lower():
                    detected_scheme = "https"
                    logger.debug(f"Detected HTTPS from Forwarded header")
                host_match = re.search(r'host=([^;,\s]+)', forwarded_header, re.IGNORECASE)
                if host_match:
                    host_value = host_match.group(1).strip('"')
                    if ":" in host_value:
                        detected_host, port_str = host_value.rsplit(":", 1)
                        try:
                            detected_port = int(port_str)
                        except ValueError:
                            pass
                    else:
                        detected_host = host_value
            
            forwarded_host = request.headers.get("X-Forwarded-Host")
            if forwarded_host:
                if ":" in forwarded_host:
                    detected_host, port_str = forwarded_host.rsplit(":", 1)
                    try:
                        detected_port = int(port_str)
                    except ValueError:
                        pass
                else:
                    detected_host = forwarded_host
                logger.debug(f"Detected host from X-Forwarded-Host: {detected_host}")
        
        # Force HTTPS in production when FORCE_HTTPS env var is set
        force_https = os.getenv("FORCE_HTTPS", "").lower() == "true"
        if force_https and not is_localhost:
            detected_scheme = "https"
            logger.debug("Forcing HTTPS due to FORCE_HTTPS environment variable")
        
        # Store corrected values in request.state for later use
        request.state.original_scheme = original_scheme
        request.state.original_host = original_host
        request.state.detected_scheme = detected_scheme
        request.state.detected_host = detected_host
        request.state.detected_port = detected_port
        
        # Rewrite request.scope to reflect actual client scheme/host
        if detected_scheme != original_scheme or detected_host != original_host or (
            detected_port and detected_port != request.scope.get("server", (None, None))[1]
        ):
            request.scope["scheme"] = detected_scheme
            port_to_use = detected_port
            if not port_to_use:
                original_server = request.scope.get("server")
                if original_server and len(original_server) == 2:
                    original_port = original_server[1]
                    default_port = 443 if detected_scheme == "https" else 80
                    if original_port != default_port:
                        port_to_use = original_port
                if not port_to_use:
                    port_to_use = 443 if detected_scheme == "https" else 80
            
            request.scope["server"] = (detected_host, port_to_use)
            
            if hasattr(request, "_url"):
                delattr(request, "_url")
            
            logger.info(
                f"Proxy-aware HTTPS: Rewrote request URL "
                f"{original_scheme}://{original_host}:{request.scope.get('server', (None, None))[1]} -> "
                f"{detected_scheme}://{detected_host}:{port_to_use}"
            )
        
        response = await call_next(request)
        return response


class HTTPSEnforcementMiddleware(BaseHTTPMiddleware):
    """
    Proxy-aware security middleware: Only enforces HTTPS when the request actually
    came via HTTPS (detected through proxy headers or direct connection).
    
    This is intelligent for deployments behind proxies like Render.com:
    - If the proxy indicates HTTPS (X-Forwarded-Proto: https), enforces HTTPS
    - If the proxy indicates HTTP, does NOT enforce HTTPS (allows proxy to handle it)
    - Prevents HTTP downgrade attacks and mixed content issues only when HTTPS is active.
    """
    async def dispatch(self, request: Request, call_next: ASGIApp):
        response = await call_next(request)
        
        # Check if this request is actually using HTTPS
        detected_scheme = getattr(request.state, "detected_scheme", None)
        if detected_scheme is None:
            forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower()
            if forwarded_proto in ("https", "http"):
                detected_scheme = forwarded_proto
            elif request.headers.get("X-Forwarded-Ssl", "").lower() == "on":
                detected_scheme = "https"
            elif "proto=https" in request.headers.get("Forwarded", "").lower():
                detected_scheme = "https"
            else:
                detected_scheme = request.url.scheme
        
        is_https = detected_scheme == "https"
        
        if not is_https:
            logger.debug(f"Skipping HTTPS enforcement - request came via {detected_scheme}")
            return response
        
        logger.debug("Enforcing HTTPS - request came via HTTPS")
        
        # Add HSTS header
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
        
        # Force HTTPS on any Location redirect headers
        if "Location" in response.headers:
            location = response.headers["Location"]
            if location.startswith("http://"):
                https_location = location.replace("http://", "https://", 1)
                response.headers["Location"] = https_location
                logger.debug(f"Enforced HTTPS redirect: {location} -> {https_location}")
        
        # Sanitize mixed content in response bodies
        content_type = response.headers.get("content-type", "").lower()
        text_content_types = [
            "application/json",
            "text/html",
            "text/css",
            "text/javascript",
            "application/javascript",
            "text/plain",
            "text/xml",
            "application/xml",
        ]
        
        if any(ct in content_type for ct in text_content_types):
            if hasattr(response, "body") and response.body:
                try:
                    if isinstance(response.body, bytes):
                        body_text = response.body.decode("utf-8")
                    else:
                        body_text = str(response.body)
                    
                    original_body = body_text
                    body_text = re.sub(r'"http://', '"https://', body_text)
                    body_text = re.sub(r'\bhttp://', 'https://', body_text)
                    body_text = re.sub(r'(src|href)=["\']http://', r'\1="https://', body_text)
                    body_text = re.sub(r'url\(http://', 'url(https://', body_text)
                    body_text = re.sub(r'":\s*"http://', '": "https://', body_text)
                    
                    if body_text != original_body:
                        response.body = body_text.encode("utf-8")
                        if "content-length" in response.headers:
                            response.headers["content-length"] = str(len(response.body))
                        logger.debug("Sanitized mixed content in response body (HTTP -> HTTPS)")
                except (UnicodeDecodeError, AttributeError) as e:
                    logger.debug(f"Skipping mixed content sanitization: {e}")
        
        return response

