# Hybrid Search Example

This document demonstrates how to configure hybrid search indexes in `manifest.json` for MongoDB Atlas Search with `$rankFusion`.

## Overview

Hybrid search combines vector similarity search with full-text search using MongoDB Atlas's `$rankFusion` operator. This requires two indexes:
1. **Vector Index**: For semantic similarity search using embeddings
2. **Text Index**: For full-text search on string fields

## Manifest Configuration

Use the `"hybrid"` index type to define both indexes together:

```json
{
  "slug": "hybrid_search_demo",
  "name": "Hybrid Search Demo",
  "status": "active",
  "auth_required": false,
  "data_scope": ["self"],
  "managed_indexes": {
    "requests": [
      {
        "name": "hybrid_search_index",
        "type": "hybrid",
        "hybrid": {
          "vector_index": {
            "name": "vector_index",
            "definition": {
              "mappings": {
                "dynamic": false,
                "fields": {
                  "_id": {"type": "objectId"},
                  "doc_text_embedding": {
                    "type": "knnVector",
                    "dimensions": 1536,
                    "similarity": "cosine"
                  },
                  "original_guide_embedding": {
                    "type": "knnVector",
                    "dimensions": 1536,
                    "similarity": "cosine"
                  },
                  "reasoning_embedding": {
                    "type": "knnVector",
                    "dimensions": 1536,
                    "similarity": "cosine"
                  },
                  "input_summary_embedding": {
                    "type": "knnVector",
                    "dimensions": 1536,
                    "similarity": "cosine"
                  }
                }
              }
            }
          },
          "text_index": {
            "name": "text_index",
            "definition": {
              "mappings": {
                "dynamic": true
              }
            }
          }
        }
      }
    ]
  }
}
```

## Index Names

- The base `name` field (`"hybrid_search_index"`) is used for logging and organization
- The `vector_index.name` and `text_index.name` fields specify the actual index names used in queries
- If `name` is not specified in `vector_index` or `text_index`, it defaults to `{base_name}_vector` and `{base_name}_text` respectively
- **Important**: All index names are automatically prefixed with the experiment slug (e.g., `"hybrid_search_demo_vector_index"`). When referencing indexes in queries, use the prefixed names.

## Using Hybrid Search in Queries

Once the indexes are created, you can use `$rankFusion` in your aggregation pipelines:

```python
pipeline = [
    {
        "$rankFusion": {
            "input": {
                "pipelines": {
                    "vectorPipeline": [{
                        "$vectorSearch": {
                            "index": "hybrid_search_demo_vector_index",  // Prefixed with slug
                            "path": "input_summary_embedding",
                            "queryVector": query_embedding,
                            "numCandidates": 100,
                            "limit": 10
                        }
                    }],
                    "fullTextPipeline": [{
                        "$search": {
                            "index": "hybrid_search_demo_text_index",  // Prefixed with slug
                            "text": {
                                "query": query_text,
                                "path": ["user_question", "final_answer", "input_summary"]
                            }
                        }
                    }, {"$limit": 10}]
                }
            },
            "combination": {
                "weights": {
                    "vectorPipeline": 0.7,
                    "fullTextPipeline": 0.3
                }
            },
            "scoreDetails": True
        }
    },
    {
        "$project": {
            "_id": 1,
            "user_question": 1,
            "input_summary": 1,
            "timestamp": 1,
            "scoreDetails": {"$meta": "scoreDetails"}
        }
    },
    {"$limit": 5}
]
```

## Requirements

- MongoDB Atlas 8.1 or higher (for `$rankFusion` support)
- Vector embeddings must match the dimensions specified in the index (e.g., 1536 for OpenAI's `text-embedding-3-small`)
- Both indexes must be queryable before using `$rankFusion`

## Notes

- The vector index uses `mappings.fields` with `knnVector` type (new unified Atlas Search format)
- The text index uses `mappings.dynamic: true` to automatically index all string fields
- Index names in the manifest will be prefixed with the experiment slug when created
- The system automatically waits for both indexes to be queryable before completing

