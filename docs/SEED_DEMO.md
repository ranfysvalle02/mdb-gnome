# Demo Seeding Pattern

This document explains the **demo seeding pattern** established for experiments in the mdb-gnome platform. This pattern provides a clean, reusable way for experiments to automatically seed demo content for demo users.

## Overview

The demo seeding pattern allows experiments to automatically populate demo content when a demo user first accesses the experiment. This creates a better onboarding experience by showing users what the experiment can do with real, meaningful content instead of empty states.

## Configuration

Demo user functionality is configured via the `ENABLE_DEMO` environment variable:

```bash
# Format: ENABLE_DEMO=email:password
# Example:
ENABLE_DEMO=demo@demo.com:demo123
```

- **If `ENABLE_DEMO` is set**: Demo user is enabled and will be created/seeded automatically
- **If `ENABLE_DEMO` is not set**: Demo user functionality is disabled (seeding skipped)

**Legacy Support**: The system also supports separate `DEMO_EMAIL` and `DEMO_PASSWORD` environment variables for backward compatibility, but the combined `ENABLE_DEMO` format is preferred.

## Value Proposition

### 🎯 **Better User Experience**
- Demo users see **meaningful, engaging content** on first visit
- No empty states that require explanation
- Content demonstrates the experiment's capabilities

### 🔄 **Automatic & Clean**
- Seeds automatically when conditions are met
- Never seeds twice (checks for existing content)
- Clean separation of seeding logic from main experiment code

### 📐 **Reusable Pattern**
- Easy to implement in other experiments
- Consistent approach across the platform
- Maintainable and testable

### 🎓 **Educational Value**
- Demo content can showcase best practices
- Real-world examples of how to use the experiment
- Can guide users toward productive workflows

## How It Works

### Architecture

For any experiment implementing this pattern:

```
experiments/[experiment_name]/
├── actor.py              # Main actor with initialize() hook
├── demo_seed.py          # Demo seeding logic (separate file)
└── ...                   # Other experiment files
```

### Key Components

#### 1. **Demo Seed Module** (`demo_seed.py`)

A separate module that contains all demo seeding logic. This keeps the "magic" of demo seeding cleanly separated from the main experiment code.

**Key Functions:**

- `should_seed_demo()` - Checks if seeding should occur
  - Verifies demo user exists
  - Checks if demo user has no existing projects/content
  - Returns tuple: `(should_seed: bool, demo_user_id: str)`

- `seed_demo_content()` - Performs the actual seeding
  - Creates demo projects
  - Adds demo notes/content
  - Creates demo quizzes or other artifacts
  - Returns `bool` indicating success

- `check_and_seed_demo()` - Main entry point
  - Orchestrates the check and seed process
  - Handles errors gracefully
  - Returns `bool` indicating if seeding occurred

#### 2. **Actor Initialization Hook** (`actor.py`)

The actor implements an `initialize()` method that gets called automatically when the actor starts up:

```python
async def initialize(self):
    """
    Post-initialization hook: seeds demo content for demo users.
    This is called automatically when the actor starts up.
    Only seeds if a demo user with demo role exists and has no projects yet.
    """
    if not self.db:
        logger.warning(f"[{self.write_scope}-Actor] Skipping initialize - DB not ready.")
        return
    
    logger.info(f"[{self.write_scope}-Actor] Starting post-initialization setup...")
    
    try:
        from .demo_seed import check_and_seed_demo
        
        # check_and_seed_demo will use config if demo_email is None
        success = await check_and_seed_demo(
            db=self.db,
            mongo_uri=self.mongo_uri,
            db_name=self.db_name,
            demo_email=None  # Will use config.DEMO_EMAIL_DEFAULT if enabled
        )
        # ... logging ...
    except Exception as e:
        logger.error(f"Error during initialization: {e}", exc_info=True)
```

### Seeding Conditions

Demo seeding occurs **only** when **all** of the following conditions are met:

1. ✅ **Demo user exists** - A user with the demo email exists in the system
2. ✅ **No existing content** - The demo user has no projects/content in this experiment
3. ✅ **Actor initialization** - The actor's `initialize()` method is called (automatic on startup)

If any condition is not met, seeding is skipped gracefully.

## Implementation Guide

### Step 1: Create Demo Seed Module

Create a `demo_seed.py` file in your experiment directory:

