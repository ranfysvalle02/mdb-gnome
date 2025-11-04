"""
Store Factory Experiment
FastAPI routes that delegate to the Ray Actor.
"""

import logging
import ray
from fastapi import APIRouter, Request, HTTPException, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from starlette import status
from typing import Optional, Dict, Any
import json
import datetime

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


async def get_experiment_user_from_session(request: Request) -> Optional[Dict[str, Any]]:
    """
    Helper dependency to get experiment user from sub-auth session.
    
    StoreFactory uses experiment_users strategy (sub-auth only, no platform linking).
    This means users must log in directly within StoreFactory to access admin features.
    
    Usage in routes:
        @bp.get("/admin/dashboard")
        async def admin_dashboard(
            user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
        ):
            if not user:
                return RedirectResponse(url="/admin/login")
            # User is authenticated via sub-auth
    
    Returns:
        None if not authenticated
        Dict with user info (email, _id, etc.) if authenticated via sub-auth
    """
    try:
        from sub_auth import get_experiment_sub_user
        from core_deps import get_experiment_config, get_scoped_db
        
        slug_id = getattr(request.state, "slug_id", "store_factory")
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        # Check if sub-auth is enabled
        sub_auth = config.get("sub_auth", {}) if config else {}
        if not sub_auth.get("enabled", False):
            return None
        
        db = await get_scoped_db(request)
        user = await get_experiment_sub_user(request, slug_id, db, config)
        return user
    except Exception as e:
        logger.error(f"Error getting experiment user from session: {e}", exc_info=True)
        return None


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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Display the store's homepage."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_store_home.remote(store_slug, context, user)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_store_home: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.get("/{store_slug}/item/{item_id}", response_class=HTMLResponse)
async def item_details(
    request: Request,
    store_slug: str,
    item_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Display details for a single item."""
    context = {"url": str(request.url), "path": request.url.path}
    try:
        html = await actor.render_item_details.remote(store_slug, item_id, context, user)
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
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    error: Optional[str] = Query(None),
    email: Optional[str] = Query(None)
):
    """Display admin login page."""
    # Check if already authenticated
    user = await get_experiment_user_from_session(request)
    if user:
        return RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}/admin/dashboard",
            status_code=status.HTTP_303_SEE_OTHER
        )
    
    # Render login template
    try:
        html = await actor.render_admin_login.remote(store_slug, {"url": str(request.url), "path": request.url.path}, error, email)
        return HTMLResponse(html)
    except Exception as e:
        logger.error(f"Actor call failed for render_admin_login: {e}", exc_info=True)
        return HTMLResponse(f"<h1>Actor Error</h1><pre>{e}</pre>", status_code=500)


@bp.post("/{store_slug}/admin/login")
async def admin_login_post(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    email: str = Form(...),
    password: str = Form(...)
):
    """Handle admin login using sub-authentication."""
    try:
        result = await actor.admin_login.remote(store_slug, email, password)
        if result.get("success"):
            # Use sub-authentication to create session
            from sub_auth import create_experiment_session
            from core_deps import get_experiment_config, get_scoped_db
            
            slug_id = getattr(request.state, "slug_id", "store_factory")
            config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
            db = await get_scoped_db(request)
            
            user_id = result.get("user_id")
            response = RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/admin/dashboard",
                status_code=status.HTTP_303_SEE_OTHER
            )
            
            # Create experiment session
            await create_experiment_session(request, slug_id, user_id, config, response)
            return response
        else:
            return RedirectResponse(
                url=f"/experiments/store_factory/{store_slug}/admin/login?error={result.get('error', 'Invalid credentials')}",
                status_code=status.HTTP_303_SEE_OTHER
            )
    except Exception as e:
        logger.error(f"Actor call failed for admin_login: {e}", exc_info=True)
        raise HTTPException(500, f"Actor failed to process login: {e}")


@bp.post("/{store_slug}/admin/logout")
async def admin_logout(
    request: Request,
    store_slug: str
):
    """Handle admin logout - clears sub-auth session."""
    try:
        from sub_auth import get_experiment_sub_user
        from core_deps import get_experiment_config, get_scoped_db
        from fastapi.responses import Response
        
        slug_id = getattr(request.state, "slug_id", "store_factory")
        config = await get_experiment_config(request, slug_id, {"sub_auth": 1})
        
        # Get session cookie name from config
        sub_auth = config.get("sub_auth", {}) if config else {}
        cookie_name = sub_auth.get("session_cookie_name", f"{slug_id}_session")
        
        # Create response that clears the cookie
        response = RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}",
            status_code=status.HTTP_303_SEE_OTHER
        )
        
        # Clear the session cookie
        response.delete_cookie(
            key=cookie_name,
            path="/",
            httponly=True,
            samesite="lax"
        )
        
        return response
    except Exception as e:
        logger.error(f"Error during logout: {e}", exc_info=True)
        # Still redirect even if logout fails
        return RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}",
            status_code=status.HTTP_303_SEE_OTHER
        )


@bp.get("/{store_slug}/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Display admin dashboard - requires sub-auth session."""
    # Check if user is authenticated via sub-auth
    if not user:
        return RedirectResponse(
            url=f"/experiments/store_factory/{store_slug}/admin/login",
            status_code=status.HTTP_303_SEE_OTHER
        )
    
    # User is authenticated via sub-auth, proceed with dashboard
    context = {"url": str(request.url), "path": request.url.path}
    try:
        store = await actor.get_store_by_slug.remote(store_slug)
        if not store:
            return HTMLResponse(f"<h1>404</h1><p>Store '{store_slug}' not found.</p>", status_code=404)
        
        # Get slideshow images for dashboard
        slideshow_images = await actor.get_slideshow_images.remote(store_slug)
        
        # Simple HTML dashboard with slideshow management
        html = f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Admin Dashboard - {store.get('name', store_slug)}</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
        </head>
        <body class="bg-gray-900 text-white min-h-screen">
            <div class="container mx-auto px-6 py-10">
                <div class="flex justify-between items-center mb-8">
                    <h1 class="text-4xl font-bold">Admin Dashboard - {store.get('name', store_slug)}</h1>
                    <div class="flex items-center space-x-4">
                        <span class="text-gray-400">Logged in as: {user.get('email', 'Admin')}</span>
                        <form action="/experiments/store_factory/{store_slug}/admin/logout" method="POST" class="inline">
                            <button type="submit" class="px-4 py-2 bg-red-600 hover:bg-red-700 text-white font-bold rounded-lg">
                                Logout
                            </button>
                        </form>
                    </div>
                </div>
                
                <!-- Slideshow Management -->
                <div class="bg-gray-800 border border-gray-700 rounded-lg p-8 mb-6">
                    <h2 class="text-2xl font-bold mb-6">
                        <i class="fas fa-images mr-2"></i>
                        Slideshow Management
                    </h2>
                    
                    <div class="mb-6">
                        <h3 class="text-xl font-semibold mb-4">Add New Slide</h3>
                        <form id="slideshow-add-form" class="space-y-4">
                            <div>
                                <label for="slide_image_url" class="block text-sm font-medium mb-2">Image URL</label>
                                <input type="text" id="slide_image_url" name="image_url" required
                                       placeholder="https://example.com/image.jpg"
                                       class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                <p class="text-sm text-gray-400 mt-2">Enter a URL to your image (supports Unsplash, Pexels, etc.)</p>
                            </div>
                            <div>
                                <label for="slide_caption" class="block text-sm font-medium mb-2">Caption (Optional)</label>
                                <input type="text" id="slide_caption" name="caption"
                                       placeholder="Enter caption text"
                                       class="w-full px-4 py-2 bg-gray-700 border border-gray-600 text-white rounded-lg">
                                <p class="text-sm text-gray-400 mt-2">Caption will be displayed on the slide</p>
                            </div>
                            <button type="submit" class="px-6 py-2 bg-green-600 hover:bg-green-700 text-white font-bold rounded-lg">
                                <i class="fas fa-plus mr-2"></i>
                                Add Slide
                            </button>
                            <div id="slideshow-add-message" class="mt-4"></div>
                        </form>
                    </div>
                    
                    <div class="border-t border-gray-700 pt-6 mt-6">
                        <h3 class="text-xl font-semibold mb-4">Current Slides</h3>
                        <div id="slideshow-list" class="space-y-4">
                            <div class="text-gray-400 text-center py-8">Loading slides...</div>
                        </div>
                    </div>
                </div>
                
                <!-- Store Settings -->
                <div class="bg-gray-800 border border-gray-700 rounded-lg p-8 mb-6">
                    <h2 class="text-2xl font-bold mb-6">
                        <i class="fas fa-cog mr-2"></i>
                        Store Settings
                    </h2>
                    
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
                        <i class="fas fa-arrow-left mr-2"></i>
                        Back to Store
                    </a>
                </div>
            </div>
            
            <script>
                // Logo management
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
                
                // Slideshow management
                let slideshowImages = [];
                
                async function loadSlideshow() {{
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/slideshow');
                        const result = await response.json();
                        if (result.success) {{
                            slideshowImages = result.images || [];
                            renderSlideshowList();
                        }}
                    }} catch (error) {{
                        console.error('Error loading slideshow:', error);
                        document.getElementById('slideshow-list').innerHTML = '<div class="text-red-400">Error loading slideshow images.</div>';
                    }}
                }}
                
                function renderSlideshowList() {{
                    const listDiv = document.getElementById('slideshow-list');
                    if (slideshowImages.length === 0) {{
                        listDiv.innerHTML = '<div class="text-gray-400 text-center py-8">No slideshow images yet. Add your first slide above!</div>';
                        return;
                    }}
                    
                    let html = '<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" id="slideshow-sortable">';
                    slideshowImages.forEach((img, index) => {{
                        const imgId = img._id?.$oid || img._id || '';
                        html += `
                            <div class="bg-gray-700 border border-gray-600 rounded-lg p-4 relative" data-id="${{imgId}}">
                                <div class="flex items-center justify-between mb-2">
                                    <span class="text-sm font-semibold text-gray-300">Slide ${{index + 1}}</span>
                                    <div class="flex gap-2">
                                        <button onclick="moveSlide(${{index}}, 'up')" ${{index === 0 ? 'disabled' : ''}} class="text-gray-400 hover:text-white ${{index === 0 ? 'opacity-50 cursor-not-allowed' : ''}}">
                                            <i class="fas fa-arrow-up"></i>
                                        </button>
                                        <button onclick="moveSlide(${{index}}, 'down')" ${{index === slideshowImages.length - 1 ? 'disabled' : ''}} class="text-gray-400 hover:text-white ${{index === slideshowImages.length - 1 ? 'opacity-50 cursor-not-allowed' : ''}}">
                                            <i class="fas fa-arrow-down"></i>
                                        </button>
                                        <button onclick="deleteSlide('${{imgId}}')" class="text-red-400 hover:text-red-300">
                                            <i class="fas fa-trash"></i>
                                        </button>
                                    </div>
                                </div>
                                <img src="${{img.image_url}}" alt="Slide" class="w-full h-32 object-cover rounded mb-2">
                                <p class="text-sm text-gray-400 truncate">${{(img.caption || 'No caption').replace(/'/g, "\\'")}}</p>
                            </div>
                        `;
                    }});
                    html += '</div>';
                    listDiv.innerHTML = html;
                }}
                
                async function moveSlide(index, direction) {{
                    if ((direction === 'up' && index === 0) || (direction === 'down' && index === slideshowImages.length - 1)) {{
                        return;
                    }}
                    
                    const newIndex = direction === 'up' ? index - 1 : index + 1;
                    const temp = slideshowImages[index];
                    slideshowImages[index] = slideshowImages[newIndex];
                    slideshowImages[newIndex] = temp;
                    
                    const imageOrders = slideshowImages.map(img => ({{
                        image_id: img._id?.$oid || img._id || ''
                    }}));
                    
                    try {{
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/slideshow/update-order', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/json',
                            }},
                            body: JSON.stringify(imageOrders)
                        }});
                        
                        const result = await response.json();
                        if (result.success) {{
                            renderSlideshowList();
                        }} else {{
                            alert('Error: ' + (result.error || 'Unknown error'));
                            loadSlideshow(); // Reload on error
                        }}
                    }} catch (error) {{
                        alert('Error: ' + error.message);
                        loadSlideshow(); // Reload on error
                    }}
                }}
                
                async function deleteSlide(imageId) {{
                    if (!confirm('Are you sure you want to delete this slide?')) {{
                        return;
                    }}
                    
                    try {{
                        const response = await fetch(`/experiments/store_factory/{store_slug}/admin/slideshow/${{imageId}}/delete`, {{
                            method: 'POST'
                        }});
                        
                        const result = await response.json();
                        if (result.success) {{
                            loadSlideshow();
                        }} else {{
                            alert('Error: ' + (result.error || 'Unknown error'));
                        }}
                    }} catch (error) {{
                        alert('Error: ' + error.message);
                    }}
                }}
                
                document.getElementById('slideshow-add-form').addEventListener('submit', async function(e) {{
                    e.preventDefault();
                    const imageUrl = document.getElementById('slide_image_url').value;
                    const caption = document.getElementById('slide_caption').value;
                    const messageDiv = document.getElementById('slideshow-add-message');
                    
                    try {{
                        const formData = new URLSearchParams();
                        formData.append('image_url', imageUrl);
                        if (caption) {{
                            formData.append('caption', caption);
                        }}
                        
                        const response = await fetch('/experiments/store_factory/{store_slug}/admin/slideshow/add', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded',
                            }},
                            body: formData
                        }});
                        
                        const result = await response.json();
                        
                        if (result.success) {{
                            messageDiv.innerHTML = '<p class="text-green-400">' + result.message + '</p>';
                            document.getElementById('slideshow-add-form').reset();
                            loadSlideshow();
                        }} else {{
                            messageDiv.innerHTML = '<p class="text-red-400">Error: ' + (result.error || 'Unknown error') + '</p>';
                        }}
                    }} catch (error) {{
                        messageDiv.innerHTML = '<p class="text-red-400">Error: ' + error.message + '</p>';
                    }}
                }});
                
                // Load slideshow on page load
                loadSlideshow();
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
    logo_url: str = Form(...),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Handle logo update."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.update_store_logo.remote(store_slug, logo_url)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_store_logo: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to update logo: {e}"}, status_code=500)


