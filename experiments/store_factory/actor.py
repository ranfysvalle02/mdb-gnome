"""
Store Factory Actor
A Ray Actor that handles all store factory operations.
Adapted from the original Flask multi-business-type store application.
"""

import logging
import datetime
import json
import re
import pathlib
from typing import Dict, Any, List, Optional
import ray
from bson.objectid import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

# Actor-local paths
experiment_dir = pathlib.Path(__file__).parent
templates_dir = experiment_dir / "templates"

# --- Business Type Configurations ---
BUSINESS_TYPES = {
    'restaurant': {
        'name': 'Restaurant',
        'item_label': 'Menu Item',
        'item_plural': 'Menu Items',
        'item_code_label': 'Item Code',
        'item_code_prefix': 'MENU',
        'section_label': 'Menu',
        'inquiry_label': 'Order/Inquiry',
        'attributes_template': [
            {'name': 'Category', 'type': 'text', 'required': True},
            {'name': 'Ingredients', 'type': 'text', 'required': False},
            {'name': 'Allergens', 'type': 'text', 'required': False},
        ],
        'status_options': ['Available', 'Sold Out'],
        'hero_text': 'Delicious Food & Great Service',
        'default_description': 'Delicious dish prepared with the finest ingredients.'
    },
    'auto-sales': {
        'name': 'Auto Sales',
        'item_label': 'Vehicle',
        'item_plural': 'Vehicles',
        'item_code_label': 'VIN/SKU',
        'item_code_prefix': 'VIN',
        'section_label': 'Inventory',
        'inquiry_label': 'Inquiry',
        'attributes_template': [
            {'name': 'Make', 'type': 'text', 'required': True},
            {'name': 'Model', 'type': 'text', 'required': True},
            {'name': 'Year', 'type': 'number', 'required': True},
            {'name': 'Mileage', 'type': 'number', 'required': False},
            {'name': 'Color', 'type': 'text', 'required': False},
            {'name': 'Condition', 'type': 'text', 'required': True},
        ],
        'status_options': ['Available', 'Pending', 'Sold'],
        'hero_text': 'Quality Vehicles at Great Prices',
        'default_description': 'Well-maintained vehicle ready for you.'
    },
    'auto-services': {
        'name': 'Auto Services',
        'item_label': 'Service',
        'item_plural': 'Services',
        'item_code_label': 'Service Code',
        'item_code_prefix': 'SRV',
        'section_label': 'Services',
        'inquiry_label': 'Appointment Request',
        'attributes_template': [
            {'name': 'Service Type', 'type': 'text', 'required': True},
            {'name': 'Duration', 'type': 'text', 'required': False},
            {'name': 'Warranty', 'type': 'text', 'required': False},
            {'name': 'Includes', 'type': 'textarea', 'required': False},
        ],
        'status_options': ['Available', 'Limited Availability'],
        'hero_text': 'Professional Auto Services',
        'default_description': 'Quality service performed by experienced technicians.'
    },
    'other-services': {
        'name': 'Other Services',
        'item_label': 'Service',
        'item_plural': 'Services',
        'item_code_label': 'Service Code',
        'item_code_prefix': 'SRV',
        'section_label': 'Services',
        'inquiry_label': 'Service Request',
        'attributes_template': [
            {'name': 'Service Type', 'type': 'text', 'required': True},
            {'name': 'Duration', 'type': 'text', 'required': False},
            {'name': 'What\'s Included', 'type': 'textarea', 'required': False},
        ],
        'status_options': ['Available', 'Limited Availability'],
        'hero_text': 'Quality Services You Can Trust',
        'default_description': 'Professional service tailored to your needs.'
    },
    'generic-store': {
        'name': 'Generic Store',
        'item_label': 'Product',
        'item_plural': 'Products',
        'item_code_label': 'SKU',
        'item_code_prefix': 'SKU',
        'section_label': 'Inventory',
        'inquiry_label': 'Inquiry',
        'attributes_template': [
            {'name': 'Category', 'type': 'text', 'required': True},
            {'name': 'Brand', 'type': 'text', 'required': False},
            {'name': 'Color', 'type': 'text', 'required': False},
            {'name': 'Size', 'type': 'text', 'required': False},
            {'name': 'Material', 'type': 'text', 'required': False},
        ],
        'status_options': ['Available', 'Pending', 'Sold'],
        'hero_text': 'Quality Products You\'ll Love',
        'default_description': 'High-quality product available now.'
    }
}


