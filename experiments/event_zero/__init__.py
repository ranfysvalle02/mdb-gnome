"""
Event Zero Experiment
FastAPI routes that delegate to the Ray Actor.
"""

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Form, Query, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from typing import Optional, Dict, Any, List

from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()

# Note: Authentication is handled at router level via manifest.json auth_required: true
# Router-level dependency ensures authentication. Routes access user from request.state


async def get_actor_handle(request: Request) -> "ray.actor.ActorHandle":
    """FastAPI Dependency to get the Event Zero actor handle."""
    if not getattr(request.app.state, "ray_is_available", False):
        logger.error("Ray is globally unavailable, blocking actor handle request.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ray service is unavailable. Check Ray cluster status."
        )
    
    slug_id = getattr(request.state, "slug_id", None)
    if not slug_id:
        logger.error("Server error: slug_id not found in request state.")
        raise HTTPException(500, "Server error: slug_id not found in request state.")
    
    actor_name = f"{slug_id}-actor"
    
    try:
        handle = ray.get_actor(actor_name, namespace="modular_labs")
        return handle
    except ValueError:
        logger.error(f"CRITICAL: Actor '{actor_name}' found no process running.")
        raise HTTPException(503, f"Experiment service '{actor_name}' is not running.")
    except Exception as e:
        logger.error(f"Failed to get actor handle '{actor_name}': {e}", exc_info=True)
        raise HTTPException(500, "Error connecting to experiment service.")


def get_user_id_for_actor(user: Dict[str, Any]) -> str:
    """Extract user_id from user dict (from sub-auth or platform auth)."""
    # Try experiment user ID first (from sub-auth), then fall back to platform user ID
    return user.get("_id") or user.get("experiment_user_id") or user.get("user_id") or user.get("platform_user_id")


async def get_user_from_request(request: Request) -> Dict[str, Any]:
    """Get user from sub-authentication session."""
    from sub_auth import get_experiment_sub_user
    from core_deps import get_experiment_config, get_scoped_db
    
    slug_id = getattr(request.state, "slug_id", "event_zero")
    config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
    db = await get_scoped_db(request)
    
    user = await get_experiment_sub_user(request, slug_id, db, config)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


# ==============================================================================
# Frontend Routes
# ==============================================================================

@bp.get("", response_class=HTMLResponse)
@bp.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Display main page."""
    try:
        from fastapi.templating import Jinja2Templates
        from pathlib import Path
        
        experiment_dir = Path(__file__).resolve().parent
        templates_dir = experiment_dir / "templates"
        experiment_templates = Jinja2Templates(directory=str(templates_dir))
        
        # Try to get user from request state (set by router-level auth dependency)
        user = getattr(request.state, "user", None)
        
        return experiment_templates.TemplateResponse(
            "index.html",
            {"request": request, "user": user}
        )
    except Exception as e:
        logger.error(f"Error rendering index page: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error rendering index page")


# ==============================================================================
# Authentication Routes
# ==============================================================================

@bp.post("/api/v1/auth/login")
async def login(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Login endpoint using sub-authentication."""
    try:
        data = await request.json()
        email = data.get('email')
        password = data.get('password')
        
        user = await actor.authenticate_user.remote(email, password)
        if not user:
            return JSONResponse({"msg": "Bad email or password"}, status_code=401)
        
        # Use sub-authentication to create experiment session
        from sub_auth import create_experiment_session
        from core_deps import get_experiment_config
        
        slug_id = getattr(request.state, "slug_id", "event_zero")
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        response = JSONResponse({"msg": "Login successful", "user": user})
        
        # Create sub-auth session cookie
        await create_experiment_session(
            request, 
            slug_id, 
            user["user_id"], 
            config, 
            response
        )
        
        return response
    except Exception as e:
        logger.error(f"Login error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Login failed")