# --- Slideshow Management Routes ---

@bp.get("/{store_slug}/admin/slideshow", response_class=JSONResponse)
async def get_slideshow_images(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Get all slideshow images for a store."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        images = await actor.get_slideshow_images.remote(store_slug)
        return JSONResponse({"success": True, "images": images})
    except Exception as e:
        logger.error(f"Actor call failed for get_slideshow_images: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to get slideshow images: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/slideshow/add", response_class=JSONResponse)
async def add_slideshow_image(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    image_url: str = Form(...),
    caption: Optional[str] = Form(None),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Add a new slideshow image."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.add_slideshow_image.remote(store_slug, image_url, caption)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for add_slideshow_image: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to add slideshow image: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/slideshow/{image_id}/delete", response_class=JSONResponse)
async def delete_slideshow_image(
    request: Request,
    store_slug: str,
    image_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Delete a slideshow image."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.delete_slideshow_image.remote(store_slug, image_id)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for delete_slideshow_image: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to delete slideshow image: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/slideshow/update-order", response_class=JSONResponse)
async def update_slideshow_order(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Update the order of slideshow images."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        from fastapi import Body
        image_orders = await request.json()
        if not isinstance(image_orders, list):
            return JSONResponse({"success": False, "error": "Invalid request format"}, status_code=400)
        
        result = await actor.update_slideshow_order.remote(store_slug, image_orders)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_slideshow_order: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to update slideshow order: {e}"}, status_code=500)


@bp.post("/{store_slug}/admin/slideshow/{image_id}/update", response_class=JSONResponse)
async def update_slideshow_image(
    request: Request,
    store_slug: str,
    image_id: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle),
    image_url: Optional[str] = Form(None),
    caption: Optional[str] = Form(None),
    user: Optional[Dict[str, Any]] = Depends(get_experiment_user_from_session)
):
    """Update a slideshow image."""
    if not user:
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        result = await actor.update_slideshow_image.remote(store_slug, image_id, image_url, caption)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Actor call failed for update_slideshow_image: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Actor failed to update slideshow image: {e}"}, status_code=500)


# --- Store Export/Download Routes ---

@bp.get("/{store_slug}/download")
async def download_store(
    request: Request,
    store_slug: str,
    actor: "ray.actor.ActorHandle" = Depends(get_actor_handle)
):
    """Download store data as JSON file."""
    try:
        result = await actor.export_store_data.remote(store_slug)
        if not result.get("success"):
            return JSONResponse(result, status_code=404)
        
        export_data = result.get("data", {})
        store_name = export_data.get("export_metadata", {}).get("store_name", store_slug)
        
        # Create a safe filename
        safe_name = "".join(c for c in store_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{safe_name}_{store_slug}_{timestamp}.json"
        
        # Convert to JSON string with pretty formatting
        json_str = json.dumps(export_data, indent=2, ensure_ascii=False)
        
        return Response(
            content=json_str,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    except Exception as e:
        logger.error(f"Actor call failed for export_store_data: {e}", exc_info=True)
        return JSONResponse({"success": False, "error": f"Failed to export store: {e}"}, status_code=500)