@ray.remote
class ExperimentActor:
    """
    Store Factory Ray Actor.
    Handles all store factory operations using the experiment database abstraction.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
        self.write_scope = write_scope
        self.read_scopes = read_scopes
        
        # Lazy-load heavy dependencies
        try:
            from fastapi.templating import Jinja2Templates
            
            if templates_dir.is_dir():
                self.templates = Jinja2Templates(directory=str(templates_dir))
            else:
                self.templates = None
                logger.warning(f"[{write_scope}-Actor] Template dir not found at {templates_dir}")
            
            logger.info(f"[{write_scope}-Actor] Successfully loaded dependencies.")
        except ImportError as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to load dependencies: {e}", exc_info=True)
            self.templates = None
        
        # Database initialization
        try:
            from experiment_db import create_actor_database
            self.db = create_actor_database(
                mongo_uri,
                db_name,
                write_scope,
                read_scopes
            )
            logger.info(
                f"[{write_scope}-Actor] initialized with write_scope='{self.write_scope}' "
                f"(DB='{db_name}') using magical database abstraction"
            )
        except Exception as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to init DB: {e}")
            self.db = None

    def _check_ready(self):
        """Check if actor is ready."""
        if not self.db:
            raise RuntimeError("Database not initialized. Check logs for import errors.")
        if not self.templates:
            raise RuntimeError("Templates not loaded. Check logs for import errors.")

    async def seed_default_data(self, store_id: ObjectId, business_type: str):
        """Seed default demo items and specials for a new store."""
        self._check_ready()
        business_config = BUSINESS_TYPES.get(business_type, BUSINESS_TYPES['generic-store'])
        now = datetime.datetime.utcnow()
        
        # Default items based on business type
        default_items = []
        if business_type == 'restaurant':
            default_items = [
                {
                    "name": "Classic Burger",
                    "item_code": f"{business_config['item_code_prefix']}-001",
                    "price": 12.99,
                    "description": "Juicy beef patty with fresh lettuce, tomato, and our special sauce on a toasted bun.",
                    "status": "Available",
                    "attributes": {"Category": "Main Course", "Ingredients": "Beef, Lettuce, Tomato, Cheese", "Allergens": "Gluten, Dairy"},
                    "image_url": "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Margherita Pizza",
                    "item_code": f"{business_config['item_code_prefix']}-002",
                    "price": 15.99,
                    "description": "Traditional Italian pizza with fresh mozzarella, basil, and tomato sauce.",
                    "status": "Available",
                    "attributes": {"Category": "Main Course", "Ingredients": "Dough, Mozzarella, Basil, Tomato Sauce", "Allergens": "Gluten, Dairy"},
                    "image_url": "https://images.unsplash.com/photo-1574071318508-1cdbab80d002?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Caesar Salad",
                    "item_code": f"{business_config['item_code_prefix']}-003",
                    "price": 9.99,
                    "description": "Fresh romaine lettuce with Caesar dressing, parmesan cheese, and croutons.",
                    "status": "Available",
                    "attributes": {"Category": "Salad", "Ingredients": "Romaine, Caesar Dressing, Parmesan, Croutons", "Allergens": "Dairy, Gluten"},
                    "image_url": "https://images.unsplash.com/photo-1546793665-c74683f339c1?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Chocolate Lava Cake",
                    "item_code": f"{business_config['item_code_prefix']}-004",
                    "price": 7.99,
                    "description": "Warm chocolate cake with a molten center, served with vanilla ice cream.",
                    "status": "Available",
                    "attributes": {"Category": "Dessert", "Ingredients": "Chocolate, Flour, Eggs, Butter", "Allergens": "Gluten, Dairy, Eggs"},
                    "image_url": "https://images.unsplash.com/photo-1606313564200-e75d5e30476c?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Fresh Lemonade",
                    "item_code": f"{business_config['item_code_prefix']}-005",
                    "price": 4.99,
                    "description": "Refreshing homemade lemonade made with fresh lemons.",
                    "status": "Available",
                    "attributes": {"Category": "Beverage", "Ingredients": "Lemons, Sugar, Water", "Allergens": "None"},
                    "image_url": "https://images.unsplash.com/photo-1523677011783-c91d1bbe2dcf?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                }
            ]
        elif business_type == 'auto-sales':
            default_items = [
                {
                    "name": "2020 Honda Accord",
                    "item_code": f"{business_config['item_code_prefix']}-001",
                    "price": 24999.00,
                    "description": "Well-maintained sedan with low mileage and excellent fuel economy.",
                    "status": "Available",
                    "attributes": {"Make": "Honda", "Model": "Accord", "Year": "2020", "Mileage": "35000", "Color": "Silver", "Condition": "Excellent"},
                    "image_url": "https://images.unsplash.com/photo-1549317661-bd32c8ce0db2?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "2019 Toyota Camry",
                    "item_code": f"{business_config['item_code_prefix']}-002",
                    "price": 22999.00,
                    "description": "Reliable and spacious family sedan with great safety features.",
                    "status": "Available",
                    "attributes": {"Make": "Toyota", "Model": "Camry", "Year": "2019", "Mileage": "42000", "Color": "White", "Condition": "Very Good"},
                    "image_url": "https://images.unsplash.com/photo-1617486496723-e46bd3c9bd9d?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "2021 Ford F-150",
                    "item_code": f"{business_config['item_code_prefix']}-003",
                    "price": 35999.00,
                    "description": "Powerful pickup truck perfect for work or adventure.",
                    "status": "Available",
                    "attributes": {"Make": "Ford", "Model": "F-150", "Year": "2021", "Mileage": "28000", "Color": "Black", "Condition": "Excellent"},
                    "image_url": "https://images.unsplash.com/photo-1533473359331-0135ef1b58bf?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                }
            ]
        elif business_type == 'auto-services':
            default_items = [
                {
                    "name": "Full Service Oil Change",
                    "item_code": f"{business_config['item_code_prefix']}-001",
                    "price": 49.99,
                    "description": "Complete oil change service with premium oil and filter replacement.",
                    "status": "Available",
                    "attributes": {"Service Type": "Maintenance", "Duration": "30 minutes", "Warranty": "3 months", "Includes": "Oil change, filter replacement, fluid check"},
                    "image_url": "https://images.unsplash.com/photo-1581092160562-40aa08e78837?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Brake Inspection & Service",
                    "item_code": f"{business_config['item_code_prefix']}-002",
                    "price": 89.99,
                    "description": "Comprehensive brake inspection and necessary adjustments or repairs.",
                    "status": "Available",
                    "attributes": {"Service Type": "Repair", "Duration": "1-2 hours", "Warranty": "6 months", "Includes": "Inspection, adjustment, pad replacement if needed"},
                    "image_url": "https://images.unsplash.com/photo-1486262715619-67b85e0b08d3?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Engine Diagnostic",
                    "item_code": f"{business_config['item_code_prefix']}-003",
                    "price": 79.99,
                    "description": "Complete engine diagnostic scan to identify any issues.",
                    "status": "Available",
                    "attributes": {"Service Type": "Diagnostic", "Duration": "1 hour", "Warranty": "N/A", "Includes": "Computer scan, report, recommendations"},
                    "image_url": "https://images.unsplash.com/photo-1492144534655-ae79c964c9d7?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                }
            ]
        elif business_type == 'other-services':
            default_items = [
                {
                    "name": "Basic Consultation",
                    "item_code": f"{business_config['item_code_prefix']}-001",
                    "price": 99.00,
                    "description": "One-on-one consultation to discuss your needs and provide recommendations.",
                    "status": "Available",
                    "attributes": {"Service Type": "Consultation", "Duration": "1 hour", "What's Included": "Initial meeting, needs assessment, recommendations"},
                    "image_url": "https://images.unsplash.com/photo-1556761175-5973dc0f32e7?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Standard Service Package",
                    "item_code": f"{business_config['item_code_prefix']}-002",
                    "price": 299.00,
                    "description": "Comprehensive service package tailored to your requirements.",
                    "status": "Available",
                    "attributes": {"Service Type": "Package", "Duration": "2-3 hours", "What's Included": "Full service, follow-up, support"},
                    "image_url": "https://images.unsplash.com/photo-1556761175-b413da4baf72?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Premium Service",
                    "item_code": f"{business_config['item_code_prefix']}-003",
                    "price": 499.00,
                    "description": "Premium service with extended support and priority handling.",
                    "status": "Available",
                    "attributes": {"Service Type": "Premium", "Duration": "4-6 hours", "What's Included": "Premium service, priority support, extended warranty"},
                    "image_url": "https://images.unsplash.com/photo-1552664730-d307ca884978?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                }
            ]
        else:  # generic-store
            default_items = [
                {
                    "name": "Premium Product",
                    "item_code": f"{business_config['item_code_prefix']}-001",
                    "price": 49.99,
                    "description": "High-quality product with excellent value.",
                    "status": "Available",
                    "attributes": {"Category": "General", "Brand": "Premium", "Color": "Black", "Size": "Standard", "Material": "Quality"},
                    "image_url": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Deluxe Edition",
                    "item_code": f"{business_config['item_code_prefix']}-002",
                    "price": 79.99,
                    "description": "Upgraded version with additional features.",
                    "status": "Available",
                    "attributes": {"Category": "Premium", "Brand": "Deluxe", "Color": "Silver", "Size": "Large", "Material": "Premium"},
                    "image_url": "https://images.unsplash.com/photo-1441986300917-64674bd600d8?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                },
                {
                    "name": "Standard Option",
                    "item_code": f"{business_config['item_code_prefix']}-003",
                    "price": 29.99,
                    "description": "Great value option for everyday use.",
                    "status": "Available",
                    "attributes": {"Category": "Standard", "Brand": "Standard", "Color": "Blue", "Size": "Medium", "Material": "Standard"},
                    "image_url": "https://images.unsplash.com/photo-1468495244123-6c6c332eeece?w=800&auto=format&fit=crop",
                    "store_id": store_id,
                    "date_added": now
                }
            ]
        
        # Default specials for all business types
        default_specials = [
            {
                "title": "Grand Opening Special!",
                "content": "Welcome to our store! Check out our amazing selection and get started today. We're excited to serve you!",
                "store_id": store_id,
                "date_created": now
            },
            {
                "title": "New Customer Discount",
                "content": "New customers get 10% off their first order! Mention this special when placing your order.",
                "store_id": store_id,
                "date_created": now
            }
        ]
        
        # Insert items
        if default_items:
            await self.db.items.insert_many(default_items)
        
        # Insert specials
        if default_specials:
            await self.db.specials.insert_many(default_specials)

    async def render_business_selection(self, request_context: Dict[str, Any]) -> str:
        """Render the business type selection page."""
        self._check_ready()
        try:
            response = self.templates.TemplateResponse(
                "business_selection.html",
                {"request": request_context, "BUSINESS_TYPES": BUSINESS_TYPES}
            )
            return response.body.decode("utf-8")
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error rendering business selection: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def render_store_selection(self, business_type: str, request_context: Dict[str, Any]) -> str:
        """Render the store selection/creation page for a business type."""
        self._check_ready()
        
        if business_type not in BUSINESS_TYPES:
            return f"<h1>Error</h1><p>Invalid business type: {business_type}</p>"
        
        # Get existing stores for this business type
        existing_stores = await self.db.stores.find({"business_type": business_type}).sort("name", 1).to_list(length=None)
        
        business_config = BUSINESS_TYPES[business_type]
        try:
            response = self.templates.TemplateResponse(
                "store_selection.html",
                {
                    "request": request_context,
                    "business_type": business_type,
                    "business_type_name": business_config['name'],
                    "existing_stores": existing_stores,
                    "BUSINESS_TYPES": BUSINESS_TYPES
                }
            )
            return response.body.decode("utf-8")
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error rendering store selection: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def render_store_list(self, request_context: Dict[str, Any], business_type: Optional[str] = None) -> str:
        """Render the list of all stores."""
        self._check_ready()
        
        query = {}
        if business_type and business_type in BUSINESS_TYPES:
            query['business_type'] = business_type
        
        stores = await self.db.stores.find(query).sort("name", 1).to_list(length=None)
        
        try:
            response = self.templates.TemplateResponse(
                "store_list.html",
                {"request": request_context, "stores": stores, "BUSINESS_TYPES": BUSINESS_TYPES}
            )
            return response.body.decode("utf-8")
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error rendering store list: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def render_store_home(self, store_slug: str, request_context: Dict[str, Any]) -> str:
        """Render the store homepage."""
        self._check_ready()
        
        store = await self.db.stores.find_one({"slug_id": store_slug})
        if not store:
            return f"<h1>404</h1><p>Store '{store_slug}' not found.</p>"
        
        # Ensure logo_url has a default if None
        if not store.get('logo_url'):
            store['logo_url'] = "/experiments/store_factory/static/img/logo.png"
        
        items = await self.db.items.find({
            "store_id": store['_id'],
            "status": {"$ne": "Sold"}
        }).sort("date_added", -1).limit(12).to_list(length=None)
        
        # Get specials for the store
        specials = await self.db.specials.find({"store_id": store['_id']}).sort("date_created", -1).limit(3).to_list(length=None)
        if specials:
            store['specials'] = specials
        
        business_config = BUSINESS_TYPES.get(store.get('business_type', 'generic-store'), BUSINESS_TYPES['generic-store'])
        
        try:
            response = self.templates.TemplateResponse(
                "store_home.html",
                {
                    "request": request_context,
                    "store": store,
                    "items": items,
                    "business_config": business_config,
                    "now": datetime.datetime.utcnow()
                }
            )
            return response.body.decode("utf-8")
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error rendering store home: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def render_item_details(self, store_slug: str, item_id: str, request_context: Dict[str, Any]) -> str:
        """Render the item details page."""
        self._check_ready()
        
        try:
            item_obj_id = ObjectId(item_id)
        except InvalidId:
            return f"<h1>Error</h1><p>Invalid item ID: {item_id}</p>"
        
        store = await self.db.stores.find_one({"slug_id": store_slug})
        if not store:
            return f"<h1>404</h1><p>Store '{store_slug}' not found.</p>"
        
        # Ensure logo_url has a default if None
        if not store.get('logo_url'):
            store['logo_url'] = "/experiments/store_factory/static/img/logo.png"
        
        item = await self.db.items.find_one({"_id": item_obj_id, "store_id": store['_id']})
        if not item:
            return f"<h1>404</h1><p>Item not found.</p>"
        
        business_config = BUSINESS_TYPES.get(store.get('business_type', 'generic-store'), BUSINESS_TYPES['generic-store'])
        
        try:
            response = self.templates.TemplateResponse(
                "item_details.html",
                {
                    "request": request_context,
                    "store": store,
                    "item": item,
                    "business_config": business_config,
                    "now": datetime.datetime.utcnow()
                }
            )
            return response.body.decode("utf-8")
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error rendering item details: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def create_store(self, business_type: str, form_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new store."""
        self._check_ready()
        
        if business_type not in BUSINESS_TYPES:
            return {"success": False, "error": "Invalid business type selected."}
        
        # Generate slug_id if not provided
        slug_id = form_data.get('slug_id', '').strip().lower()
        if not slug_id:
            # Auto-generate from name
            slug_id = form_data.get('name', '').lower().replace(' ', '-')
            slug_id = ''.join(c for c in slug_id if c.isalnum() or c == '-')
        
        # Validate slug_id
        if not slug_id or not all(c.isalnum() or c == '-' for c in slug_id):
            return {"success": False, "error": "Invalid URL slug. Use only lowercase letters, numbers, and hyphens."}
        
        # Check if slug_id already exists
        existing = await self.db.stores.find_one({"slug_id": slug_id})
        if existing:
            return {"success": False, "error": f'A store with URL slug "{slug_id}" already exists. Please choose a different one.'}
        
        # Create store with theme defaults
        now = datetime.datetime.utcnow()
        store_data = {
            "name": form_data.get('name'),
            "slug_id": slug_id,
            "business_type": business_type,
            "tagline": form_data.get('tagline'),
            "about_text": form_data.get('about_text'),
            "address": form_data.get('address'),
            "phone": form_data.get('phone'),
            "phone_display": form_data.get('phone'),
            "hours": form_data.get('hours'),
            "lang": "en",
            "hero_image_url": None,
            "logo_url": "/experiments/store_factory/static/img/logo.png",  # Default logo
            "gallery_image_1_url": None,
            "gallery_image_2_url": None,
            "menu_image_url": None,
            "google_maps_embed_html": '<iframe src="https://www.google.com/maps?q=Orlando,+FL+32812&output=embed" width="100%" height="100%" style="border:0;" allowfullscreen="" loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe>',
            "inventory_description": None,
            "socials": [],
            # Theme defaults
            "theme_primary": form_data.get('theme_primary', '#3b82f6'),
            "theme_secondary": form_data.get('theme_secondary', '#f59e0b'),
            "theme_background": form_data.get('theme_background', '#111827'),
            "theme_surface": form_data.get('theme_surface', '#1f2937'),
            "theme_text": form_data.get('theme_text', '#f9fafb'),
            "theme_text_secondary": form_data.get('theme_text_secondary', '#d1d5db'),
            "date_created": now
        }
        
        try:
            result = await self.db.stores.insert_one(store_data)
            store_id = result.inserted_id
            
            # Create admin user
            await self.db.users.insert_one({
                "email": form_data.get('email'),
                "password": form_data.get('password'),  # In production, hash this!
                "role": "owner",
                "store_id": store_id,
                "date_created": now
            })
            
            # Seed default demo data
            await self.seed_default_data(store_id, business_type)
            
            return {"success": True, "store_slug": slug_id, "message": f'Store "{store_data["name"]}" created successfully with demo data!'}
        except DuplicateKeyError:
            return {"success": False, "error": f'A store with URL slug "{slug_id}" already exists. Please choose a different one.'}
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error creating store: {e}", exc_info=True)
            return {"success": False, "error": f"Error creating store: {str(e)}"}

    async def submit_inquiry(self, store_slug: str, item_id: str, form_data: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a customer inquiry."""
        self._check_ready()
        
        try:
            item_obj_id = ObjectId(item_id)
        except InvalidId:
            return {"success": False, "error": "Invalid item ID."}
        
        store = await self.db.stores.find_one({"slug_id": store_slug})
        if not store:
            return {"success": False, "error": "Store not found."}
        
        item = await self.db.items.find_one({"_id": item_obj_id, "store_id": store['_id']})
        if not item:
            return {"success": False, "error": "Item not found."}
        
        new_inquiry = {
            "item_id": item['_id'],
            "item_name": item['name'],
            "customer_name": form_data.get('customer_name'),
            "customer_contact": form_data.get('customer_contact'),
            "message": form_data.get('message'),
            "date_submitted": datetime.datetime.utcnow(),
            "store_id": store['_id'],
            "status": "New"
        }
        
        try:
            await self.db.inquiries.insert_one(new_inquiry)
            return {"success": True, "message": "Thank you! We have received your inquiry and will contact you soon."}
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error submitting inquiry: {e}", exc_info=True)
            return {"success": False, "error": f"Error submitting inquiry: {str(e)}"}

    async def admin_login(self, store_slug: str, email: str, password: str) -> Dict[str, Any]:
        """Handle admin login."""
        self._check_ready()
        
        store = await self.db.stores.find_one({"slug_id": store_slug})
        if not store:
            return {"success": False, "error": "Store not found."}
        
        user = await self.db.users.find_one({
            "email": email,
            "password": password,
            "store_id": store['_id']
        })
        
        if user:
            return {"success": True, "user_id": str(user['_id']), "store_id": str(store['_id'])}
        else:
            return {"success": False, "error": "Invalid email or password for this store."}

    async def get_store_by_slug(self, store_slug: str) -> Optional[Dict[str, Any]]:
        """Get a store by slug."""
        self._check_ready()
        store = await self.db.stores.find_one({"slug_id": store_slug})
        if store and not store.get('logo_url'):
            store['logo_url'] = "/experiments/store_factory/static/img/logo.png"
        return store

    async def update_store_logo(self, store_slug: str, logo_url: str) -> Dict[str, Any]:
        """Update the store logo URL."""
        self._check_ready()
        
        store = await self.db.stores.find_one({"slug_id": store_slug})
        if not store:
            return {"success": False, "error": "Store not found."}
        
        try:
            await self.db.stores.update_one(
                {"slug_id": store_slug},
                {"$set": {"logo_url": logo_url}}
            )
            return {"success": True, "message": "Logo updated successfully."}
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error updating logo: {e}", exc_info=True)
            return {"success": False, "error": f"Error updating logo: {str(e)}"}

    async def get_business_types(self) -> Dict[str, Any]:
        """Get all business types configuration."""
        return BUSINESS_TYPES

    async def initialize(self):
        """
        Post-initialization hook: seeds demo stores if they don't exist.
        This is called automatically when the actor starts up.
        Uses the exact seeding logic from the original Flask code.
        """
        if not self.db:
            logger.warning(f"[{self.write_scope}-Actor] Skipping initialize - DB not ready.")
            return
        
        logger.info(f"[{self.write_scope}-Actor] Starting post-initialization setup...")
        logger.info("Checking for demo stores...")
        
        try:
            # Demo stores from the original Flask code - EXACT COPY
            demo_stores = [
                {
                    'business_type': 'restaurant',
                    'name': 'Country Pizza',
                    'slug_id': 'country-pizza',
                    'tagline': 'Más de 40 años brindándoles servicio en Aguadilla',
                    'about_text': 'Cocina, barra y billares... todo en un solo lugar.',
                    'address': 'Carr. 110 Km 9, Bo. Maleza, Aguadilla, PR',
                    'phone': '17878914860',
                    'phone_display': '(787) 891-4860',
                    'hours': 'Abierto todos los días: 4pm – 12am\nAlmuerzo: 11am – 2pm',
                    'hero_image_url': 'https://images.pexels.com/photos/376464/pexels-photo-376464.jpeg?auto=compress&cs=tinysrgb&w=1260&h=750&dpr=2',
                    'gallery_image_1_url': 'https://chepot87.github.io/country-pizza/img/salon-billar.png',
                    'gallery_image_2_url': 'https://chepot87.github.io/country-pizza/img/billar.jpeg',
                    'menu_image_url': 'https://i.imgur.com/8aJ4z2Y.png',
                    'google_maps_embed_html': '<iframe src="https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d15134.149722116716!2d-67.12504398730123!3d18.50460047092597!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!3m3!1m2!1s0x8c029616f8e64d75%3A0x7356a68ba6d44dca!2sCountry%20Pizza!5e0!3m2!1sen!2sus!4v1750828525017!5m2!1sen!2sus" width="100%" height="100%" style="border:0;" allowfullscreen="" loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe>',
                    'socials': [
                        {'name': 'Facebook', 'icon': 'facebook-f', 'url': 'https://www.facebook.com/CountryPizzaa'},
                        {'name': 'Instagram', 'icon': 'instagram', 'url': 'https://www.instagram.com/countrypizzapr'},
                        {'name': 'WhatsApp', 'icon': 'whatsapp', 'url': 'https://wa.me/17878914860'}
                    ],
                    'items': [
                        {'name': 'Pizza de Pepperoni', 'price': 12.50, 'item_code': 'MENU001', 'description': 'Clásica pizza de pepperoni con queso mozzarella', 'image_url': 'https://chepot87.github.io/country-pizza/img/pizza.png', 'attributes': {'Category': 'Pizzas', 'Ingredients': 'Pepperoni, Queso Mozzarella, Salsa de Tomate'}},
                        {'name': 'Surtido Criollo', 'price': 18.00, 'item_code': 'MENU002', 'description': 'Variedad de platos criollos tradicionales', 'image_url': 'https://chepot87.github.io/country-pizza/img/surtido.png', 'attributes': {'Category': 'Aperitivos', 'Ingredients': 'Carne, Pollo, Tostones'}},
                        {'name': 'Mofongo con Carne Frita', 'price': 15.75, 'item_code': 'MENU003', 'description': 'Mofongo tradicional con carne frita jugosa', 'image_url': 'https://chepot87.github.io/country-pizza/img/mofongo.png', 'attributes': {'Category': 'Platos Principales', 'Ingredients': 'Plátano, Carne Frita, Ajo'}},
                        {'name': 'Churrasco con Tostones', 'price': 19.50, 'item_code': 'MENU005', 'description': 'Churrasco jugoso servido con tostones', 'image_url': 'https://images.unsplash.com/photo-1558030006-450675393462?w=800&auto=format&fit=crop', 'attributes': {'Category': 'Platos Principales', 'Ingredients': 'Churrasco, Tostones'}},
                        {'name': 'Hamburguesa Clásica', 'price': 11.25, 'item_code': 'MENU006', 'description': 'Hamburguesa clásica con todos los acompañamientos', 'image_url': 'https://images.unsplash.com/photo-1568901346375-23c9450c58cd?w=800&auto=format&fit=crop', 'attributes': {'Category': 'Platos Principales', 'Ingredients': 'Carne, Queso, Vegetales'}},
                    ],
                    'specials': [
                        {'title': 'Happy Hour: Cervezas Locales', 'content': '🍺 Disfruta de 2x1 en todas las cervezas locales. Válido de 5pm a 7pm.'},
                        {'title': 'Happy Hour: Combo Pizza', 'content': '🍕 Combo de Pizza personal + Cerveza por solo $9.99.'},
                        {'title': 'Happy Hour: Billar Gratis', 'content': '🎱 Juega billar GRATIS con tu primera ronda de bebidas.'},
                    ],
                    'owner_email': 'owner@country-pizza.com',
                    'owner_password': 'password123'
                },
                {
                    'business_type': 'auto-sales',
                    'name': 'Premium Auto Sales',
                    'slug_id': 'premium-auto-sales',
                    'tagline': 'Quality Vehicles at Great Prices',
                    'about_text': 'We specialize in quality pre-owned vehicles with full inspection and warranty options.',
                    'address': '456 Auto Boulevard, Car City, CA 90210',
                    'phone': '15551234567',
                    'phone_display': '(555) 123-4567',
                    'hours': 'Monday-Saturday: 9am-7pm\nSunday: 11am-5pm',
                    'hero_image_url': 'https://images.pexels.com/photos/116675/pexels-photo-116675.jpeg?auto=compress&cs=tinysrgb&w=1600',
                    'items': [
                        {'name': '2020 Toyota Camry', 'price': 18900.00, 'item_code': 'VIN001', 'description': 'Well-maintained sedan with low mileage', 'status': 'Available', 'image_url': 'https://images.pexels.com/photos/3802508/pexels-photo-3802508.jpeg?auto=compress&cs=tinysrgb&w=800&h=600&fit=crop', 'attributes': {'Make': 'Toyota', 'Model': 'Camry', 'Year': '2020', 'Mileage': '25000', 'Color': 'Silver', 'Condition': 'Excellent'}},
                        {'name': '2019 Honda CR-V', 'price': 22900.00, 'item_code': 'VIN002', 'description': 'Spacious SUV perfect for families', 'status': 'Available', 'image_url': 'https://images.pexels.com/photos/1149137/pexels-photo-1149137.jpeg?auto=compress&cs=tinysrgb&w=800&h=600&fit=crop', 'attributes': {'Make': 'Honda', 'Model': 'CR-V', 'Year': '2019', 'Mileage': '32000', 'Color': 'Black', 'Condition': 'Very Good'}},
                        {'name': '2021 Ford F-150', 'price': 32900.00, 'item_code': 'VIN003', 'description': 'Powerful truck ready for work or play', 'status': 'Pending', 'image_url': 'https://images.unsplash.com/photo-1533473359331-0135ef1b58bf?w=800&auto=format&fit=crop', 'attributes': {'Make': 'Ford', 'Model': 'F-150', 'Year': '2021', 'Mileage': '18000', 'Color': 'Blue', 'Condition': 'Excellent'}},
                    ],
                    'specials': [
                        {'title': 'Spring Sale', 'content': '💰 0% APR financing available on select vehicles this month!'},
                    ],
                    'owner_email': 'owner@premiumauto.com',
                    'owner_password': 'demo123'
                },
                {
                    'business_type': 'auto-services',
                    'name': 'Quick Fix Auto Service',
                    'slug_id': 'quick-fix-auto',
                    'tagline': 'Professional Auto Services You Can Trust',
                    'about_text': 'Full-service auto repair and maintenance with certified technicians.',
                    'address': '789 Service Road, Repair Town, TX 75001',
                    'phone': '15559876543',
                    'phone_display': '(555) 987-6543',
                    'hours': 'Monday-Friday: 8am-6pm\nSaturday: 9am-4pm',
                    'hero_image_url': 'https://images.unsplash.com/photo-1605559424843-9e4c228bf1c2?w=1600&auto=format&fit=crop',
                    'items': [
                        {'name': 'Oil Change Service', 'price': 39.99, 'item_code': 'SRV001', 'description': 'Full synthetic oil change with filter replacement', 'status': 'Available', 'image_url': 'https://images.unsplash.com/photo-1581092160562-40aa08e78837?w=800&auto=format&fit=crop', 'attributes': {'Service Type': 'Maintenance', 'Duration': '30 minutes', 'Warranty': '3 months'}},
                        {'name': 'Brake Inspection & Service', 'price': 89.99, 'item_code': 'SRV002', 'description': 'Complete brake system inspection and pad replacement', 'status': 'Available', 'image_url': 'https://images.unsplash.com/photo-1486262715619-67b85e0b08d3?w=800&auto=format&fit=crop', 'attributes': {'Service Type': 'Repair', 'Duration': '1-2 hours', 'Warranty': '12 months'}},
                        {'name': 'Tire Rotation & Balance', 'price': 49.99, 'item_code': 'SRV003', 'description': 'Professional tire rotation and wheel balancing', 'status': 'Available', 'image_url': 'https://images.pexels.com/photos/3802508/pexels-photo-3802508.jpeg?auto=compress&cs=tinysrgb&w=800', 'attributes': {'Service Type': 'Maintenance', 'Duration': '45 minutes', 'Warranty': '6 months'}},
                    ],
                    'specials': [
                        {'title': 'New Customer Special', 'content': '🔧 $10 off your first service! Mention this ad.'},
                    ],
                    'owner_email': 'owner@quickfix.com',
                    'owner_password': 'demo123'
                },
                {
                    'business_type': 'other-services',
                    'name': 'Pro Services Hub',
                    'slug_id': 'pro-services-hub',
                    'tagline': 'Quality Services You Can Trust',
                    'about_text': 'Professional services for home and business needs.',
                    'address': '321 Service Center Drive, Business City, NY 10001',
                    'phone': '15558889999',
                    'phone_display': '(555) 888-9999',
                    'hours': 'Monday-Friday: 9am-6pm\nBy Appointment',
                    'hero_image_url': 'https://images.unsplash.com/photo-1497366216548-37526070297c?w=1600&auto=format&fit=crop',
                    'items': [
                        {'name': 'Home Cleaning Service', 'price': 120.00, 'item_code': 'SRV101', 'description': 'Professional deep cleaning for your home', 'status': 'Available', 'image_url': 'https://images.unsplash.com/photo-1581578731548-c64695cc6952?w=800&auto=format&fit=crop', 'attributes': {'Service Type': 'Cleaning', 'Duration': '2-4 hours', "What's Included": 'Kitchen, Bathrooms, Living Areas'}},
                        {'name': 'Consulting Session', 'price': 150.00, 'item_code': 'SRV102', 'description': 'One-on-one business consulting', 'status': 'Available', 'image_url': 'https://images.unsplash.com/photo-1556761175-5973dc0f32e7?w=800&auto=format&fit=crop', 'attributes': {'Service Type': 'Consulting', 'Duration': '1 hour', "What's Included": 'Strategy Session & Action Plan'}},
                    ],
                    'specials': [],
                    'owner_email': 'owner@proservices.com',
                    'owner_password': 'demo123'
                },
                {
                    'business_type': 'generic-store',
                    'name': 'General Store Demo',
                    'slug_id': 'general-store-demo',
                    'tagline': 'Quality Products You\'ll Love',
                    'about_text': 'A wide variety of quality products for all your needs.',
                    'address': '999 Store Street, Shopping Mall, FL 33101',
                    'phone': '15557778888',
                    'phone_display': '(555) 777-8888',
                    'hours': 'Monday-Saturday: 10am-8pm\nSunday: 12pm-6pm',
                    'hero_image_url': 'https://images.unsplash.com/photo-1441986300917-64674bd600d8?w=1600&auto=format&fit=crop',
                    'items': [
                        {'name': 'Premium Widget', 'price': 29.99, 'item_code': 'SKU001', 'description': 'High-quality widget for everyday use', 'status': 'Available', 'image_url': 'https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=800&auto=format&fit=crop', 'attributes': {'Category': 'Electronics', 'Brand': 'WidgetPro', 'Color': 'Black'}},
                        {'name': 'Deluxe Gadget', 'price': 49.99, 'item_code': 'SKU002', 'description': 'Advanced gadget with multiple features', 'status': 'Available', 'image_url': 'https://images.unsplash.com/photo-1441986300917-64674bd600d8?w=800&auto=format&fit=crop', 'attributes': {'Category': 'Electronics', 'Brand': 'GadgetMaster', 'Color': 'Silver'}},
                    ],
                    'specials': [
                        {'title': 'Grand Opening Sale', 'content': '🎉 20% off all items this week!'},
                    ],
                    'owner_email': 'owner@generalstore.com',
                    'owner_password': 'demo123'
                }
            ]
            
            created_count = 0
            for demo in demo_stores:
                # Check if store already exists
                existing_store = await self.db.stores.find_one({"slug_id": demo['slug_id']})
                if existing_store:
                    logger.info(f"  ✓ Store '{demo['slug_id']}' already exists, skipping...")
                    continue
                
                logger.info(f"  Creating demo store: {demo['name']}...")
                
                # Create store - EXACT structure from original Flask code
                now = datetime.datetime.utcnow()
                store_data = {
                    "name": demo['name'],
                    "slug_id": demo['slug_id'],
                    "business_type": demo['business_type'],
                    "tagline": demo['tagline'],
                    "about_text": demo['about_text'],
                    "address": demo['address'],
                    "phone": demo['phone'],
                    "phone_display": demo['phone_display'],
                    "hours": demo['hours'],
                    "lang": "es" if demo['slug_id'] == 'country-pizza' else "en",
                    "hero_image_url": demo['hero_image_url'],
                    "logo_url": 'https://chepot87.github.io/country-pizza/img/logo.svg' if demo['slug_id'] == 'country-pizza' else "/experiments/store_factory/static/img/logo.png",  # Default logo
                    "gallery_image_1_url": demo.get('gallery_image_1_url'),
                    "gallery_image_2_url": demo.get('gallery_image_2_url'),
                    "menu_image_url": demo.get('menu_image_url'),
                    "google_maps_embed_html": demo.get('google_maps_embed_html') or '<iframe src="https://www.google.com/maps?q=Orlando,+FL+32812&output=embed" width="100%" height="100%" style="border:0;" allowfullscreen="" loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe>',
                    "inventory_description": None,
                    "socials": demo.get('socials', []),
                    # Theme defaults
                    "theme_primary": demo.get('theme_primary', '#3b82f6'),
                    "theme_secondary": demo.get('theme_secondary', '#f59e0b'),
                    "theme_background": demo.get('theme_background', '#111827'),
                    "theme_surface": demo.get('theme_surface', '#1f2937'),
                    "theme_text": demo.get('theme_text', '#f9fafb'),
                    "theme_text_secondary": demo.get('theme_text_secondary', '#d1d5db'),
                    "date_created": now
                }
                
                try:
                    result = await self.db.stores.insert_one(store_data)
                    store_id = result.inserted_id
                except Exception as e:
                    logger.error(f"  ❌ Error creating store '{demo['slug_id']}': {e}", exc_info=True)
                    continue
                
                # Create admin user
                try:
                    await self.db.users.insert_one({
                        "email": demo['owner_email'],
                        "password": demo['owner_password'],  # In production, hash this!
                        "role": "owner",
                        "store_id": store_id,
                        "date_created": now
                    })
                except Exception as e:
                    logger.error(f"  ❌ Error creating admin user for '{demo['slug_id']}': {e}", exc_info=True)
                    continue
                
                # Create items
                business_config = BUSINESS_TYPES[demo['business_type']]
                items_inserted = 0
                for item_data in demo['items']:
                    try:
                        item = {
                            "name": item_data['name'],
                            "item_code": item_data['item_code'],
                            "price": item_data['price'],
                            "description": item_data.get('description', business_config['default_description']),
                            "image_url": item_data.get('image_url'),
                            "status": item_data.get('status', business_config['status_options'][0] if business_config['status_options'] else None),
                            "store_id": store_id,
                            "date_added": now,
                            "attributes": item_data.get('attributes', {})
                        }
                        await self.db.items.insert_one(item)
                        items_inserted += 1
                    except Exception as e:
                        logger.error(f"  ❌ Error inserting item '{item_data.get('name', 'unknown')}': {e}")
                
                # Create specials
                specials_inserted = 0
                for special_data in demo.get('specials', []):
                    try:
                        await self.db.specials.insert_one({
                            "title": special_data['title'],
                            "content": special_data['content'],
                            "date_created": now,
                            "store_id": store_id
                        })
                        specials_inserted += 1
                    except Exception as e:
                        logger.error(f"  ❌ Error inserting special '{special_data.get('title', 'unknown')}': {e}")
                
                created_count += 1
                logger.info(f"    ✓ Created store with {items_inserted} items and {specials_inserted} specials")
            
            if created_count > 0:
                logger.info(f"\n✅ Successfully created {created_count} demo store(s)!")
                logger.info("    You can now access them from the business type selection page.")
            else:
                logger.info("\n✅ All demo stores already exist.")
                
        except Exception as e:
            logger.error(f"[{self.write_scope}-Actor] Error during initialization: {e}", exc_info=True)
        
        logger.info(f"[{self.write_scope}-Actor] Post-initialization setup complete.")