@bp.get("/api/v1/auth/profile")
async def profile(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get user profile using sub-authentication."""
    # Get user from sub-auth session
    from sub_auth import get_experiment_sub_user
    from core_deps import get_experiment_config, get_scoped_db
    from datetime import datetime
    from bson import ObjectId
    
    slug_id = getattr(request.state, "slug_id", "event_zero")
    config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
    db = await get_scoped_db(request)
    
    user = await get_experiment_sub_user(request, slug_id, db, config)
    
    if not user:
        # Return 401 if not authenticated (expected behavior for unauthenticated users)
        raise HTTPException(status_code=401, detail="Authentication required")
    
    # Get user ID from sub-auth user
    user_id = user.get("_id") or user.get("experiment_user_id")
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_doc = await actor.get_user_profile.remote(str(user_id))
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Convert MongoDB document to JSON-serializable format
    # Use the utility function from utils.py to handle datetime, ObjectId, etc.
    from utils import make_json_serializable
    serializable_doc = make_json_serializable(user_doc)
    
    return JSONResponse(serializable_doc)


# ==============================================================================
# Hotel Routes
# ==============================================================================

@bp.get("/api/v1/hotels/")
async def list_hotels(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """List hotels (public endpoint)."""
    hotels = await actor.list_hotels.remote(admin_only=False)
    return JSONResponse(hotels)


@bp.get("/api/v1/admin/hotels/")
async def admin_list_hotels(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """List hotels (admin endpoint with full details)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    hotels = await actor.list_hotels.remote(admin_only=True)
    return JSONResponse(hotels)


@bp.post("/api/v1/admin/hotels/")
async def create_hotel(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Create a hotel (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    data = await request.json()
    hotel = await actor.create_hotel.remote(
        name=data.get('name'),
        location=data.get('location'),
        image_url=data.get('image_url')
    )
    return JSONResponse(hotel, status_code=201)


@bp.put("/api/v1/admin/hotels/{hotel_id}")
async def update_hotel(
    request: Request,
    hotel_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Update a hotel (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    data = await request.json()
    result = await actor.update_hotel.remote(hotel_id, data)
    return JSONResponse(result)


@bp.delete("/api/v1/admin/hotels/{hotel_id}")
async def delete_hotel(
    request: Request,
    hotel_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Delete a hotel (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    result = await actor.delete_hotel.remote(hotel_id)
    return JSONResponse(result)


# ==============================================================================
# Event Routes
# ==============================================================================

@bp.get("/api/v1/events/")
async def list_events(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """List published events (public endpoint)."""
    events = await actor.list_events.remote(published_only=True, admin_view=False)
    return JSONResponse(events)


@bp.get("/api/v1/admin/events/")
async def list_all_events(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """List all events (admin endpoint)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    events = await actor.list_events.remote(published_only=False, admin_view=True)
    return JSONResponse(events)


@bp.get("/api/v1/events/{event_id}")
async def get_event_details(
    request: Request,
    event_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get event details (public endpoint)."""
    event = await actor.get_event_details.remote(event_id)
    return JSONResponse(event)


@bp.post("/api/v1/events/")
async def create_event(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Create an event (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    user_id = get_user_id_for_actor(user)
    data = await request.json()
    event = await actor.create_event.remote(user_id, data)
    return JSONResponse(event, status_code=201)


@bp.put("/api/v1/events/{event_id}")
async def update_event(
    request: Request,
    event_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Update an event (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    data = await request.json()
    result = await actor.update_event.remote(event_id, data)
    return JSONResponse(result)


@bp.delete("/api/v1/events/{event_id}")
async def delete_event(
    request: Request,
    event_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Delete an event (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    result = await actor.delete_event.remote(event_id)
    return JSONResponse(result)


# ==============================================================================
# Booking Routes
# ==============================================================================

@bp.post("/api/v1/bookings/checkout")
async def checkout(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Process checkout."""
    user = await get_user_from_request(request)
    user_id = get_user_id_for_actor(user)
    
    data = await request.json()
    booking = await actor.checkout.remote(user_id, data)
    return JSONResponse(booking, status_code=201)


@bp.get("/api/v1/bookings")
async def get_my_bookings(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get user's bookings."""
    user = await get_user_from_request(request)
    user_id = get_user_id_for_actor(user)
    
    bookings = await actor.get_user_bookings.remote(user_id)
    return JSONResponse(bookings)


@bp.get("/api/v1/bookings/{booking_id}/tickets")
async def get_tickets_for_booking(
    request: Request,
    booking_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get tickets for a booking."""
    user = await get_user_from_request(request)
    user_id = get_user_id_for_actor(user)
    is_admin = user.get("role") == "admin"
    
    tickets = await actor.get_booking_tickets.remote(booking_id, user_id, is_admin)
    return JSONResponse(tickets)


# ==============================================================================
# Check-in Routes
# ==============================================================================

@bp.post("/api/v1/checkin")
async def checkin_ticket(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Check in a ticket (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    data = await request.json()
    ticket_id = data.get('ticket_id')
    if not ticket_id:
        raise HTTPException(status_code=400, detail="ticket_id is required")
    
    result = await actor.check_in_ticket_service.remote(ticket_id)
    return JSONResponse(result)


# ==============================================================================
# QR Code Routes
# ==============================================================================

@bp.get("/api/v1/tickets/{ticket_id}/qrcode")
async def get_ticket_qrcode(
    request: Request,
    ticket_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Get QR code for a ticket (owner only)."""
    user = await get_user_from_request(request)
    user_id = get_user_id_for_actor(user)
    
    try:
        qr_code_data = await actor.generate_ticket_qr_code.remote(ticket_id, user_id)
        return JSONResponse({"qr_code": qr_code_data})
    except Exception as e:
        logger.error(f"QR code generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate QR code: {str(e)}")


@bp.post("/api/v1/verify-qrcode")
async def verify_qrcode(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Verify a QR code and return ticket information (owner only for check-in)."""
    user = await get_user_from_request(request)
    # Only owners (admins) can verify QR codes for check-in, not buyers
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Owners only! Only event owners can scan tickets.")
    
    data = await request.json()
    qr_data = data.get('qr_data')
    if not qr_data:
        raise HTTPException(status_code=400, detail="qr_data is required")
    
    try:
        ticket_info = await actor.verify_ticket_qr_code.remote(qr_data)
        return JSONResponse(ticket_info)
    except Exception as e:
        logger.error(f"QR code verification error: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Invalid QR code: {str(e)}")


# ==============================================================================
# Debug/Admin Routes
# ==============================================================================

@bp.post("/api/v1/debug/reinitialize")
async def debug_reinitialize(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Debug endpoint to manually trigger initialization (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    try:
        result = await actor.initialize.remote()
        return JSONResponse({"msg": "Initialization triggered", "result": result})
    except Exception as e:
        logger.error(f"Reinitialize error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Reinitialize failed: {e}")


@bp.get("/api/v1/debug/status")
async def debug_status(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Debug endpoint to check database status (admin only)."""
    user = await get_user_from_request(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only! Access forbidden.")
    
    try:
        status = await actor.get_debug_status.remote()
        return JSONResponse(status)
    except Exception as e:
        logger.error(f"Debug status error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
