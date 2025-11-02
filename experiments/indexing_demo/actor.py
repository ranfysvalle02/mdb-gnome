"""
Experiment Actor for Indexing Demo

Demonstrates all index types supported by g.nome:
- Regular indexes (unique, compound)
- TTL indexes
- Partial indexes
- Text indexes
- Geospatial indexes
- Vector Search indexes
"""

import logging
import random
import datetime
from typing import Any, Dict, List, Optional
import ray

logger = logging.getLogger(__name__)


@ray.remote
class ExperimentActor:
    """
    Actor that demonstrates all index types.
    """
    
    def __init__(
        self,
        mongo_uri: str,
        db_name: str,
        write_scope: str,
        read_scopes: List[str]
    ):
        """
        Initialize the actor with MongoDB connection.
        """
        from async_mongo_wrapper import ScopedMongoWrapper
        from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
        
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.write_scope = write_scope
        self.read_scopes = read_scopes
        
        # Initialize MongoDB connection
        self.client: Optional[AsyncIOMotorClient] = None
        self.db: Optional[AsyncIOMotorDatabase] = None
        self.wrapped_db: Optional[ScopedMongoWrapper] = None
        
        logger.info(f"[IndexingDemo] Actor initialized with write_scope={write_scope}, read_scopes={read_scopes}")
    
    async def initialize(self):
        """
        Initialize MongoDB connection.
        """
        from async_mongo_wrapper import ScopedMongoWrapper
        from motor.motor_asyncio import AsyncIOMotorClient
        
        try:
            self.client = AsyncIOMotorClient(self.mongo_uri)
            self.db = self.client[self.db_name]
            self.wrapped_db = ScopedMongoWrapper(
                real_db=self.db,
                read_scopes=self.read_scopes,
                write_scope=self.write_scope,
                auto_index=False  # We're managing indexes via manifest
            )
            logger.info(f"[IndexingDemo] MongoDB connection established")
        except Exception as e:
            logger.error(f"[IndexingDemo] Failed to initialize MongoDB: {e}", exc_info=True)
            raise
    
    async def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the demo data.
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            products_count = await self.wrapped_db.products.count_documents({})
            sessions_count = await self.wrapped_db.sessions.count_documents({})
            embeddings_count = await self.wrapped_db.embeddings.count_documents({})
            logs_count = await self.wrapped_db.logs.count_documents({})
            
            return {
                "products": products_count,
                "sessions": sessions_count,
                "embeddings": embeddings_count,
                "logs": logs_count,
                "collections": {
                    "products": products_count,
                    "sessions": sessions_count,
                    "embeddings": embeddings_count,
                    "logs": logs_count
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error getting stats: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def seed_sample_data(self) -> Dict[str, Any]:
        """
        Seed sample data for all index types.
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            # Seed products (regular, text, geospatial indexes)
            products = []
            categories = ["Electronics", "Clothing", "Food", "Books", "Tools"]
            
            for i in range(20):
                products.append({
                    "sku": f"SKU-{i:04d}",
                    "name": f"Product {i}",
                    "description": f"This is a description for product {i} with various keywords",
                    "category": random.choice(categories),
                    "price": round(random.uniform(10.0, 1000.0), 2),
                    "location": {
                        "type": "Point",
                        "coordinates": [
                            random.uniform(-122.5, -122.3),  # San Francisco area
                            random.uniform(37.7, 37.9)
                        ]
                    },
                    "in_stock": random.choice([True, False]),
                    "created_at": datetime.datetime.utcnow()
                })
            
            await self.wrapped_db.products.insert_many(products)
            
            # Seed sessions (TTL, partial indexes)
            sessions = []
            for i in range(10):
                sessions.append({
                    "user_id": f"user_{i}",
                    "session_id": f"session_{i}",
                    "active": i % 2 == 0,  # Half active, half inactive
                    "created_at": datetime.datetime.utcnow(),
                    "data": {"page_views": random.randint(1, 50)}
                })
            
            await self.wrapped_db.sessions.insert_many(sessions)
            
            # Seed embeddings (vector search index)
            embeddings = []
            for i in range(5):
                # Generate a simple 384-dimensional vector (for demo purposes)
                vector = [random.uniform(-1.0, 1.0) for _ in range(384)]
                embeddings.append({
                    "document_id": f"doc_{i}",
                    "embedding_vector": vector,
                    "text": f"Sample document {i}",
                    "metadata": {"type": "demo"}
                })
            
            await self.wrapped_db.embeddings.insert_many(embeddings)
            
            # Seed logs (regular, text indexes)
            logs = []
            levels = ["INFO", "WARNING", "ERROR", "DEBUG"]
            messages = [
                "Application started",
                "User logged in",
                "Database connection established",
                "Error processing request",
                "Cache miss occurred",
                "Task completed successfully"
            ]
            
            for i in range(15):
                logs.append({
                    "timestamp": datetime.datetime.utcnow() - datetime.timedelta(seconds=i * 60),
                    "level": random.choice(levels),
                    "message": random.choice(messages),
                    "source": f"service_{random.randint(1, 3)}"
                })
            
            await self.wrapped_db.logs.insert_many(logs)
            
            return {
                "success": True,
                "products": len(products),
                "sessions": len(sessions),
                "embeddings": len(embeddings),
                "logs": len(logs)
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error seeding data: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def test_regular_indexes(self) -> Dict[str, Any]:
        """
        Test regular index queries (unique SKU, compound category/price).
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            # Test unique index: find by SKU
            product_by_sku = await self.wrapped_db.products.find_one({"sku": "SKU-0001"})
            
            # Test compound index: find by category and price range
            products_by_category = await self.wrapped_db.products.find({
                "category": "Electronics",
                "price": {"$gte": 100.0}
            }).sort("price", -1).limit(5).to_list(5)
            
            return {
                "success": True,
                "unique_index": {
                    "found": product_by_sku is not None,
                    "sku": product_by_sku.get("sku") if product_by_sku else None
                },
                "compound_index": {
                    "count": len(products_by_category),
                    "products": [
                        {"sku": p.get("sku"), "category": p.get("category"), "price": p.get("price")}
                        for p in products_by_category
                    ]
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error testing regular indexes: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def test_text_index(self) -> Dict[str, Any]:
        """
        Test text index queries.
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            # Text search query
            text_results = await self.wrapped_db.products.find({
                "$text": {"$search": "description keywords"}
            }).limit(5).to_list(5)
            
            return {
                "success": True,
                "text_search": {
                    "query": "description keywords",
                    "count": len(text_results),
                    "products": [
                        {"name": p.get("name"), "description": p.get("description")[:50]}
                        for p in text_results
                    ]
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error testing text index: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def test_geospatial_index(self) -> Dict[str, Any]:
        """
        Test geospatial index queries (2dsphere).
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            # Find products near San Francisco
            sf_coords = [-122.4194, 37.7749]
            
            nearby_products = await self.wrapped_db.products.find({
                "location": {
                    "$near": {
                        "$geometry": {
                            "type": "Point",
                            "coordinates": sf_coords
                        },
                        "$maxDistance": 50000  # 50km
                    }
                }
            }).limit(5).to_list(5)
            
            return {
                "success": True,
                "geospatial_search": {
                    "center": sf_coords,
                    "max_distance_km": 50,
                    "count": len(nearby_products),
                    "products": [
                        {
                            "name": p.get("name"),
                            "location": p.get("location", {}).get("coordinates")
                        }
                        for p in nearby_products
                    ]
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error testing geospatial index: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def test_partial_index(self) -> Dict[str, Any]:
        """
        Test partial index queries (only active sessions).
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            # Query that should use partial index (active sessions only)
            active_sessions = await self.wrapped_db.sessions.find({
                "active": True
            }).limit(5).to_list(5)
            
            # Query that shouldn't use partial index (inactive sessions)
            all_sessions = await self.wrapped_db.sessions.find({}).limit(10).to_list(10)
            
            return {
                "success": True,
                "partial_index": {
                    "active_sessions_count": len(active_sessions),
                    "total_sessions_count": len(all_sessions),
                    "active_sessions": [
                        {
                            "user_id": s.get("user_id"),
                            "session_id": s.get("session_id"),
                            "active": s.get("active")
                        }
                        for s in active_sessions
                    ]
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error testing partial index: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def test_vector_index(self) -> Dict[str, Any]:
        """
        Test vector search index queries.
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            # Generate a query vector
            query_vector = [random.uniform(-1.0, 1.0) for _ in range(384)]
            
            # Vector search query
            pipeline = [
                {
                    "$vectorSearch": {
                        "index": "indexing_demo_embedding_vector_search",
                        "path": "embedding_vector",
                        "queryVector": query_vector,
                        "numCandidates": 5,
                        "limit": 3
                    }
                },
                {
                    "$project": {
                        "document_id": 1,
                        "text": 1,
                        "score": {"$meta": "vectorSearchScore"}
                    }
                }
            ]
            
            vector_results = await self.wrapped_db.embeddings.aggregate(pipeline).to_list(3)
            
            return {
                "success": True,
                "vector_search": {
                    "query_vector_dim": len(query_vector),
                    "results_count": len(vector_results),
                    "results": [
                        {
                            "document_id": r.get("document_id"),
                            "text": r.get("text"),
                            "score": r.get("score")
                        }
                        for r in vector_results
                    ]
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error testing vector index: {e}", exc_info=True)
            # Vector search might not be available in all environments
            return {
                "success": False,
                "error": str(e),
                "note": "Vector search requires Atlas Vector Search. This may not be available in all environments."
            }
    
    async def test_ttl_index(self) -> Dict[str, Any]:
        """
        Test TTL index (check session expiration).
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            # Count sessions (TTL index should delete old ones)
            total_sessions = await self.wrapped_db.sessions.count_documents({})
            
            # Create a session with old timestamp (should be expired)
            old_session = {
                "user_id": "old_user",
                "session_id": "old_session",
                "active": True,
                "created_at": datetime.datetime.utcnow() - datetime.timedelta(hours=2),  # 2 hours old
                "data": {"note": "This should expire soon"}
            }
            
            await self.wrapped_db.sessions.insert_one(old_session)
            
            # Wait a moment, then check if it still exists
            import asyncio
            await asyncio.sleep(1)
            
            old_session_check = await self.wrapped_db.sessions.find_one({"session_id": "old_session"})
            
            return {
                "success": True,
                "ttl_index": {
                    "total_sessions": total_sessions,
                    "old_session_inserted": True,
                    "old_session_still_exists": old_session_check is not None,
                    "note": "TTL indexes delete documents automatically. MongoDB runs cleanup every 60 seconds."
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error testing TTL index: {e}", exc_info=True)
            return {"error": str(e)}
    
    async def clear_all_data(self) -> Dict[str, Any]:
        """
        Clear all demo data.
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            products_deleted = await self.wrapped_db.products.delete_many({})
            sessions_deleted = await self.wrapped_db.sessions.delete_many({})
            embeddings_deleted = await self.wrapped_db.embeddings.delete_many({})
            logs_deleted = await self.wrapped_db.logs.delete_many({})
            
            return {
                "success": True,
                "deleted": {
                    "products": products_deleted.deleted_count,
                    "sessions": sessions_deleted.deleted_count,
                    "embeddings": embeddings_deleted.deleted_count,
                    "logs": logs_deleted.deleted_count
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error clearing data: {e}", exc_info=True)
            return {"error": str(e)}

