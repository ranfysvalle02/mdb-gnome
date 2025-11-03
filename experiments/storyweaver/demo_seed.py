"""
Demo Seed Module for StoryWeaver

This module provides demo seeding functionality for StoryWeaver experiment.
It seeds educational content for demo users with the 'demo' role, only if they haven't
created any projects yet.

This establishes a clean pattern for experiments to handle demo-specific seeding logic.
"""

import logging
import datetime
from typing import Optional, Tuple
from bson.objectid import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

# Demo user configuration - import from config
from config import DEMO_EMAIL_DEFAULT, DEMO_ENABLED

# Fallback for backward compatibility
if DEMO_EMAIL_DEFAULT is None:
    DEMO_EMAIL_DEFAULT = "demo@demo.com"


async def should_seed_demo(db, mongo_uri: str, db_name: str, demo_email: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    Check if demo seeding should occur.
    
    Conditions:
    1. Experiment-specific demo user exists (with matching demo_email in experiment's users collection)
    2. Demo user has no projects in StoryWeaver
    
    Note: This function checks the experiment-specific users collection (created by sub-auth),
    not the top-level users collection. This ensures compatibility with sub-auth demo user seeding.
    
    Args:
        db: ExperimentDB instance (scoped to StoryWeaver)
        mongo_uri: MongoDB connection URI (for accessing top-level database)
        db_name: Database name (for accessing top-level database)
        demo_email: Email of the demo user (default: None, will use config.DEMO_EMAIL_DEFAULT if enabled)
    
    Returns:
        tuple: (should_seed: bool, demo_user_id: Optional[str])
               If should_seed is True, demo_user_id will be the ObjectId as a string (from experiment's users collection)
    """
    # Use provided email or fallback to config
    if demo_email is None:
        if not DEMO_ENABLED or DEMO_EMAIL_DEFAULT is None:
            logger.debug("Demo seeding disabled: ENABLE_DEMO not configured.")
            return False, None
        demo_email = DEMO_EMAIL_DEFAULT
    
    try:
        # Check if experiment-specific demo user exists (created by sub-auth)
        # ExperimentDB provides MongoDB-style access via collection() method
        # For backward compatibility, also support ScopedMongoWrapper via getattr
        if hasattr(db, 'collection'):
            # ExperimentDB: use collection() method for cleaner API
            demo_user = await db.collection("users").find_one({"email": demo_email}, {"_id": 1, "email": 1})
        else:
            # ScopedMongoWrapper: use getattr for attribute access
            users_collection = getattr(db, "users")
            demo_user = await users_collection.find_one({"email": demo_email}, {"_id": 1, "email": 1})
        
        if not demo_user:
            logger.debug(f"Experiment-specific demo user '{demo_email}' does not exist in StoryWeaver users collection. Skipping demo seed.")
            return False, None
        
        demo_user_id = str(demo_user["_id"])
        
        # Check if demo user has any projects in StoryWeaver
        # If they have projects, check if they also have notes (seeding might have failed partway)
        # ExperimentDB provides MongoDB-style access via attribute access
        if hasattr(db, 'projects'):
            # ExperimentDB: use attribute access for collections
            project_count = await db.projects.count_documents({"user_id": ObjectId(demo_user_id)})
            note_count = await db.notes.count_documents({"user_id": ObjectId(demo_user_id)})
        else:
            # ScopedMongoWrapper: use getattr for attribute access
            projects_collection = getattr(db, "projects")
            project_count = await projects_collection.count_documents({"user_id": ObjectId(demo_user_id)})
            notes_collection = getattr(db, "notes")
            note_count = await notes_collection.count_documents({"user_id": ObjectId(demo_user_id)})
        
        print(f"📊 should_seed_demo: Demo user '{demo_email}' has {project_count} project(s) and {note_count} note(s)", flush=True)
        logger.info(f"should_seed_demo: Demo user '{demo_email}' has {project_count} project(s) and {note_count} note(s)")
        
        # If user has projects but no notes, seeding likely failed - re-seed
        if project_count > 0 and note_count == 0:
            print(f"⚠️  Demo user '{demo_email}' has {project_count} project(s) but NO NOTES. Re-seeding content.", flush=True)
            logger.warning(f"Demo user '{demo_email}' has {project_count} project(s) but NO NOTES. Re-seeding content.")
            return True, demo_user_id
        
        # If user has projects with notes, check if we have all 4 projects AND if there are duplicates
        if project_count > 0 and note_count > 0:
            # Check for unique project names (should be exactly 4: Math, Spanish, Basketball, Story)
            if hasattr(db, 'projects'):
                unique_names = await db.projects.distinct("name", {"user_id": ObjectId(demo_user_id)})
            else:
                projects_collection = getattr(db, "projects")
                unique_names = await projects_collection.distinct("name", {"user_id": ObjectId(demo_user_id)})
            
            unique_count = len(unique_names)
            print(f"📊 should_seed_demo: Demo user '{demo_email}' has {project_count} project(s) total, {unique_count} unique project name(s): {unique_names}", flush=True)
            logger.info(f"should_seed_demo: Demo user '{demo_email}' has {project_count} project(s) total, {unique_count} unique project name(s): {unique_names}")
            
            # If we have duplicates or missing projects, re-seed
            if unique_count < 4 or project_count != unique_count:
                print(f"⚠️  Demo user '{demo_email}' has {project_count} project(s) but only {unique_count} unique. Expected 4 unique projects. Re-seeding to fix.", flush=True)
                logger.warning(f"Demo user '{demo_email}' has {project_count} project(s) but only {unique_count} unique. Expected 4 unique projects. Re-seeding to fix.")
                return True, demo_user_id
            
            # Check that we have all 4 required projects
            required_names = {"Math & Algebra - 8th Grade", "Spanish - 8th Grade", "Basketball IQ - 8th Grade", "My Story Collection"}
            existing_names = set(unique_names)
            missing_names = required_names - existing_names
            
            if missing_names:
                print(f"⚠️  Demo user '{demo_email}' missing projects: {missing_names}. Re-seeding to create missing projects.", flush=True)
                logger.warning(f"Demo user '{demo_email}' missing projects: {missing_names}. Re-seeding to create missing projects.")
                return True, demo_user_id
            
            print(f"✅ Demo user '{demo_email}' already has all 4 unique projects. Skipping seed.", flush=True)
            logger.info(f"Demo user '{demo_email}' already has all 4 unique projects in StoryWeaver. Skipping demo seed.")
            return False, demo_user_id
        
        logger.info(f"Demo user '{demo_email}' exists in StoryWeaver and has no projects. Proceeding with demo seed.")
        return True, demo_user_id
        
    except Exception as e:
        logger.error(f"Error checking if demo seed should occur: {e}", exc_info=True)
        return False, None


async def seed_demo_content(db, demo_user_id: str) -> bool:
    """
    Seed demo content for StoryWeaver.
    
    Creates 4 separate projects (8th Grade / Middle School focused):
    1. Math & Algebra Project: Slope, Y Intercept, X Intercept, Line Equations
    2. Spanish Project: Pronouns, Adverbs
    3. Basketball IQ Project: Strategy, offense, defense
    4. Story Collection: Creative writing examples
    
    Args:
        db: ExperimentDB instance (scoped to StoryWeaver)
        demo_user_id: User ID of the demo user (as string)
    
    Returns:
        bool: True if seeding was successful, False otherwise
    """
    try:
        print(f"🌱 Starting demo content seeding for user_id={demo_user_id}", flush=True)
        logger.info(f"Starting demo content seeding for user_id={demo_user_id}")
        demo_user_obj_id = ObjectId(demo_user_id)
        now = datetime.datetime.utcnow()
        
        # Helper function to insert project (only if it doesn't already exist)
        async def insert_project(project_data):
            try:
                project_name = project_data.get('name')
                user_id = project_data.get('user_id')
                
                # Check if project with this name already exists for this user
                if hasattr(db, 'projects'):
                    existing = await db.projects.find_one({"name": project_name, "user_id": user_id})
                else:
                    projects_collection = getattr(db, "projects")
                    existing = await projects_collection.find_one({"name": project_name, "user_id": user_id})
                
                if existing:
                    logger.debug(f"⚠️  Project '{project_name}' already exists for user {user_id}, skipping creation")
                    print(f"⚠️  Project '{project_name}' already exists, skipping creation", flush=True)
                    return existing['_id']
                
                # Project doesn't exist, create it
                if hasattr(db, 'projects'):
                    result = await db.projects.insert_one(project_data)
                else:
                    projects_collection = getattr(db, "projects")
                    result = await projects_collection.insert_one(project_data)
                logger.debug(f"✅ Inserted project: {project_name} -> {result.inserted_id}")
                return result.inserted_id
            except Exception as e:
                logger.error(f"❌ Failed to insert project {project_data.get('name')}: {e}", exc_info=True)
                raise
        
        # Helper function to insert note
        async def insert_note(note_data):
            try:
                if hasattr(db, 'notes'):
                    await db.notes.insert_one(note_data)
                else:
                    notes_collection = getattr(db, "notes")
                    await notes_collection.insert_one(note_data)
            except Exception as e:
                logger.error(f"Failed to insert note for project_id={note_data.get('project_id')}: {e}", exc_info=True)
                raise
        
        # Helper function to insert quiz
        async def insert_quiz(quiz_data):
            try:
                if hasattr(db, 'quizzes'):
                    await db.quizzes.insert_one(quiz_data)
                else:
                    quizzes_collection = getattr(db, "quizzes")
                    await quizzes_collection.insert_one(quiz_data)
            except Exception as e:
                logger.error(f"Failed to insert quiz for project_id={quiz_data.get('project_id')}: {e}", exc_info=True)
                raise
        
        # ========================================================================
        # PROJECT 1: Math/Algebra (8th Grade / Middle School)
        # ========================================================================
        math_project = {
            "name": "Math & Algebra - 8th Grade",
            "project_goal": "Master key algebra concepts: Slope, Intercepts, and Linear Equations (8th Grade / Middle School)",
            "project_type": "educational",
            "created_at": now,
            "user_id": demo_user_obj_id
        }
        math_project_id = await insert_project(math_project)
        print(f"✅ Created PROJECT 1/4: '{math_project['name']}' with ID: {math_project_id}", flush=True)
        logger.info(f"✅ Created PROJECT 1/4: '{math_project['name']}' with ID: {math_project_id}")
        
        math_notes = [
            "**What is Slope? (8th Grade)** Slope is how steep a line is. Think of it like a hill - a steep hill has a big slope, a flat road has a slope of zero. The key phrase to remember is 'Rise over Run' - how much the line goes UP divided by how much it goes OVER to the right.",
            "**Finding Slope from a Graph (8th Grade)** Step 1: Pick two points on the line (where the line crosses grid lines makes it easiest). Step 2: Count how many spaces UP or DOWN you go from the first point to the second (this is your Rise). Step 3: Count how many spaces RIGHT or LEFT you go (this is your Run). Step 4: Divide Rise by Run. Going UP is positive, going DOWN is negative. Going RIGHT is positive.",
            "**Slope Formula (8th Grade)** If you have two points, say point A at (2, 3) and point B at (6, 11), find the slope like this: First, subtract the y-values: 11 minus 3 = 8 (that's your rise). Then, subtract the x-values: 6 minus 2 = 4 (that's your run). Divide: 8 divided by 4 = 2. So the slope is 2, which means the line goes up 2 spaces for every 1 space to the right.",
            "**Four Types of Slope (8th Grade)** Positive slope means the line goes uphill from left to right - like climbing a mountain. Negative slope means the line goes downhill from left to right - like skiing down a slope. Zero slope means the line is perfectly flat - like a horizon. Undefined slope means the line is perfectly vertical - like a wall (you can't divide by zero, so the slope is undefined).",
            "**Y-Intercept Explained (8th Grade)** The y-intercept is where the line crosses the vertical line (the y-axis). At this exact point, x is always zero. To find it on a graph: just look where your line touches the left or right edge of the graph. To find it in an equation: replace x with 0 and solve. Example: If a line crosses the y-axis at 4, the point is (0, 4) - meaning when x is zero, y is 4.",
            "**X-Intercept Explained (8th Grade)** The x-intercept is where the line crosses the horizontal line (the x-axis). At this exact point, y is always zero. To find it on a graph: look where your line touches the top or bottom edge of the graph. To find it in an equation: replace y with 0 and solve. Example: If a line crosses the x-axis at -2, the point is (-2, 0) - meaning when y is zero, x is -2.",
            "**Slope-Intercept Form (8th Grade)** This is the easiest form: y = mx + b. The letter m is the slope, and the letter b is the y-intercept. It's great because you can see both important numbers right away! Example: y = 2x - 3 means the slope is 2 (go up 2, over 1) and the y-intercept is -3 (the line starts at the point (0, -3)).",
            "**Point-Slope Form (8th Grade)** Use this when you know one point and the slope. The formula looks like: y minus y1 equals m times (x minus x1). The (x1, y1) is the point you know, and m is the slope. Example: If you know the slope is 5 and the line passes through point (2, 8), you write: y - 8 = 5(x - 2). This form is super helpful when you're given a point and a slope!",
            "**Standard Form (8th Grade)** This form looks like: Ax + By = C, where A, B, and C are just regular numbers. It's not as friendly as slope-intercept form, but it's great for finding intercepts. To find the y-intercept: pretend the x term doesn't exist (cover it up or set it to 0) and solve for y. To find the x-intercept: pretend the y term doesn't exist (cover it up or set it to 0) and solve for x.",
            "**Cover-Up Method Example (8th Grade)** For the equation 3x + 2y = 12: To find y-intercept, ignore 3x (pretend it's zero), so you get 2y = 12. Divide both sides by 2 to get y = 6. So the y-intercept is (0, 6). To find x-intercept, ignore 2y (pretend it's zero), so you get 3x = 12. Divide both sides by 3 to get x = 4. So the x-intercept is (4, 0)."
        ]
        
        for note_content in math_notes:
            await insert_note({
                "project_id": math_project_id,
                "user_id": demo_user_obj_id,
                "content": note_content,
                "timestamp": now,
                "contributor_label": "Demo Content",
                "tags": ["math", "algebra", "linear-equations", "slope", "intercepts", "8th-grade", "middle-school"]
            })
        
        logger.info(f"Created {len(math_notes)} notes for math project")
        
        # Create quiz for math project
        math_quiz = {
            "user_id": demo_user_obj_id,
            "project_id": math_project_id,
            "title": "Math Check: Slope & Intercepts (8th Grade)",
            "quiz_data": [
                {
                    "question": "What is the slope formula when given two points ($x_1, y_1$) and ($x_2, y_2$)?",
                    "options": [
                        "$m = x_2 - x_1$",
                        "$m = \\frac{y_2 - y_1}{x_2 - x_1}$",
                        "$m = y_2 + y_1$",
                        "$m = \\frac{x_2 - x_1}{y_2 - y_1}$"
                    ],
                    "correct_answer_index": 1
                },
                {
                    "question": "In the equation y = 2x - 3, what is the y-intercept?",
                    "options": ["(0, 2)", "(0, -3)", "(-3, 0)", "(2, -3)"],
                    "correct_answer_index": 1
                },
                {
                    "question": "What is the 'Cover-Up' method used for in Standard Form equations?",
                    "options": [
                        "Finding the slope",
                        "Finding intercepts quickly",
                        "Graphing the line",
                        "Simplifying fractions"
                    ],
                    "correct_answer_index": 1
                }
            ],
            "question_type": "Multiple Choice",
            "source_note_ids": [],
            "created_at": now,
            "share_token": None
        }
        await insert_quiz(math_quiz)
        logger.info("Created quiz for math project")
        
        # ========================================================================
        # PROJECT 2: Spanish (8th Grade / Middle School)
        # ========================================================================
        spanish_project = {
            "name": "Spanish - 8th Grade",
            "project_goal": "Master Spanish pronouns and adverbs (8th Grade / Middle School)",
            "project_type": "educational",
            "created_at": now,
            "user_id": demo_user_obj_id
        }
        spanish_project_id = await insert_project(spanish_project)
        print(f"✅ Created PROJECT 2/4: '{spanish_project['name']}' with ID: {spanish_project_id}", flush=True)
        logger.info(f"✅ Created PROJECT 2/4: '{spanish_project['name']}' with ID: {spanish_project_id}")
        
        spanish_notes = [
            "**What are Pronouns? (8th Grade)** Pronouns are words that replace nouns so you don't have to repeat them. In English, instead of saying 'Maria is tall. Maria is smart,' you say 'Maria is tall. She is smart.' The word 'she' replaces 'Maria'. Spanish works the same way!",
            "**Subject Pronouns in Spanish (8th Grade)** These tell you WHO is doing the action. Think of them like the actors in a sentence. Yo means I. Tú means you (but only with friends and family - it's informal). Usted also means you, but it's formal - use it with teachers and adults. Él means he. Ella means she. Nosotros means we (masculine or mixed group). Nosotras means we (all girls). Ellos means they (masculine or mixed). Ellas means they (all girls).",
            "**The Cool Thing About Spanish (8th Grade)** In Spanish, you can usually drop the pronoun! In English you MUST say 'I am going,' but in Spanish you can just say 'Voy' (I am going) because the verb ending already tells you it's 'I'. The verb 'voy' means 'I go' - you don't need to add 'Yo' unless you want to emphasize it.",
            "**What are Adverbs? (8th Grade)** Adverbs are words that describe HOW, WHEN, WHERE, or HOW MUCH something happens. In English: quickly (HOW), today (WHEN), here (WHERE), very (HOW MUCH). Spanish has the same idea!",
            "**Making Adverbs from Adjectives (8th Grade)** Most Spanish adverbs are easy to make. Take an adjective like rápido (fast), make it feminine (rápida), then add -mente. So rápido becomes rápidamente (quickly). It's like adding -ly in English! Another example: lento (slow) becomes lentamente (slowly). Perfecto (perfect) becomes perfectamente (perfectly).",
            "**Time Adverbs You Need to Know (8th Grade)** hoy = today. ayer = yesterday. mañana = tomorrow. siempre = always. nunca = never. a veces = sometimes. ahora = now. Memorize these - you'll use them constantly!",
            "**Place Adverbs (8th Grade)** aquí = here. allí or allá = there (they're pretty much the same). cerca = near. lejos = far. These help you describe where things are or where actions happen.",
            "**Quantity Adverbs (8th Grade)** mucho = a lot. poco = a little. muy = very (use it like 'muy rápido' means 'very fast'). más = more. menos = less. These help you say HOW MUCH of something there is.",
            "**Manner Adverbs (8th Grade)** bien = well. mal = badly. así = like this / so. These describe HOW something is done. Example: 'I speak Spanish well' = 'Hablo español bien'. 'I sing badly' = 'Canto mal'.",
            "**When to Use Tú vs Usted (8th Grade)** This is about respect and formality. Use TÚ with: friends your age, family members, people you know well. Use USTED with: teachers, principals, adults you don't know well, anyone you want to show respect to. In Florida and Latin America, use USTEDES (not vosotros) when saying 'you all' to a group."
        ]
        
        for note_content in spanish_notes:
            await insert_note({
                "project_id": spanish_project_id,
                "user_id": demo_user_obj_id,
                "content": note_content,
                "timestamp": now,
                "contributor_label": "Demo Content",
                "tags": ["spanish", "grammar", "pronouns", "adverbs", "8th-grade", "middle-school"]
            })
        
        logger.info(f"Created {len(spanish_notes)} notes for Spanish project")
        
        # Create quiz for Spanish project
        spanish_quiz = {
            "user_id": demo_user_obj_id,
            "project_id": spanish_project_id,
            "title": "Spanish Check: Pronouns & Adverbs (8th Grade)",
            "quiz_data": [
                {
                    "question": "In Spanish, when should you use 'Tú' vs 'Usted'?",
                    "options": [
                        "Tú is formal, Usted is informal",
                        "Tú is for friends/family, Usted is for teachers/adults",
                        "There is no difference",
                        "Tú is only used in Spain"
                    ],
                    "correct_answer_index": 1
                },
                {
                    "question": "How do you form the Spanish adverb from the adjective 'rápido' (fast)?",
                    "options": [
                        "Add -ly: rapidoly",
                        "Add -mente to feminine form: rápidamente",
                        "Use the same word: rápido",
                        "Add -mente directly: rápidomente"
                    ],
                    "correct_answer_index": 1
                },
                {
                    "question": "What does 'siempre' mean in Spanish?",
                    "options": ["Sometimes", "Always", "Never", "Today"],
                    "correct_answer_index": 1
                }
            ],
            "question_type": "Multiple Choice",
            "source_note_ids": [],
            "created_at": now,
            "share_token": None
        }
        await insert_quiz(spanish_quiz)
        logger.info("Created quiz for Spanish project")
        
        # ========================================================================
        # PROJECT 3: Basketball IQ (8th Grade / Middle School)
        # ========================================================================
        basketball_project = {
            "name": "Basketball IQ - 8th Grade",
            "project_goal": "Master basketball strategy and court awareness (8th Grade / Middle School)",
            "project_type": "educational",
            "created_at": now,
            "user_id": demo_user_obj_id
        }
        basketball_project_id = await insert_project(basketball_project)
        print(f"✅ Created PROJECT 3/4: '{basketball_project['name']}' with ID: {basketball_project_id}", flush=True)
        logger.info(f"✅ Created PROJECT 3/4: '{basketball_project['name']}' with ID: {basketball_project_id}")
        
        basketball_notes = [
            "**What is Basketball IQ? (8th Grade)** Basketball IQ isn't about being the tallest or fastest player. It's about understanding the game and making smart decisions. A high-IQ player reads the court, sees plays developing, and helps their whole team play better. It's the difference between someone who plays hard and someone who plays smart.",
            "**The Spacing Rule (8th Grade)** This is the most important rule: DON'T follow the ball around. DON'T stand right next to your teammate. DO spread out! Imagine each player has their own spot on the court, like the spots on a 3-point shooting rack. When players spread out, it creates open paths to the basket (called 'driving lanes') and makes it much harder for the defense to guard everyone.",
            "**What to Do Without the Ball (8th Grade)** Don't just stand there watching the person dribbling! Be active. Move! Here are three moves to master: (1) CUT - When your defender looks away, run hard straight to the basket, ready to catch a pass. (2) SET A SCREEN - Run over and stand legally in front of the person guarding your teammate to help them get open. (3) V-CUT - Walk your defender toward the basket, then quickly change direction and cut back out to get open for a pass.",
            "**Good Shot vs Bad Shot (8th Grade)** Bad basketball IQ: Taking a rushed, contested shot with lots of time left on the shot clock. Good basketball IQ: Taking an open shot that you've practiced a hundred times, when you're in rhythm, and it fits the offense. Ask yourself: Is this the best shot we can get right now?",
            "**The Extra Pass Philosophy (8th Grade)** Don't take a decent shot if your teammate has a great shot. Passing up your own good shot to give someone else a great shot shows high basketball IQ. It builds team chemistry and trust. Teams that share the ball win more games.",
            "**Defensive Positioning (8th Grade)** Remember 'Ball-You-Man' - always know where the BALL is, where YOU are, and where your MAN (the person you're guarding) is. Always stay between your player and the basket. If you're guarding the ball, pressure them. If you're one pass away, get ready to help but also stay ready to guard your player. If you're far from the ball, position yourself to help stop anyone driving to the basket.",
            "**Help Defense Basics (8th Grade)** If your teammate gets beat by their player, don't just watch! Your teammate needs help. Quickly slide over (called 'stunting') to stop the ball-handler from getting an easy basket. Your first job is to STOP THE BALL. Then worry about your own player. Help defense wins games.",
            "**Communication is Everything (8th Grade)** A quiet defense is a weak defense. Talk constantly! Yell 'BALL! BALL! BALL!' when you're guarding the ball-handler. Shout 'SCREEN LEFT!' or 'SCREEN RIGHT!' to warn your teammate. Call out 'I GOT YOUR HELP!' to let teammates know you're ready. Yell 'SHOT!' when someone shoots so everyone boxes out and rebounds.",
            "**Boxing Out Technique (8th Grade)** After a shot goes up, don't just turn and watch the ball! Find the person you're guarding, step in front of them, make contact with your back or hip, and seal them away from the basket. Do this BEFORE you try to get the rebound. Good boxing out beats jumping ability every time.",
            "**Game Awareness (8th Grade)** Always know what's happening in the game. Check the score and time - if you're winning by 10 with one minute left, don't take a bad shot. Keep track of fouls - know if your team is in the 'bonus' (meaning the other team shoots free throws on every foul). Know your matchup - who are you guarding? Are they fast? Can they shoot? Force fast players to use their weak hand. Don't leave good shooters open!"
        ]
        
        for note_content in basketball_notes:
            await insert_note({
                "project_id": basketball_project_id,
                "user_id": demo_user_obj_id,
                "content": note_content,
                "timestamp": now,
                "contributor_label": "Demo Content",
                "tags": ["basketball", "strategy", "sports-iq", "offense", "defense", "8th-grade", "middle-school"]
            })
        
        logger.info(f"Created {len(basketball_notes)} notes for basketball project")
        
        # Create quiz for basketball project
        basketball_quiz = {
            "user_id": demo_user_obj_id,
            "project_id": basketball_project_id,
            "title": "Basketball IQ Check (8th Grade)",
            "quiz_data": [
                {
                    "question": "Why is spacing the #1 rule in basketball offense?",
                    "options": [
                        "It makes the court look organized",
                        "It creates driving lanes and makes it harder for one defender to guard two players",
                        "It uses less energy",
                        "It makes the game more fun to watch"
                    ],
                    "correct_answer_index": 1
                },
                {
                    "question": "What is the 'Ball-You-Man' principle in basketball defense?",
                    "options": [
                        "Always know where the ball, you, and your man are positioned",
                        "Always be between your player and the basket",
                        "Always double-team the ball handler",
                        "Always guard the best player"
                    ],
                    "correct_answer_index": 1
                },
                {
                    "question": "Why is communication important in basketball defense?",
                    "options": [
                        "It makes you look more professional",
                        "A quiet defense is a bad defense - yelling helps coordinate help defense and warns teammates",
                        "It intimidates the other team",
                        "It's required by the rules"
                    ],
                    "correct_answer_index": 1
                }
            ],
            "question_type": "Multiple Choice",
            "source_note_ids": [],
            "created_at": now,
            "share_token": None
        }
        await insert_quiz(basketball_quiz)
        logger.info("Created quiz for basketball project")
        
        # ========================================================================
        # PROJECT 4: Story Collection
        # ========================================================================
        story_project = {
            "name": "My Story Collection",
            "project_goal": "Capture ideas and weave them into compelling narratives",
            "project_type": "story",
            "created_at": now,
            "user_id": demo_user_obj_id
        }
        story_project_id = await insert_project(story_project)
        print(f"✅ Created PROJECT 4/4: '{story_project['name']}' with ID: {story_project_id}", flush=True)
        logger.info(f"✅ Created PROJECT 4/4: '{story_project['name']}' with ID: {story_project_id}")
        
        story_notes = [
            "The old lighthouse keeper watched the storm approach from the east. Years of experience told him this would be different - the barometer had dropped further than he'd ever seen.",
            "In the library's quiet corner, Maya discovered a book bound in leather so old it crumbled at her touch. Inside, handwritten entries spoke of a city that shouldn't exist.",
            "Three friends, one promise: they would always meet here at sunset, no matter what. But tonight, only two showed up, and they knew their world had changed forever.",
            "The letter arrived on a Tuesday, marked with a stamp from a country that had been gone for thirty years. Inside, a single sentence: 'The time has come.'",
            "Every morning, she walked the same path through the garden. Today, she noticed something different - a door where yesterday there had only been a wall."
        ]
        
        for i, note_content in enumerate(story_notes):
            await insert_note({
                "project_id": story_project_id,
                "user_id": demo_user_obj_id,
                "content": note_content,
                "timestamp": now + datetime.timedelta(minutes=i * 5),
                "contributor_label": "Demo Content",
                "tags": ["story", "narrative", "fiction"]
            })
        
        logger.info(f"Created {len(story_notes)} notes for story project")
        
        # Verify all projects were created
        if hasattr(db, 'projects'):
            # ExperimentDB: use attribute access
            project_count = await db.projects.count_documents({"user_id": demo_user_obj_id})
            note_count = await db.notes.count_documents({"user_id": demo_user_obj_id})
            quiz_count = await db.quizzes.count_documents({"user_id": demo_user_obj_id})
        else:
            # ScopedMongoWrapper: use getattr for attribute access
            projects_collection = getattr(db, "projects")
            notes_collection = getattr(db, "notes")
            quizzes_collection = getattr(db, "quizzes")
            project_count = await projects_collection.count_documents({"user_id": demo_user_obj_id})
            note_count = await notes_collection.count_documents({"user_id": demo_user_obj_id})
            quiz_count = await quizzes_collection.count_documents({"user_id": demo_user_obj_id})
        
        logger.info("✅ Successfully seeded demo content for StoryWeaver!")
        logger.info(f"   - Math & Algebra project: {len(math_notes)} notes + quiz")
        logger.info(f"   - Spanish project: {len(spanish_notes)} notes + quiz")
        logger.info(f"   - Basketball IQ project: {len(basketball_notes)} notes + quiz")
        logger.info(f"   - Story Collection: {len(story_notes)} notes")
        logger.info(f"   - Verification: {project_count} projects, {note_count} notes, {quiz_count} quizzes created")
        
        if project_count != 4:
            logger.warning(f"⚠️ Expected 4 projects but found {project_count}")
        if note_count != (len(math_notes) + len(spanish_notes) + len(basketball_notes) + len(story_notes)):
            logger.warning(f"⚠️ Expected {len(math_notes) + len(spanish_notes) + len(basketball_notes) + len(story_notes)} notes but found {note_count}")
        if quiz_count != 3:
            logger.warning(f"⚠️ Expected 3 quizzes but found {quiz_count}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error seeding demo content: {e}", exc_info=True)
        return False


async def _cleanup_duplicate_projects(db, user_id: str):
    """
    Remove duplicate projects for a user, keeping only the first occurrence of each project name.
    """
    try:
        from bson import ObjectId
        user_obj_id = ObjectId(user_id)
        
        # Get all projects for this user
        if hasattr(db, 'projects'):
            projects = await db.projects.find({"user_id": user_obj_id}).to_list(length=None)
        else:
            projects_collection = getattr(db, "projects")
            projects = await projects_collection.find({"user_id": user_obj_id}).to_list(length=None)
        
        if not projects:
            return
        
        # Group projects by name
        projects_by_name = {}
        for project in projects:
            name = project.get('name')
            if name not in projects_by_name:
                projects_by_name[name] = []
            projects_by_name[name].append(project)
        
        # Find and remove duplicates (keep the oldest one)
        duplicates_removed = 0
        for name, project_list in projects_by_name.items():
            if len(project_list) > 1:
                # Sort by created_at, keep the oldest one
                project_list.sort(key=lambda p: p.get('created_at', datetime.datetime.min))
                # Remove all but the first (oldest)
                for project_to_remove in project_list[1:]:
                    project_id = project_to_remove['_id']
                    if hasattr(db, 'projects'):
                        await db.projects.delete_one({"_id": project_id})
                    else:
                        projects_collection = getattr(db, "projects")
                        await projects_collection.delete_one({"_id": project_id})
                    duplicates_removed += 1
                    logger.warning(f"🗑️  Removed duplicate project: {name} (ID: {project_id})")
        
        if duplicates_removed > 0:
            logger.warning(f"🧹 Cleaned up {duplicates_removed} duplicate project(s) for user {user_id}")
            print(f"🧹 Cleaned up {duplicates_removed} duplicate project(s) for user {user_id}", flush=True)
        
    except Exception as e:
        logger.error(f"Error cleaning up duplicate projects: {e}", exc_info=True)


async def check_and_seed_demo(db, mongo_uri: str, db_name: str, demo_email: Optional[str] = None) -> bool:
    """
    Main entry point for demo seeding.
    
    Checks if demo seeding should occur and performs it if conditions are met.
    
    Args:
        db: ExperimentDB instance (scoped to StoryWeaver)
        mongo_uri: MongoDB connection URI
        db_name: Database name
        demo_email: Email of the demo user (default: None, will use config.DEMO_EMAIL_DEFAULT if enabled)
    
    Returns:
        bool: True if seeding occurred (or wasn't needed), False if an error occurred
    """
    # Use provided email or fallback to config
    if demo_email is None:
        if not DEMO_ENABLED or DEMO_EMAIL_DEFAULT is None:
            logger.debug("Demo seeding disabled: ENABLE_DEMO not configured.")
            return True  # Not an error, just disabled
        demo_email = DEMO_EMAIL_DEFAULT
    
    try:
        print(f"🌱 check_and_seed_demo: Checking if seeding needed for '{demo_email}'...", flush=True)
        logger.info(f"check_and_seed_demo: Checking if seeding needed for '{demo_email}'...")
        should_seed, demo_user_id = await should_seed_demo(db, mongo_uri, db_name, demo_email)
        
        print(f"🌱 check_and_seed_demo: should_seed={should_seed}, demo_user_id={demo_user_id}", flush=True)
        logger.info(f"check_and_seed_demo: should_seed={should_seed}, demo_user_id={demo_user_id}")
        
        if not should_seed:
            # Even if we don't need to seed, check for duplicates and clean them up
            logger.info(f"Demo seeding skipped (conditions not met or already seeded) for '{demo_email}'")
            # Clean up any duplicates that might exist
            await _cleanup_duplicate_projects(db, demo_user_id)
            return True  # Not an error, just didn't need to seed
        
        if not demo_user_id:
            logger.warning(f"Demo user ID not found for '{demo_email}', cannot seed")
            return False
        
        logger.info(f"check_and_seed_demo: Proceeding with seeding for user_id={demo_user_id}...")
        success = await seed_demo_content(db, demo_user_id)
        
        # Clean up any duplicates that might have been created
        await _cleanup_duplicate_projects(db, demo_user_id)
        
        logger.info(f"check_and_seed_demo: Seeding completed with success={success}")
        return success
        
    except Exception as e:
        logger.error(f"Error in check_and_seed_demo: {e}", exc_info=True)
        return False
