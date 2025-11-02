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
        Seed sample data for all index types with detailed progress information.
        """
        if not self.wrapped_db:
            return {"error": "Database not initialized"}
        
        try:
            steps = []
            
            # Step 1: Seed products (regular, text, geospatial indexes)
            steps.append({
                "step": 1,
                "name": "Creating Products Collection",
                "description": "Seeding product data to demonstrate regular indexes (unique SKU), text indexes (searchable name/description), and geospatial indexes (location-based queries).",
                "indexes": ["Unique SKU index", "Compound category/price index", "Text index on name/description", "2dsphere geospatial index"],
                "status": "in_progress"
            })
            
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
            steps[-1]["status"] = "completed"
            steps[-1]["count"] = len(products)
            
            # Step 2: Seed sessions (TTL, partial indexes)
            steps.append({
                "step": 2,
                "name": "Creating Sessions Collection",
                "description": "Seeding session data to demonstrate TTL indexes (auto-expiring documents) and partial indexes (indexing only active sessions).",
                "indexes": ["TTL index on created_at (1 hour expiration)", "Partial unique index on user_id/session_id (active sessions only)"],
                "status": "in_progress"
            })
            
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
            steps[-1]["status"] = "completed"
            steps[-1]["count"] = len(sessions)
            steps[-1]["note"] = f"{len([s for s in sessions if s['active']])} active sessions will be indexed by the partial index"
            
            # Step 3: Seed embeddings (vector search index)
            steps.append({
                "step": 3,
                "name": "Creating Embeddings Collection",
                "description": "Seeding vector embeddings to demonstrate Atlas Vector Search for semantic similarity queries.",
                "indexes": ["Vector Search index (384 dimensions, cosine similarity)"],
                "status": "in_progress"
            })
            
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
            steps[-1]["status"] = "completed"
            steps[-1]["count"] = len(embeddings)
            
            # Step 4: Seed logs (regular, text indexes)
            steps.append({
                "step": 4,
                "name": "Creating Logs Collection",
                "description": "Seeding log entries to demonstrate compound indexes and text search capabilities.",
                "indexes": ["Compound index on timestamp/level", "Text index on message"],
                "status": "in_progress"
            })
            
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
            steps[-1]["status"] = "completed"
            steps[-1]["count"] = len(logs)
            
            return {
                "success": True,
                "products": len(products),
                "sessions": len(sessions),
                "embeddings": len(embeddings),
                "logs": len(logs),
                "steps": steps,
                "summary": {
                    "total_documents": len(products) + len(sessions) + len(embeddings) + len(logs),
                    "collections": 4,
                    "index_types_demonstrated": 6
                }
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error seeding data: {e}", exc_info=True)
            return {"error": str(e), "steps": steps if 'steps' in locals() else []}
    
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
                "index_type": "Regular Indexes",
                "description": "Regular indexes are the foundation of MongoDB query performance. They enable fast lookups, enforce uniqueness, and optimize compound queries.",
                "tests": [
                    {
                        "name": "Unique Index Lookup",
                        "index": "product_sku_unique",
                        "query": '{"sku": "SKU-0001"}',
                        "explanation": "The unique index on SKU ensures fast O(log n) lookups and prevents duplicate SKUs. Without an index, MongoDB would scan every document.",
                        "result": {
                            "found": product_by_sku is not None,
                            "sku": product_by_sku.get("sku") if product_by_sku else None,
                            "name": product_by_sku.get("name") if product_by_sku else None
                        }
                    },
                    {
                        "name": "Compound Index Query",
                        "index": "product_category_price",
                        "query": '{"category": "Electronics", "price": {"$gte": 100.0}}',
                        "explanation": "Compound indexes support efficient multi-field queries. The index order (category, price) optimizes queries that filter by category first, then by price. Sorting by price descending is also optimized.",
                        "result": {
                            "count": len(products_by_category),
                            "products": [
                                {"sku": p.get("sku"), "category": p.get("category"), "price": p.get("price")}
                                for p in products_by_category
                            ]
                        }
                    }
                ],
                "value": "Regular indexes can improve query performance from O(n) full collection scans to O(log n) index lookups, often delivering 100-1000x speed improvements."
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
                "index_type": "Text Index",
                "description": "Text indexes enable full-text search capabilities, allowing you to search for words and phrases across multiple fields with relevance scoring.",
                "tests": [
                    {
                        "name": "Full-Text Search",
                        "index": "product_name_text",
                        "query": '{"$text": {"$search": "description keywords"}}',
                        "explanation": "Text indexes tokenize and stem words, enabling natural language search. Fields can have different weights (name: 10, description: 5) to prioritize matches in certain fields. The search uses word stemming and case-insensitive matching.",
                        "result": {
                            "query": "description keywords",
                            "count": len(text_results),
                            "products": [
                                {"name": p.get("name"), "description": p.get("description")[:80] + "..." if len(p.get("description", "")) > 80 else p.get("description", "")}
                                for p in text_results
                            ]
                        }
                    }
                ],
                "value": "Text indexes enable powerful search functionality without requiring exact matches. They're essential for search engines, product catalogs, and content discovery features."
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
                "index_type": "Geospatial Index",
                "description": "Geospatial indexes enable location-based queries using GeoJSON or legacy coordinate pairs. The 2dsphere index type supports spherical geometry calculations for accurate distance measurements.",
                "tests": [
                    {
                        "name": "Nearby Location Search",
                        "index": "product_location_2dsphere",
                        "query": "$near query with 50km radius from San Francisco",
                        "explanation": "The 2dsphere index uses spherical calculations to find documents within a specified distance from a point. Results are automatically sorted by distance. This is perfect for 'find stores near me' or 'find products in your area' features.",
                        "result": {
                            "center": sf_coords,
                            "center_label": "San Francisco, CA",
                            "max_distance_km": 50,
                            "count": len(nearby_products),
                            "products": [
                                {
                                    "name": p.get("name"),
                                    "location": p.get("location", {}).get("coordinates"),
                                    "sku": p.get("sku")
                                }
                                for p in nearby_products
                            ]
                        }
                    }
                ],
                "value": "Geospatial indexes power location-based features like finding nearby restaurants, delivery zones, ride-sharing matching, and IoT device tracking. They enable real-time spatial queries with sub-second response times."
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
                "index_type": "Partial Index",
                "description": "Partial indexes only index documents that match a filter expression. This reduces index size and maintenance overhead while still optimizing queries that match the filter.",
                "tests": [
                    {
                        "name": "Partial Index Query",
                        "index": "session_user_active",
                        "query": '{"active": true}',
                        "explanation": "This partial index only indexes active sessions. It provides the same query performance as a full index for active sessions, but uses less storage and requires less maintenance. Inactive sessions are not indexed, saving space.",
                        "result": {
                            "active_sessions_count": len(active_sessions),
                            "total_sessions_count": len(all_sessions),
                            "indexed_documents": len(active_sessions),
                            "non_indexed_documents": len(all_sessions) - len(active_sessions),
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
                ],
                "value": "Partial indexes are perfect when you frequently query a subset of documents. They can reduce index size by 50-90% while maintaining full performance for filtered queries. This saves storage, reduces write overhead, and speeds up index maintenance."
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
                "index_type": "Vector Search Index",
                "description": "Vector search indexes enable semantic similarity searches using high-dimensional embeddings. This powers AI features like similarity search, recommendation systems, and semantic retrieval.",
                "tests": [
                    {
                        "name": "Semantic Similarity Search",
                        "index": "embedding_vector_search",
                        "query": "Cosine similarity search with 384-dimensional vectors",
                        "explanation": "Vector search finds documents with similar meaning by comparing the cosine similarity between embedding vectors. Each document's text is converted to a 384-dimensional vector representing its semantic meaning. The search returns documents ranked by semantic similarity, not just keyword matches.",
                        "result": {
                            "query_vector_dim": len(query_vector),
                            "similarity_metric": "cosine",
                            "results_count": len(vector_results),
                            "results": [
                                {
                                    "document_id": r.get("document_id"),
                                    "text": r.get("text"),
                                    "score": round(r.get("score", 0), 4) if r.get("score") else None
                                }
                                for r in vector_results
                            ]
                        }
                    }
                ],
                "value": "Vector search enables AI-powered features like 'find similar items', intelligent recommendations, semantic search (finding documents by meaning, not just keywords), and RAG (Retrieval-Augmented Generation) for LLM applications. It's the foundation of modern AI applications."
            }
        except Exception as e:
            logger.error(f"[IndexingDemo] Error testing vector index: {e}", exc_info=True)
            # Vector search might not be available in all environments
            return {
                "success": False,
                "error": str(e),
                "index_type": "Vector Search Index",
                "description": "Vector search indexes enable semantic similarity searches using high-dimensional embeddings.",
                "note": "Vector search requires Atlas Vector Search (MongoDB Atlas). This may not be available in all environments. Vector search is essential for AI applications like recommendation systems, semantic search, and RAG (Retrieval-Augmented Generation).",
                "value": "Vector search enables AI-powered features by finding documents based on semantic meaning rather than exact keyword matches."
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
                "index_type": "TTL Index",
                "description": "TTL (Time-To-Live) indexes automatically delete documents after a specified expiration time. MongoDB's background task removes expired documents approximately every 60 seconds.",
                "tests": [
                    {
                        "name": "Automatic Document Expiration",
                        "index": "session_created_at_ttl",
                        "expiration": "1 hour after created_at",
                        "explanation": "TTL indexes monitor a date field and automatically remove documents when their expiration time is reached. This is perfect for session data, temporary caches, event logs, and any data that has a natural expiration. No application code needed - MongoDB handles it automatically.",
                        "result": {
                            "total_sessions": total_sessions,
                            "old_session_inserted": True,
                            "old_session_age_hours": 2,
                            "old_session_still_exists": old_session_check is not None,
                            "expiration_setting": "1 hour",
                            "cleanup_note": "MongoDB runs TTL cleanup approximately every 60 seconds. The old session may not be deleted immediately, but will be removed in the next cleanup cycle."
                        }
                    }
                ],
                "value": "TTL indexes eliminate the need for application-level cleanup jobs, reduce storage costs, and ensure data retention policies are automatically enforced. They're essential for GDPR compliance, log rotation, and managing temporary data."
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

