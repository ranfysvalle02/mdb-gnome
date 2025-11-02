"""
Store Factory Experiment
FastAPI routes that delegate to the Ray Actor.
"""

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette import status
from typing import Optional, Dict, Any

from .actor import ExperimentActor

logger = logging.getLogger(__name__)
bp = APIRouter()


async def get_actor_handle(request: Request) -> "ray.actor.ActorHandle":
    """FastAPI Dependency to get the Store Factory actor handle."""
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


# --- Root Routes ---
@bp.get("/", response_class=HTMLResponse)
async def home(request: Request, actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)):
    """Default to business selection page."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_business_selection.remote(context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_business_selection: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/create", response_class=HTMLResponse)
async def create_store_flow(request: Request, actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)):
    """Business type selection page for creating a new store."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_business_selection.remote(context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_business_selection: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/select/{business_type}", response_class=HTMLResponse)
async def select_business(
    request: Request,
    business_type: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Show store selection/creation page for a business type."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_store_selection.remote(business_type, context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_store_selection: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/create/{business_type}")
async def create_store_post(
    request: Request,
    business_type: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    name: str = Form(...),
    slug_id: str = Form(...),
    tagline: Optional[str] = Form(None),
    about_text: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    hours: Optional[str] = Form(None),
    email: str = Form(...),
    password: str = Form(...)
):
    """Handle store creation."""
    form_data = {
        "name": name,
        "slug_id": slug_id,
        "tagline": tagline,
        "about_text": about_text,
        "address": address,
        "phone": phone,
        "hours": hours,
        "email": email,
        "password": password
    }
    
    try:
        result = await actor.create_store.remote(business_type, form_data)
        if result.get("success"):
            # Redirect to store home
            return RedirectResponse(
                url=f"/experiments/store_factory/{result['store_slug']}",
                status_code=status.HTTP_303_SEE_OTHER
            )
        else:
            # Redirect back with error (could use flash messages in future)
            return RedirectResponse(
                url=f"/experiments/store_factory/select/{business_type}?error={result.get('error', 'Unknown error')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
    except Exception as e:
        logger.error(f"Actor call failed for create_store: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to create store: {e}")


@bp.get("/stores", response_class=HTMLResponse)
async def list_stores(
    request: Request,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    business_type: Optional[str] = Query(None)
):
    """List all stores, optionally filtered by business type."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_store_list.remote(context, business_type)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_store_list: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


# --- Store Routes ---
@bp.get("/{store_slug}", response_class=HTMLResponse)
async def store_home(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Display the store's homepage."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_store_home.remote(store_slug, context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_store_home: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/{store_slug}/item/{item_id}", response_class=HTMLResponse)
async def item_details(
    request: Request,
    store_slug: str,
    item_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Display details for a single item."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_item_details.remote(store_slug, item_id, context)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_item_details: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/{store_slug}/inquire/{item_id}")
async def submit_inquiry_post(
    request: Request,
    store_slug: str,
    item_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    customer_name: str = Form(...),
    customer_contact: str = Form(...),
    message: Optional[str] = Form(None)
):
    """Handle the submission of a new customer inquiry."""
    form_data = {
        "customer_name": customer_name,
        "customer_contact": customer_contact,
        "message": message
    }
    
    try:
        result = await actor.submit_inquiry.remote(store_slug, item_id, form_data)
        if result.get("success"):
            return RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/item/{item_id}",
                status_code=status.HTTP_303_SEE_OTHER
            )
        else:
            return RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/item/{item_id}?error={result.get('error', 'Unknown error')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
    except Exception as e:
        logger.error(f"Actor call failed for submit_inquiry: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to submit inquiry: {e}")


# --- Admin Routes ---
@bp.get("/{store_slug}/admin/login", response_class=HTMLResponse)
async def admin_login_get(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Display admin login page."""
    # TODO: Render login template
    return HTMLResponse(f"<h1>Admin Login for {store_slug}</h1><p>Login form will be rendered here</p>")


@bp.post("/{store_slug}/admin/login")
async def admin_login_post(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    email: str = Form(...),
    password: str = Form(...)
):
    """Handle admin login."""
    try:
        result = await actor.admin_login.remote(store_slug, email, password)
        if result.get("success"):
            # TODO: Set session cookies
            return RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/admin/dashboard",
                status_code=status.HTTP_303_SEE_OTHER
            )
        else:
            return RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/admin/login?error={result.get('error', 'Invalid credentials')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
    except Exception as e:
        logger.error(f"Actor call failed for admin_login: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to process login: {e}")


@bp.get("/{store_slug}/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Display admin dashboard."""
    # TODO: Add authentication check once session management is implemented
    context = {"url": str(request.url), "path": request.url.path}
    try:
        store = await actor.get_store_by_slug.remote(store_slug)
        if not store:
            return HTMLResponse(f"<h1>404</h1><p>Store '{store_slug}' not found.</p>", status_code=404)
        
        # Simple HTML dashboard for now
        html = f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Admin Dashboard - {store.get('name', store_slug)}</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gray-900 text-white min-h-screen">
            <div class="container mx-auto px-6 py-10">
                <h1 class="text-4xl font-bold mb-8">Admin Dashboard - {store.get('name', store_slug)}</h1>
                
                <div class="bg-gray-800 border border-gray-700 rounded-lg p-8 mb-6">
                    <h2 class="text-2xl font-bold mb-6">Store Settings</h2>
                    
                    <div class="space-y-6">
                        <div>
                            <h3 class="text-xl font-semibold mb-4">Update Logo</h3>
                            <form id="logo-form" class="space-y-4">
                                <div>
                                    <label for="logo_url" class="block text-sm font-medium mb-2">Logo URL</label>
                                    <input type="text" id="logo_url" name="logo_url" 
                                           value="{store.get('logo_url', '/experiments/store_factory/static/img/logo.png')}"
                                           class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                    <p class="text-sm text-gray-400 mt-2">Enter a URL or path to your logo image. Default: /experiments/store_factory/static/img/logo.png</p>
                                </div>
                                <div id="logo-preview" class="mt-4">
                                    <p class="text-sm font-medium mb-2">Preview:</p>
                                    <img src="{store.get('logo_url', '/experiments/store_factory/static/img/logo.png')}" 
                                         alt="Logo Preview" 
                                         class="h-20 w-auto border border-gray-600 rounded">
                                </div>
                                <button type="submit" class="px-6 py-2 bg-blue-600 hover:bg-blue-700 text-white font-bold rounded-lg">
                                    Update Logo
                                </button>
                                <div id="logo-message" class="mt-4"></div>
                            </form>
                        </div>
                    </div>
                </div>
                
                <div class="mt-6">
                    <a href="/experiments/store_factory/{store_slug}" class="text-blue-400 hover:underline">
                        ← Back to Store
                    </a>
                </div>
            </div>
            
            <script>
                document.getElementById('logo_url').addEventListener('input', function(e) {{
                    document.getElementById('logo-preview').querySelector('img').src = e.target.value;
                }});
                
                document.getElementById('logo-form').addEventListener('submit', async function(e) {{
                    e.preventDefault();
                    const logoUrl = document.getElementById('logo_url').value;
                    const messageDiv = document.getElementById('logo-message');
                    
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/update-logo', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                            }},
                            body: 'logo_url=' + encodeURIComponent(logoUrl)
                        }});
                        
                        const result = await response.json();
                        
                        if (result.success) {{
                            messageDiv.innerHTML = '<p class="text-green-400">' + result.message + '</p>';
                        }} else {{
                            messageDiv.innerHTML = '<p class="text-red-400">Error: ' + (result.error || 'Unknown error') + '</p>';
                        }}
                    }} catch (error) {{
                        messageDiv.innerHTML = '<p class="text-red-400">Error: ' + error.message + '</p>';
                    }}
                }});
            </script>
        </body>
        </html>
        """
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for admin_dashboard: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/{store_slug}/admin/update-logo", response_class=JSONResponse)
async def update_logo_post(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    logo_url: str = Form(...)
):
    """Handle logo update."""
    # TODO: Add authentication check once session management is implemented
    try:
        result = await actor.update_store_logo.remote(store_slug, logo_url)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_store_logo: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to update logo: {e}"}, status_code=500)