```python
"""
Demo Seed Module for [Experiment Name]

This module provides demo seeding functionality.
Seeds demo content for demo users, only if they haven't created any content yet.
"""

import logging
import datetime
from typing import Optional, Tuple
from bson.objectid import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

# Import demo config from platform config
from config import DEMO_EMAIL_DEFAULT, DEMO_ENABLED

# Fallback for backward compatibility
if DEMO_EMAIL_DEFAULT is None:
    DEMO_EMAIL_DEFAULT = "demo@demo.com"


async def should_seed_demo(db, mongo_uri: str, db_name: str, demo_email: str = DEMO_EMAIL_DEFAULT) -> Tuple[bool, Optional[str]]:
    """
    Check if demo seeding should occur.
    
    Conditions:
    1. Demo user exists (with matching demo_email)
    2. Demo user has no content in this experiment
    
    Returns:
        tuple: (should_seed: bool, demo_user_id: Optional[str])
    """
    try:
        # Access top-level database to check for demo user
        client = AsyncIOMotorClient(mongo_uri)
        top_level_db = client[db_name]
        
        # Check if demo user exists
        demo_user = await top_level_db.users.find_one({"email": demo_email}, {"_id": 1, "email": 1})
        if not demo_user:
            logger.debug(f"Demo user '{demo_email}' does not exist. Skipping demo seed.")
            client.close()
            return False, None
        
        demo_user_id = str(demo_user["_id"])
        
        # Check if demo user has any content in this experiment
        # Adjust this check based on your experiment's data model
        content_count = await db.your_collection.count_documents({"user_id": ObjectId(demo_user_id)})
        
        client.close()
        
        if content_count > 0:
            logger.debug(f"Demo user '{demo_email}' already has {content_count} item(s). Skipping demo seed.")
            return False, demo_user_id
        
        logger.info(f"Demo user '{demo_email}' exists and has no content. Proceeding with demo seed.")
        return True, demo_user_id
        
    except Exception as e:
        logger.error(f"Error checking if demo seed should occur: {e}", exc_info=True)
        return False, None


async def seed_demo_content(db, demo_user_id: str) -> bool:
    """
    Seed demo content for the experiment.
    
    Args:
        db: ExperimentDB instance (scoped to this experiment)
        demo_user_id: User ID of the demo user (as string)
    
    Returns:
        bool: True if seeding was successful, False otherwise
    """
    try:
        demo_user_obj_id = ObjectId(demo_user_id)
        now = datetime.datetime.utcnow()
        
        # Your seeding logic here
        # Create demo projects, notes, content, etc.
        
        logger.info("✅ Successfully seeded demo content!")
        return True
        
    except Exception as e:
        logger.error(f"Error seeding demo content: {e}", exc_info=True)
        return False


async def check_and_seed_demo(db, mongo_uri: str, db_name: str, demo_email: str = DEMO_EMAIL_DEFAULT) -> bool:
    """
    Main entry point for demo seeding.
    
    Checks if demo seeding should occur and performs it if conditions are met.
    """
    try:
        should_seed, demo_user_id = await should_seed_demo(db, mongo_uri, db_name, demo_email)
        
        if not should_seed:
            logger.debug("Demo seeding skipped (conditions not met or already seeded)")
            return True  # Not an error, just didn't need to seed
        
        if not demo_user_id:
            logger.warning("Demo user ID not found, cannot seed")
            return False
        
        success = await seed_demo_content(db, demo_user_id)
        return success
        
    except Exception as e:
        logger.error(f"Error in check_and_seed_demo: {e}", exc_info=True)
        return False
```

### Step 2: Store Connection Info in Actor

Make sure your actor stores `mongo_uri` and `db_name` for accessing the top-level database:

```python
def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
    self.mongo_uri = mongo_uri  # Store for demo seeding
    self.db_name = db_name      # Store for demo seeding
    self.write_scope = write_scope
    self.read_scopes = read_scopes
    # ... rest of initialization ...
```

### Step 3: Implement Initialize Hook

Add an `initialize()` method to your actor:

```python
async def initialize(self):
    """
    Post-initialization hook: seeds demo content for demo users.
    This is called automatically when the actor starts up.
    """
    if not self.db:
        logger.warning(f"[{self.write_scope}-Actor] Skipping initialize - DB not ready.")
        return
    
    logger.info(f"[{self.write_scope}-Actor] Starting post-initialization setup...")
    
    try:
        from .demo_seed import check_and_seed_demo
        
        # check_and_seed_demo will use config if demo_email is None
        success = await check_and_seed_demo(
            db=self.db,
            mongo_uri=self.mongo_uri,
            db_name=self.db_name,
            demo_email=None  # Will use config.DEMO_EMAIL_DEFAULT if enabled
        )
        
        if success:
            logger.info(f"[{self.write_scope}-Actor] Demo seeding check completed.")
        else:
            logger.warning(f"[{self.write_scope}-Actor] Demo seeding check failed or was skipped.")
            
    except Exception as e:
        logger.error(f"[{self.write_scope}-Actor] Error during initialization: {e}", exc_info=True)
    
    logger.info(f"[{self.write_scope}-Actor] Post-initialization setup complete.")
```

That's it! The platform automatically calls `initialize()` when your actor starts up.

## Example: StoryWeaver Implementation

StoryWeaver (see `experiments/storyweaver/demo_seed.py`) seeds:
- **1 Educational Project** with comprehensive notes on:
  - Math/Algebra (Slope, Intercepts, Line Equations)
  - Spanish (Pronouns, Adverbs - 8th grade level)
  - Basketball IQ (Offensive & Defensive strategies)
- **1 Story Project** with narrative notes
- **1 Quiz** covering all educational topics

This gives demo users immediate, meaningful content that demonstrates StoryWeaver's capabilities.

You can reference `experiments/storyweaver/demo_seed.py` as a complete implementation example.

