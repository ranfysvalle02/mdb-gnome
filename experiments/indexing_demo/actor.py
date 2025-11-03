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
        Initialize the actor with MongoDB connection using ExperimentDB.
        """
        self.write_scope = write_scope
        self.read_scopes = read_scopes
        
        # Initialize database using ExperimentDB (consistent with other experiments)
        try:
            from experiment_db import create_actor_database
            self.db = create_actor_database(
                mongo_uri,
                db_name,
                write_scope,
                read_scopes
            )
            logger.info(
                f"[IndexingDemo] Actor initialized with write_scope='{self.write_scope}' "
                f"(DB='{db_name}') using magical database abstraction"
            )
        except Exception as e:
            logger.critical(f"[IndexingDemo] ❌ CRITICAL: Failed to init DB: {e}")
            self.db = None
    
    async def initialize(self):
        """
        Post-initialization hook (kept for backward compatibility).
        Database is already initialized in __init__ via ExperimentDB.
        """
        if not self.db:
            logger.warning(f"[IndexingDemo] Skipping initialize - DB not ready.")
            return
        
        logger.info(f"[IndexingDemo] Post-initialization complete (database already initialized).")
    
    async def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the demo data.
        """
        if not self.db:
            return {"error": "Database not initialized"}
        
        try:
            products_count = await self.db.products.count_documents({})
            sessions_count = await self.db.sessions.count_documents({})
            embeddings_count = await self.db.embeddings.count_documents({})
            logs_count = await self.db.logs.count_documents({})
            
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
        if not self.db:
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
            
            # Product name and description templates for variety in text search
            product_types = [
                ("Laptop", "High-performance laptop computer with fast processor and large memory"),
                ("Smartphone", "Latest smartphone with advanced camera and long battery life"),
                ("T-Shirt", "Comfortable cotton t-shirt available in multiple colors and sizes"),
                ("Coffee", "Premium roasted coffee beans from various regions around the world"),
                ("Novel", "Engaging fiction novel with compelling characters and plot twists"),
                ("Hammer", "Durable construction hammer with ergonomic grip and balanced weight"),
                ("Tablet", "Portable tablet device perfect for reading and entertainment"),
                ("Jacket", "Weather-resistant jacket with multiple pockets and adjustable hood"),
                ("Chocolate", "Artisan chocolate bars made with organic ingredients"),
                ("Textbook", "Comprehensive textbook covering advanced topics and concepts"),
                ("Screwdriver", "Professional screwdriver set with multiple bits and attachments"),
                ("Headphones", "Premium wireless headphones with noise cancellation technology"),
                ("Sneakers", "Comfortable running shoes designed for athletic performance"),
                ("Pizza", "Gourmet pizza with fresh toppings and handmade dough"),
                ("Biography", "Detailed biography documenting historical events and personal stories"),
                ("Wrench", "Adjustable wrench tool for various mechanical applications"),
            ]
            
            # Generate 10,000 products to really demonstrate index power
            for i in range(10000):
                product_type = random.choice(product_types)
                category = random.choice(categories)
                products.append({
                    "sku": f"SKU-{i:05d}",
                    "name": f"{product_type[0]} {i % 100}",
                    "description": f"{product_type[1]}. This {product_type[0].lower()} is perfect for {random.choice(['home use', 'professional use', 'students', 'enthusiasts', 'beginners', 'experts'])}. Features include quality materials, reliable performance, and excellent value.",
                    "category": category,
                    "price": round(random.uniform(10.0, 1000.0), 2),
                    "location": {
                        "type": "Point",
                        "coordinates": [
                            random.uniform(-122.5, -122.3),  # San Francisco area
                            random.uniform(37.7, 37.9)
                        ]
                    },
                    "in_stock": random.choice([True, False]),
                    "created_at": datetime.datetime.utcnow() - datetime.timedelta(days=random.randint(0, 365))
                })
            
            # Insert in batches for better performance with large datasets
            batch_size = 1000
            for i in range(0, len(products), batch_size):
                batch = products[i:i + batch_size]
                await self.db.products.insert_many(batch)
            
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
            # Generate 5,000 sessions to demonstrate TTL and partial indexes
            for i in range(5000):
                # Vary created_at times to show TTL index working over time
                created_at = datetime.datetime.utcnow() - datetime.timedelta(
                    hours=random.randint(0, 48), 
                    minutes=random.randint(0, 59)
                )
                sessions.append({
                    "user_id": f"user_{random.randint(0, 999)}",
                    "session_id": f"session_{i:05d}",
                    "active": random.random() > 0.4,  # 60% active, 40% inactive
                    "created_at": created_at,
                    "data": {"page_views": random.randint(1, 200)}
                })
            
            # Insert in batches for better performance with large datasets
            batch_size = 1000
            for i in range(0, len(sessions), batch_size):
                batch = sessions[i:i + batch_size]
                await self.db.sessions.insert_many(batch)
            
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
            # Generate 1,000 embeddings for vector search demonstration
            document_topics = [
                "machine learning", "artificial intelligence", "data science",
                "web development", "database design", "software engineering",
                "cloud computing", "cybersecurity", "mobile development",
                "user interface design", "API development", "testing strategies"
            ]
            
            for i in range(1000):
                # Generate a simple 384-dimensional vector (for demo purposes)
                vector = [random.uniform(-1.0, 1.0) for _ in range(384)]
                topic = random.choice(document_topics)
                embeddings.append({
                    "document_id": f"doc_{i:05d}",
                    "embedding_vector": vector,
                    "text": f"Document about {topic}: comprehensive guide covering all aspects of {topic} including best practices, examples, and implementation strategies.",
                    "metadata": {"type": "demo", "topic": topic, "index": i}
                })
            
            # Insert in batches for better performance with large datasets
            batch_size = 500  # Smaller batch for vector data
            for i in range(0, len(embeddings), batch_size):
                batch = embeddings[i:i + batch_size]
                await self.db.embeddings.insert_many(batch)
            
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
                "Application started successfully",
                "User logged in from remote location",
                "Database connection established with connection pooling",
                "Error processing request with invalid parameters",
                "Cache miss occurred for frequently accessed data",
                "Task completed successfully after processing large dataset",
                "API request received from external client",
                "Authentication token validated for secure endpoint",
                "File upload completed with encryption enabled",
                "Background job scheduled for asynchronous processing",
                "Email notification sent to registered users",
                "Payment transaction processed with encryption",
                "System backup completed without errors",
                "Memory usage exceeded threshold requiring cleanup",
                "Network latency detected in distributed system",
                "Configuration updated for production environment",
                "Security audit performed on sensitive data",
                "Load balancer redirected traffic to healthy server",
                "Database index created for improved query performance",
                "Session timeout occurred for inactive user"
            ]
            
            # Generate 10,000 log entries over the past 30 days
            for i in range(10000):
                # Spread logs over the past 30 days
                days_ago = random.randint(0, 30)
                hours_ago = random.randint(0, 23)
                minutes_ago = random.randint(0, 59)
                logs.append({
                    "timestamp": datetime.datetime.utcnow() - datetime.timedelta(
                        days=days_ago, 
                        hours=hours_ago, 
                        minutes=minutes_ago,
                        seconds=random.randint(0, 59)
                    ),
                    "level": random.choice(levels),
                    "message": random.choice(messages),
                    "source": f"service_{random.randint(1, 10)}"
                })
            
            # Insert in batches for better performance with large datasets
            batch_size = 1000
            for i in range(0, len(logs), batch_size):
                batch = logs[i:i + batch_size]
                await self.db.logs.insert_many(batch)
            
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
        Test regular index queries (unique SKU, compound category/price) with performance metrics.
        """
        if not self.db:
            return {"error": "Database not initialized"}
        
        try:
            import time
            
            # Test unique index: find by SKU with timing
            # Use a mid-range SKU to demonstrate index efficiency with large dataset
            test_sku = "SKU-05000"
            start_time = time.time()
            product_by_sku = await self.db.products.find_one({"sku": test_sku})
            unique_query_time = (time.time() - start_time) * 1000  # Convert to milliseconds
            
            # Get explain plan for unique index query
            try:
                explain_result = await self.db.raw.products.find({"sku": test_sku}).explain("executionStats")
                unique_explain = self._extract_explain_info(explain_result)
                logger.debug(f"[IndexingDemo] Unique index explain: {unique_explain}")
            except Exception as e:
                logger.warning(f"Could not get explain plan: {e}", exc_info=True)
                unique_explain = None
            
            # Test compound index: find by category and price range with timing
            start_time = time.time()
            products_by_category = await self.db.products.find({
                "category": "Electronics",
                "price": {"$gte": 100.0}
            }).sort("price", -1).limit(5).to_list(5)
            compound_query_time = (time.time() - start_time) * 1000
            
            # Get explain plan for compound query
            try:
                explain_result = await self.db.raw.products.find({
                    "category": "Electronics",
                    "price": {"$gte": 100.0}
                }).sort("price", -1).limit(5).explain("executionStats")
                compound_explain = self._extract_explain_info(explain_result)
                logger.debug(f"[IndexingDemo] Compound index explain: {compound_explain}")
            except Exception as e:
                logger.warning(f"Could not get explain plan: {e}", exc_info=True)
                compound_explain = None
            
            return {
                "success": True,
                "index_type": "Regular Indexes",
                "description": "Regular indexes are the foundation of MongoDB query performance. They enable fast lookups, enforce uniqueness, and optimize compound queries.",
                "tests": [
                    {
                        "name": "Unique Index Lookup",
                        "index": "product_sku_unique",
                        "query": f'{{"sku": "{test_sku}"}}',
                        "explanation": "The unique index on SKU ensures fast O(log n) lookups and prevents duplicate SKUs. Without an index, MongoDB would scan every document.",
                        "performance": {
                            "execution_time_ms": round(unique_query_time, 2),
                            "explain": unique_explain
                        },
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
                        "performance": {
                            "execution_time_ms": round(compound_query_time, 2),
                            "explain": compound_explain
                        },
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
    
    def _extract_explain_info(self, explain_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract key information from MongoDB explain() result.
        Traverses nested stages to find index usage.
        """
        try:
            execution_stats = explain_result.get("executionStats", {})
            winning_plan = explain_result.get("queryPlanner", {}).get("winningPlan", {})
            
            # Recursively find index name in nested stages
            def find_index_name(plan: Dict[str, Any]) -> Optional[str]:
                """Recursively search for indexName in plan stages."""
                if not plan or not isinstance(plan, dict):
                    return None
                
                # Check current stage for indexName
                if "indexName" in plan:
                    return plan.get("indexName")
                
                # Check nested inputStage(s)
                if "inputStage" in plan:
                    result = find_index_name(plan["inputStage"])
                    if result:
                        return result
                
                # Check inputStages (for stages like OR, SORT_MERGE, etc.)
                if "inputStages" in plan:
                    for stage in plan.get("inputStages", []):
                        result = find_index_name(stage)
                        if result:
                            return result
                
                return None
            
            index_name = find_index_name(winning_plan)
            
            # Determine stage name (look for IXSCAN, FETCH, etc.)
            def find_stage_type(plan: Dict[str, Any]) -> str:
                """Find the most relevant stage type."""
                if not plan or not isinstance(plan, dict):
                    return "unknown"
                
                stage = plan.get("stage", "")
                if stage in ["IXSCAN", "FETCH", "SORT"]:
                    return stage
                
                # Check nested stages
                if "inputStage" in plan:
                    nested_stage = find_stage_type(plan["inputStage"])
                    if nested_stage != "unknown":
                        return nested_stage
                
                if "inputStages" in plan:
                    for stage in plan.get("inputStages", []):
                        nested_stage = find_stage_type(stage)
                        if nested_stage != "unknown":
                            return nested_stage
                
                return stage or "unknown"
            
            stage_type = find_stage_type(winning_plan)
            
            # Determine if it's a collection scan
            is_collection_scan = (
                stage_type == "COLLSCAN" or 
                (not index_name and execution_stats.get("totalDocsExamined", 0) > execution_stats.get("nReturned", 0))
            )
            
            return {
                "execution_time_ms": execution_stats.get("executionTimeMillis", 0),
                "total_docs_examined": execution_stats.get("totalDocsExamined", 0),
                "total_docs_returned": execution_stats.get("nReturned", 0),
                "index_used": index_name if index_name else ("Collection Scan (No Index)" if is_collection_scan else "Collection Scan"),
                "stage": stage_type,
                "efficiency": round(execution_stats.get("nReturned", 0) / max(execution_stats.get("totalDocsExamined", 1), 1) * 100, 1) if execution_stats.get("totalDocsExamined", 0) > 0 else 100
            }
        except Exception as e:
            logger.warning(f"Error extracting explain info: {e}", exc_info=True)
            return None
    
    async def test_text_index(self) -> Dict[str, Any]:
        """
        Test text index queries with performance metrics.
        """
        if not self.db:
            return {"error": "Database not initialized"}
        
        try:
            import time
            
            # Text search query with timing
            start_time = time.time()
            text_results = await self.db.products.find({
                "$text": {"$search": "description keywords"}
            }).limit(5).to_list(5)
            text_query_time = (time.time() - start_time) * 1000
            
            # Get explain plan
            try:
                explain_result = await self.db.raw.products.find({
                    "$text": {"$search": "description keywords"}
                }).limit(5).explain("executionStats")
                text_explain = self._extract_explain_info(explain_result)
                logger.debug(f"[IndexingDemo] Text index explain: {text_explain}")
            except Exception as e:
                logger.warning(f"Could not get explain plan: {e}", exc_info=True)
                text_explain = None
            
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
                        "performance": {
                            "execution_time_ms": round(text_query_time, 2),
                            "explain": text_explain
                        },
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
        Test geospatial index queries (2dsphere) with performance metrics.
        """
        if not self.db:
            return {"error": "Database not initialized"}
        
        try:
            import time
            
            # Find products near San Francisco with timing
            sf_coords = [-122.4194, 37.7749]
            
            start_time = time.time()
            nearby_products = await self.db.products.find({
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
            geo_query_time = (time.time() - start_time) * 1000
            
            # Get explain plan
            try:
                explain_result = await self.db.raw.products.find({
                    "location": {
                        "$near": {
                            "$geometry": {
                                "type": "Point",
                                "coordinates": sf_coords
                            },
                            "$maxDistance": 50000
                        }
                    }
                }).limit(5).explain("executionStats")
                geo_explain = self._extract_explain_info(explain_result)
                logger.debug(f"[IndexingDemo] Geospatial index explain: {geo_explain}")
            except Exception as e:
                logger.warning(f"Could not get explain plan: {e}", exc_info=True)
                geo_explain = None
            
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
                        "performance": {
                            "execution_time_ms": round(geo_query_time, 2),
                            "explain": geo_explain
                        },
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
        Test partial index queries (only active sessions) with performance metrics.
        """
        if not self.db:
            return {"error": "Database not initialized"}
        
        try:
            import time
            
            # Query that should use partial index (active sessions only) with timing
            start_time = time.time()
            active_sessions = await self.db.sessions.find({
                "active": True
            }).limit(5).to_list(5)
            partial_query_time = (time.time() - start_time) * 1000
            
            # Query that shouldn't use partial index (inactive sessions)
            all_sessions = await self.db.sessions.find({}).limit(10).to_list(10)
            
            # Get explain plan
            try:
                explain_result = await self.db.raw.sessions.find({
                    "active": True
                }).limit(5).explain("executionStats")
                partial_explain = self._extract_explain_info(explain_result)
                logger.debug(f"[IndexingDemo] Partial index explain: {partial_explain}")
            except Exception as e:
                logger.warning(f"Could not get explain plan: {e}", exc_info=True)
                partial_explain = None
            
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
                        "performance": {
                            "execution_time_ms": round(partial_query_time, 2),
                            "explain": partial_explain
                        },
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
        Test vector search index queries with performance metrics.
        """
        if not self.db:
            return {"error": "Database not initialized"}
        
        try:
            import time
            
            # Generate a query vector
            query_vector = [random.uniform(-1.0, 1.0) for _ in range(384)]
            
            # Vector search query (using data_imaging pattern) with timing
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
            
            start_time = time.time()
            # Use raw access for vector search aggregation (consistent with data_imaging pattern)
            cur = self.db.raw.embeddings.aggregate(pipeline)
            vector_results = await cur.to_list(length=None)
            vector_query_time = (time.time() - start_time) * 1000
            
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
                        "performance": {
                            "execution_time_ms": round(vector_query_time, 2),
                            "explain": None  # Vector search explain is different
                        },
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
        Test TTL index (check session expiration) with performance metrics.
        """
        if not self.db:
            return {"error": "Database not initialized"}
        
        try:
            import time
            import asyncio
            
            # Count sessions (TTL index should delete old ones)
            start_time = time.time()
            total_sessions = await self.db.sessions.count_documents({})
            count_query_time = (time.time() - start_time) * 1000
            
            # Create a session with old timestamp (should be expired)
            old_session = {
                "user_id": "old_user",
                "session_id": "old_session",
                "active": True,
                "created_at": datetime.datetime.utcnow() - datetime.timedelta(hours=2),  # 2 hours old
                "data": {"note": "This should expire soon"}
            }
            
            await self.db.sessions.insert_one(old_session)
            
            # Wait a moment, then check if it still exists
            await asyncio.sleep(1)
            
            start_time = time.time()
            old_session_check = await self.db.sessions.find_one({"session_id": "old_session"})
            find_query_time = (time.time() - start_time) * 1000
            
            # Get explain plan
            try:
                explain_result = await self.db.raw.sessions.find({
                    "session_id": "old_session"
                }).explain("executionStats")
                ttl_explain = self._extract_explain_info(explain_result)
                logger.debug(f"[IndexingDemo] TTL index explain: {ttl_explain}")
            except Exception as e:
                logger.warning(f"Could not get explain plan: {e}", exc_info=True)
                ttl_explain = None
            
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
                        "performance": {
                            "count_query_time_ms": round(count_query_time, 2),
                            "find_query_time_ms": round(find_query_time, 2),
                            "explain": ttl_explain
                        },
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
    
    async def is_empty(self) -> bool:
        """
        Check if the database collections are empty.
        """
        if not self.db:
            return True
        
        try:
            products_count = await self.db.products.count_documents({})
            sessions_count = await self.db.sessions.count_documents({})
            embeddings_count = await self.db.embeddings.count_documents({})
            logs_count = await self.db.logs.count_documents({})
            
            total = products_count + sessions_count + embeddings_count + logs_count
            return total == 0
        except Exception as e:
            logger.error(f"[IndexingDemo] Error checking if empty: {e}", exc_info=True)
            return True
    
    async def track_user_click(self, action: str, user_info: Optional[Dict[str, Any]] = None) -> None:
        """
        Track user clicks/interactions for analytics.
        Saves to a 'user_clicks' collection.
        """
        if not self.db:
            return
        
        try:
            import datetime
            click_doc = {
                "action": action,
                "timestamp": datetime.datetime.utcnow(),
                "user_info": user_info or {}
            }
            await self.db.user_clicks.insert_one(click_doc)
        except Exception as e:
            logger.warning(f"[IndexingDemo] Error tracking user click: {e}", exc_info=True)
    
    async def clear_all_data(self) -> Dict[str, Any]:
        """
        Clear all demo data.
        """
        if not self.db:
            return {"error": "Database not initialized"}
        
        try:
            products_deleted = await self.db.products.delete_many({})
            sessions_deleted = await self.db.sessions.delete_many({})
            embeddings_deleted = await self.db.embeddings.delete_many({})
            logs_deleted = await self.db.logs.delete_many({})
            
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

