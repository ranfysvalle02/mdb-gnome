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
    1. Demo user exists (with matching demo_email)
    2. Demo user has no projects in StoryWeaver
    
    Note: This function assumes that if a user with demo_email exists, they have the demo role.
    In a production system with stricter requirements, you could check the role via the
    authz provider, but that would require passing the authz provider instance.
    
    Args:
        db: ExperimentDB instance (scoped to StoryWeaver)
        mongo_uri: MongoDB connection URI (for accessing top-level database)
        db_name: Database name (for accessing top-level database)
        demo_email: Email of the demo user (default: None, will use config.DEMO_EMAIL_DEFAULT if enabled)
    
    Returns:
        tuple: (should_seed: bool, demo_user_id: Optional[str])
               If should_seed is True, demo_user_id will be the ObjectId as a string
    """
    # Use provided email or fallback to config
    if demo_email is None:
        if not DEMO_ENABLED or DEMO_EMAIL_DEFAULT is None:
            logger.debug("Demo seeding disabled: ENABLE_DEMO not configured.")
            return False, None
        demo_email = DEMO_EMAIL_DEFAULT
    
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
        
        # Check if demo user has any projects in StoryWeaver
        # If they have projects, they've already used StoryWeaver, so don't seed
        project_count = await db.projects.count_documents({"user_id": ObjectId(demo_user_id)})
        
        client.close()
        
        if project_count > 0:
            logger.debug(f"Demo user '{demo_email}' already has {project_count} project(s) in StoryWeaver. Skipping demo seed.")
            return False, demo_user_id
        
        logger.info(f"Demo user '{demo_email}' exists and has no projects. Proceeding with demo seed.")
        return True, demo_user_id
        
    except Exception as e:
        logger.error(f"Error checking if demo seed should occur: {e}", exc_info=True)
        return False, None


async def seed_demo_content(db, demo_user_id: str) -> bool:
    """
    Seed demo content for StoryWeaver.
    
    Creates:
    1. Educational project with notes and quizzes on:
       - Math/Algebra: Slope, Y Intercept, X Intercept, Line Equations
       - Spanish Class: Pronouns, Adverbs - 8th grade
       - Basketball IQ
    2. Story project with notes
    
    Args:
        db: ExperimentDB instance (scoped to StoryWeaver)
        demo_user_id: User ID of the demo user (as string)
    
    Returns:
        bool: True if seeding was successful, False otherwise
    """
    try:
        demo_user_obj_id = ObjectId(demo_user_id)
        now = datetime.datetime.utcnow()
        
        # 1. Create Educational Project
        educational_project = {
            "name": "Educational Study Notes",
            "project_goal": "Master key concepts in Math, Spanish, and Basketball strategy",
            "project_type": "educational",
            "created_at": now,
            "user_id": demo_user_obj_id
        }
        
        edu_project_result = await db.projects.insert_one(educational_project)
        edu_project_id = edu_project_result.inserted_id
        
        logger.info(f"Created educational project '{educational_project['name']}' with ID: {edu_project_id}")
        
        # Generate notes for each topic
        topics = [
            {
                "topic": "Math/Algebra: Slope, Y Intercept, X Intercept, Line Equations",
                "notes": [
                    "**What is Slope?** Slope is how steep a line is. Think of it like a hill - a steep hill has a big slope, a flat road has a slope of zero. The key phrase to remember is 'Rise over Run' - how much the line goes UP divided by how much it goes OVER to the right.",
                    "**Finding Slope from a Graph** Step 1: Pick two points on the line (where the line crosses grid lines makes it easiest). Step 2: Count how many spaces UP or DOWN you go from the first point to the second (this is your Rise). Step 3: Count how many spaces RIGHT or LEFT you go (this is your Run). Step 4: Divide Rise by Run. Going UP is positive, going DOWN is negative. Going RIGHT is positive.",
                    "**Slope Formula** If you have two points, say point A at (2, 3) and point B at (6, 11), find the slope like this: First, subtract the y-values: 11 minus 3 = 8 (that's your rise). Then, subtract the x-values: 6 minus 2 = 4 (that's your run). Divide: 8 divided by 4 = 2. So the slope is 2, which means the line goes up 2 spaces for every 1 space to the right.",
                    "**Four Types of Slope** Positive slope means the line goes uphill from left to right - like climbing a mountain. Negative slope means the line goes downhill from left to right - like skiing down a slope. Zero slope means the line is perfectly flat - like a horizon. Undefined slope means the line is perfectly vertical - like a wall (you can't divide by zero, so the slope is undefined).",
                    "**Y-Intercept Explained** The y-intercept is where the line crosses the vertical line (the y-axis). At this exact point, x is always zero. To find it on a graph: just look where your line touches the left or right edge of the graph. To find it in an equation: replace x with 0 and solve. Example: If a line crosses the y-axis at 4, the point is (0, 4) - meaning when x is zero, y is 4.",
                    "**X-Intercept Explained** The x-intercept is where the line crosses the horizontal line (the x-axis). At this exact point, y is always zero. To find it on a graph: look where your line touches the top or bottom edge of the graph. To find it in an equation: replace y with 0 and solve. Example: If a line crosses the x-axis at -2, the point is (-2, 0) - meaning when y is zero, x is -2.",
                    "**Slope-Intercept Form** This is the easiest form: y = mx + b. The letter m is the slope, and the letter b is the y-intercept. It's great because you can see both important numbers right away! Example: y = 2x - 3 means the slope is 2 (go up 2, over 1) and the y-intercept is -3 (the line starts at the point (0, -3)).",
                    "**Point-Slope Form** Use this when you know one point and the slope. The formula looks like: y minus y1 equals m times (x minus x1). The (x1, y1) is the point you know, and m is the slope. Example: If you know the slope is 5 and the line passes through point (2, 8), you write: y - 8 = 5(x - 2). This form is super helpful when you're given a point and a slope!",
                    "**Standard Form** This form looks like: Ax + By = C, where A, B, and C are just regular numbers. It's not as friendly as slope-intercept form, but it's great for finding intercepts. To find the y-intercept: pretend the x term doesn't exist (cover it up or set it to 0) and solve for y. To find the x-intercept: pretend the y term doesn't exist (cover it up or set it to 0) and solve for x.",
                    "**Cover-Up Method Example** For the equation 3x + 2y = 12: To find y-intercept, ignore 3x (pretend it's zero), so you get 2y = 12. Divide both sides by 2 to get y = 6. So the y-intercept is (0, 6). To find x-intercept, ignore 2y (pretend it's zero), so you get 3x = 12. Divide both sides by 3 to get x = 4. So the x-intercept is (4, 0)."
                ],
                "tags": ["math", "algebra", "linear-equations", "slope", "intercepts"]
            },
            {
                "topic": "Spanish Class: Pronouns, Adverbs - 8th grade",
                "notes": [
                    "**What are Pronouns?** Pronouns are words that replace nouns so you don't have to repeat them. In English, instead of saying 'Maria is tall. Maria is smart,' you say 'Maria is tall. She is smart.' The word 'she' replaces 'Maria'. Spanish works the same way!",
                    "**Subject Pronouns in Spanish** These tell you WHO is doing the action. Think of them like the actors in a sentence. Yo means I. Tú means you (but only with friends and family - it's informal). Usted also means you, but it's formal - use it with teachers and adults. Él means he. Ella means she. Nosotros means we (masculine or mixed group). Nosotras means we (all girls). Ellos means they (masculine or mixed). Ellas means they (all girls).",
                    "**The Cool Thing About Spanish** In Spanish, you can usually drop the pronoun! In English you MUST say 'I am going,' but in Spanish you can just say 'Voy' (I am going) because the verb ending already tells you it's 'I'. The verb 'voy' means 'I go' - you don't need to add 'Yo' unless you want to emphasize it.",
                    "**What are Adverbs?** Adverbs are words that describe HOW, WHEN, WHERE, or HOW MUCH something happens. In English: quickly (HOW), today (WHEN), here (WHERE), very (HOW MUCH). Spanish has the same idea!",
                    "**Making Adverbs from Adjectives** Most Spanish adverbs are easy to make. Take an adjective like rápido (fast), make it feminine (rápida), then add -mente. So rápido becomes rápidamente (quickly). It's like adding -ly in English! Another example: lento (slow) becomes lentamente (slowly). Perfecto (perfect) becomes perfectamente (perfectly).",
                    "**Time Adverbs You Need to Know** hoy = today. ayer = yesterday. mañana = tomorrow. siempre = always. nunca = never. a veces = sometimes. ahora = now. Memorize these - you'll use them constantly!",
                    "**Place Adverbs** aquí = here. allí or allá = there (they're pretty much the same). cerca = near. lejos = far. These help you describe where things are or where actions happen.",
                    "**Quantity Adverbs** mucho = a lot. poco = a little. muy = very (use it like 'muy rápido' means 'very fast'). más = more. menos = less. These help you say HOW MUCH of something there is.",
                    "**Manner Adverbs** bien = well. mal = badly. así = like this / so. These describe HOW something is done. Example: 'I speak Spanish well' = 'Hablo español bien'. 'I sing badly' = 'Canto mal'.",
                    "**When to Use Tú vs Usted** This is about respect and formality. Use TÚ with: friends your age, family members, people you know well. Use USTED with: teachers, principals, adults you don't know well, anyone you want to show respect to. In Florida and Latin America, use USTEDES (not vosotros) when saying 'you all' to a group."
                ],
                "tags": ["spanish", "grammar", "pronouns", "adverbs", "8th-grade"]
            },
            {
                "topic": "Basketball IQ",
                "notes": [
                    "**What is Basketball IQ?** Basketball IQ isn't about being the tallest or fastest player. It's about understanding the game and making smart decisions. A high-IQ player reads the court, sees plays developing, and helps their whole team play better. It's the difference between someone who plays hard and someone who plays smart.",
                    "**The Spacing Rule** This is the most important rule: DON'T follow the ball around. DON'T stand right next to your teammate. DO spread out! Imagine each player has their own spot on the court, like the spots on a 3-point shooting rack. When players spread out, it creates open paths to the basket (called 'driving lanes') and makes it much harder for the defense to guard everyone.",
                    "**What to Do Without the Ball** Don't just stand there watching the person dribbling! Be active. Move! Here are three moves to master: (1) CUT - When your defender looks away, run hard straight to the basket, ready to catch a pass. (2) SET A SCREEN - Run over and stand legally in front of the person guarding your teammate to help them get open. (3) V-CUT - Walk your defender toward the basket, then quickly change direction and cut back out to get open for a pass.",
                    "**Good Shot vs Bad Shot** Bad basketball IQ: Taking a rushed, contested shot with lots of time left on the shot clock. Good basketball IQ: Taking an open shot that you've practiced a hundred times, when you're in rhythm, and it fits the offense. Ask yourself: Is this the best shot we can get right now?",
                    "**The Extra Pass Philosophy** Don't take a decent shot if your teammate has a great shot. Passing up your own good shot to give someone else a great shot shows high basketball IQ. It builds team chemistry and trust. Teams that share the ball win more games.",
                    "**Defensive Positioning** Remember 'Ball-You-Man' - always know where the BALL is, where YOU are, and where your MAN (the person you're guarding) is. Always stay between your player and the basket. If you're guarding the ball, pressure them. If you're one pass away, get ready to help but also stay ready to guard your player. If you're far from the ball, position yourself to help stop anyone driving to the basket.",
                    "**Help Defense Basics** If your teammate gets beat by their player, don't just watch! Your teammate needs help. Quickly slide over (called 'stunting') to stop the ball-handler from getting an easy basket. Your first job is to STOP THE BALL. Then worry about your own player. Help defense wins games.",
                    "**Communication is Everything** A quiet defense is a weak defense. Talk constantly! Yell 'BALL! BALL! BALL!' when you're guarding the ball-handler. Shout 'SCREEN LEFT!' or 'SCREEN RIGHT!' to warn your teammate. Call out 'I GOT YOUR HELP!' to let teammates know you're ready. Yell 'SHOT!' when someone shoots so everyone boxes out and rebounds.",
                    "**Boxing Out Technique** After a shot goes up, don't just turn and watch the ball! Find the person you're guarding, step in front of them, make contact with your back or hip, and seal them away from the basket. Do this BEFORE you try to get the rebound. Good boxing out beats jumping ability every time.",
                    "**Game Awareness** Always know what's happening in the game. Check the score and time - if you're winning by 10 with one minute left, don't take a bad shot. Keep track of fouls - know if your team is in the 'bonus' (meaning the other team shoots free throws on every foul). Know your matchup - who are you guarding? Are they fast? Can they shoot? Force fast players to use their weak hand. Don't leave good shooters open!"
                ],
                "tags": ["basketball", "strategy", "sports-iq", "offense", "defense"]
            }
        ]
        
        # Insert notes for educational project
        for topic_data in topics:
            for note_content in topic_data["notes"]:
                note_doc = {
                    "project_id": edu_project_id,
                    "user_id": demo_user_obj_id,
                    "content": note_content,
                    "timestamp": now,
                    "contributor_label": "Demo Content",
                    "tags": topic_data["tags"]
                }
                await db.notes.insert_one(note_doc)
        
        logger.info(f"Created {sum(len(t['notes']) for t in topics)} notes for educational project")
        
        # Create a quiz for the educational project (combining all topics)
        quiz_doc = {
            "user_id": demo_user_obj_id,
            "project_id": edu_project_id,
            "title": "Study Check: Math, Spanish & Basketball",
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
                },
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
                },
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
                    "question": "What should you do when setting a screen (pick) in basketball?",
                    "options": [
                        "Always roll to the basket",
                        "Read whether the defender goes over or under - if over, roll; if under, pop out",
                        "Stand still and wait for the ball",
                        "Run away from the screen"
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
            "share_token": None  # Will be generated if needed
        }
        
        await db.quizzes.insert_one(quiz_doc)
        logger.info("Created quiz for educational project")
        
        # 2. Create Story Project with notes
        story_project = {
            "name": "My Story Collection",
            "project_goal": "Capture ideas and weave them into compelling narratives",
            "project_type": "story",
            "created_at": now,
            "user_id": demo_user_obj_id
        }
        
        story_project_result = await db.projects.insert_one(story_project)
        story_project_id = story_project_result.inserted_id
        
        logger.info(f"Created story project '{story_project['name']}' with ID: {story_project_id}")
        
        # Add some demo story notes
        story_notes = [
            "The old lighthouse keeper watched the storm approach from the east. Years of experience told him this would be different - the barometer had dropped further than he'd ever seen.",
            "In the library's quiet corner, Maya discovered a book bound in leather so old it crumbled at her touch. Inside, handwritten entries spoke of a city that shouldn't exist.",
            "Three friends, one promise: they would always meet here at sunset, no matter what. But tonight, only two showed up, and they knew their world had changed forever.",
            "The letter arrived on a Tuesday, marked with a stamp from a country that had been gone for thirty years. Inside, a single sentence: 'The time has come.'",
            "Every morning, she walked the same path through the garden. Today, she noticed something different - a door where yesterday there had only been a wall."
        ]
        
        for i, note_content in enumerate(story_notes):
            note_doc = {
                "project_id": story_project_id,
                "user_id": demo_user_obj_id,
                "content": note_content,
                "timestamp": now + datetime.timedelta(minutes=i * 5),  # Space them out slightly
                "contributor_label": "Demo Content",
                "tags": ["story", "narrative", "fiction"]
            }
            await db.notes.insert_one(note_doc)
        
        logger.info(f"Created {len(story_notes)} notes for story project")
        
        logger.info("✅ Successfully seeded demo content for StoryWeaver!")
        return True
        
    except Exception as e:
        logger.error(f"Error seeding demo content: {e}", exc_info=True)
        return False


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