## Best Practices

### ✅ Do

- **Keep seeding logic separate** - Put it in `demo_seed.py`, not mixed with main code
- **Check before seeding** - Always verify the user has no existing content
- **Make content meaningful** - Seed real, useful content that showcases capabilities
- **Use appropriate tags** - Tag demo content appropriately for organization
- **Handle errors gracefully** - Log errors but don't crash the actor initialization
- **Document your seeding** - Add comments explaining what gets seeded and why
- **Keep content age-appropriate** - If your experiment targets specific age groups, seed appropriate content

### ❌ Don't

- **Don't seed multiple times** - Always check for existing content first
- **Don't seed for non-demo users** - Only seed for users with the demo email
- **Don't mix seeding with business logic** - Keep it separate and clean
- **Don't seed too much** - Keep it focused and meaningful, not overwhelming
- **Don't hardcode user IDs** - Use the demo user lookup pattern
- **Don't seed without checking** - Always verify conditions before seeding

## Advanced Patterns

### Conditional Seeding by Role

If you need stricter role checking, you can enhance `should_seed_demo()` to check roles via the authz provider. However, the current pattern (checking email + no existing content) is usually sufficient.

**Note:** The current implementation assumes that if a user with `demo_email` exists, they have the demo role. For stricter role checking, you'd need to pass the authz provider to the seeding functions, which adds complexity.

### Multi-Step Seeding

For complex experiments, you might want to seed in stages:

```python
async def seed_demo_content(db, demo_user_id: str) -> bool:
    """Seed demo content in stages."""
    try:
        # Stage 1: Create projects/containers
        await _seed_projects(db, demo_user_id)
        
        # Stage 2: Add content to projects
        await _seed_content(db, demo_user_id)
        
        # Stage 3: Create artifacts (quizzes, exports, etc.)
        await _seed_artifacts(db, demo_user_id)
        
        return True
    except Exception as e:
        logger.error(f"Error seeding demo content: {e}", exc_info=True)
        return False
```

### Seeding Based on Experiment Type

You could customize seeding based on experiment configuration or user preferences, though the current simple pattern works well for most cases.

## Testing Demo Seeding

### Manual Testing

1. **Ensure demo user exists**:
   ```bash
   # Check if demo user exists in database
   # User should have email: demo@demo.com (or configured DEMO_EMAIL)
   ```

2. **Clear any existing content** (if testing re-seeding):
   ```python
   # In your experiment database, remove projects/content for demo user
   ```

3. **Restart the actor**:
   ```bash
   # Restart the application or reload experiments
   # Watch logs for "Demo seeding check completed"
   ```

4. **Verify content exists**:
   - Log in as demo user
   - Check that demo content appears
   - Verify it matches your seed configuration

### Automated Testing

You could create tests that:
- Mock the database
- Verify `should_seed_demo()` logic
- Test seeding with various conditions
- Ensure idempotency (doesn't seed twice)

## Troubleshooting

### Seeding Not Occurring

**Check:**
1. ✅ Demo user exists in top-level `users` collection
2. ✅ Demo user has no existing content in experiment
3. ✅ Actor `initialize()` method is implemented correctly
4. ✅ `demo_seed.py` module imports successfully
5. ✅ Check application logs for errors
6. ✅ Actor is actually starting up (check Ray logs)

### Seeding Multiple Times

**Cause:** Logic not checking for existing content properly

**Fix:** Ensure `should_seed_demo()` checks for existing projects/content:
```python
content_count = await db.your_collection.count_documents({"user_id": ObjectId(demo_user_id)})
if content_count > 0:
    return False, demo_user_id  # Already has content, don't seed
```

### Database Connection Issues

**Cause:** Cannot access top-level database to check for demo user

**Fix:** Ensure `mongo_uri` and `db_name` are stored in actor and passed correctly to `check_and_seed_demo()`

### Import Errors

**Cause:** `demo_seed.py` cannot be imported from actor

**Fix:** Ensure the file is in the same directory as `actor.py` and uses relative imports:
```python
from .demo_seed import check_and_seed_demo
```

## Conclusion

The demo seeding pattern provides a clean, reusable way to enhance the demo user experience across experiments. By following this pattern:

- ✅ **Separation of concerns** - Seeding logic is separate from business logic
- ✅ **Automatic execution** - Happens on actor startup via `initialize()` hook
- ✅ **Idempotent** - Never seeds twice, gracefully skips if content exists
- ✅ **Maintainable** - Easy to understand, modify, and extend
- ✅ **Reusable** - Same pattern works for any experiment

This pattern can be adopted by any experiment in the platform to provide better demo experiences for users.

### Next Steps

1. **Review StoryWeaver implementation** - See `experiments/storyweaver/demo_seed.py` for a complete example
2. **Implement in your experiment** - Follow the implementation guide above
3. **Customize for your needs** - Adapt the seeding logic to your experiment's data model and demo content needs
4. **Test thoroughly** - Ensure seeding works correctly and doesn't interfere with normal operation

---

**Questions or improvements?** Consider this a living document that can be updated as the pattern evolves across experiments!

