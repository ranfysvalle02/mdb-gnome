"""
Story Weaver Actor
A Ray Actor that handles all Story Weaver operations.
Adapted from the original Flask Story Weaver application.
"""

import os
import logging
import datetime
import json
import uuid
import pathlib
import time
from typing import Dict, Any, List, Optional
import ray
from bson.objectid import ObjectId
from bson.errors import InvalidId
from pymongo.errors import OperationFailure
# Removed werkzeug imports - authentication is handled at top level

logger = logging.getLogger(__name__)

# Actor-local paths
experiment_dir = pathlib.Path(__file__).parent
templates_dir = experiment_dir / "templates"

# Constants
NOTES_PER_PAGE = 10
STORY_TONES = ["Nostalgic & Warm", "Comedic Monologue", "Hardboiled Detective", "Documentary Narrator", "Epic Saga", "Formal & Academic"]
STORY_FORMATS = ["Short Story (3-5 paragraphs)", "Executive Summary (1 paragraph)", "Key Plot Points (Bulleted List)"]
SONG_GENRES = ["Pop", "Folk", "Indie Rock", "Hip-Hop", "Electronic", "Country", "R&B"]
SONG_MOODS = ["Upbeat & Hopeful", "Melancholic & Reflective", "Energetic & Anthemic", "Intimate & Acoustic", "Dark & Brooding"]

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
MAILGUN_API_KEY = os.environ.get('MAILGUN_API_KEY')
MAILGUN_DOMAIN = os.environ.get('MAILGUN_DOMAIN')
NOTIFICATION_EMAIL_TO_OVERRIDE = os.environ.get('NOTIFICATION_EMAIL_TO')
NOTIFICATION_EMAIL_FROM = os.environ.get('NOTIFICATION_EMAIL_FROM', 'app-alerts@your_mailgun_domain.com')
SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'a-very-secret-key-for-dev')

IS_ATLAS = os.environ.get('MONGO_URI', '').startswith('mongodb+srv')


@ray.remote
class ExperimentActor:
    """
    Story Weaver Ray Actor.
    Handles all Story Weaver operations using the experiment database abstraction.
    """

    def __init__(self, mongo_uri: str, db_name: str, write_scope: str, read_scopes: List[str]):
        self.mongo_uri = mongo_uri
        self.db_name = db_name
        self.write_scope = write_scope
        self.read_scopes = read_scopes
        self.is_atlas = "mongodb+srv" in mongo_uri if mongo_uri else False
        
        # Load templates
        try:
            from fastapi.templating import Jinja2Templates
            
            if templates_dir.is_dir():
                self.templates = Jinja2Templates(directory=str(templates_dir))
            else:
                self.templates = None
                logger.warning(f"[{write_scope}-Actor] Template dir not found at {templates_dir}")
            
            logger.info(f"[{write_scope}-Actor] Successfully loaded templates.")
        except ImportError as e:
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to load templates: {e}", exc_info=True)
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
            logger.critical(f"[{write_scope}-Actor] ❌ CRITICAL: Failed to init DB: {e}", exc_info=True)
            self.db = None

    def _check_ready(self):
        """Check if actor is ready."""
        if not self.db:
            raise RuntimeError("Database not initialized. Check logs for import errors.")
        if not self.templates:
            raise RuntimeError("Templates not loaded. Check logs for import errors.")

    # --- AI Helper Functions ---
    def _get_embedding(self, text: str, model: str = "text-embedding-3-small"):
        """Generates a vector embedding for a given text using OpenAI."""
        if not OPENAI_API_KEY:
            logger.warning("OpenAI API key not set, cannot generate embeddings.")
            return None
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            text = text.replace("\n", " ").strip()
            if not text:
                return None
            return openai.embeddings.create(input=[text], model=model).data[0].embedding
        except Exception as e:
            logger.error(f"Error calling OpenAI for embedding: {e}", exc_info=True)
            return None

    def _get_ai_follow_ups(self, project_goal: str, original_prompt: str, entry_content: str):
        if not OPENAI_API_KEY:
            return []
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            system_prompt = f"You are a helpful assistant for a writing project. The project's goal is: '{project_goal}'. Generate 3 insightful, open-ended follow-up questions to encourage deeper exploration of the topic. Based on the user's response to a prompt, generate exactly 3 distinct questions. Return as a JSON array of strings."
            user_prompt = f"Original Prompt: \"{original_prompt}\"\n\nUser's Latest Response:\n\"{entry_content}\"\n\nGenerate 3 follow-up questions."
            completion = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"}
            )
            questions_data = json.loads(completion.choices[0].message.content)
            return questions_data.get('questions', []) if isinstance(questions_data, dict) else questions_data if isinstance(questions_data, list) else []
        except Exception as e:
            logger.error(f"Error calling OpenAI for follow-ups: {e}", exc_info=True)
            return []

    async def _get_ai_suggested_tags(self, project_id: str, user_id: str, entry_content: str):
        if not OPENAI_API_KEY:
            return []
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            
            # Get example entries
            example_entries = []
            try:
                cursor = self.db.notes.find({
                    'project_id': ObjectId(project_id),
                    'user_id': ObjectId(user_id),
                    'tags': {'$exists': True, '$ne': []}
                }).sort("timestamp", -1).limit(15)
                example_entries = await cursor.to_list(length=15)
            except Exception:
                pass
            
            example_prompt_part = ""
            if example_entries:
                example_prompt_part = "Here are examples of how I've tagged previous notes in this project:\n\n"
                for entry in example_entries:
                    content_snippet = (entry['content'][:150] + '...') if len(entry.get('content', '')) > 150 else entry.get('content', '')
                    example_prompt_part += f"- Note: \"{content_snippet.strip()}\"\n  Tags: {', '.join(entry.get('tags', []))}\n"
            
            system_prompt = "You are an AI assistant that helps tag notes for a writing project. Suggest 3-5 relevant, concise, single-word or two-word tags. Analyze the new note and the user's past tagging style. Return as a JSON object: {\"tags\": [\"tag1\", \"tag2\"]}."
            user_prompt = f"{example_prompt_part}Now, suggest tags for this new note:\n\n\"{entry_content}\""
            
            completion = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"}
            )
            tags_data = json.loads(completion.choices[0].message.content)
            return tags_data.get('tags', [])
        except Exception as e:
            logger.error(f"Error calling OpenAI for tag suggestions: {e}", exc_info=True)
            return []

    def _search_web(self, query: str, max_results: int = 5) -> str:
        """Search the web using DuckDuckGo and return a formatted summary of results."""
        try:
            from duckduckgo_search import DDGS
            
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
                
            if not results:
                return ""
            
            # Format search results as context for AI
            search_context = f"Web search results for '{query}':\n\n"
            for i, result in enumerate(results[:max_results], 1):
                title = result.get('title', 'No title')
                body = result.get('body', 'No description')
                url = result.get('href', 'No URL')
                search_context += f"{i}. {title}\n   {body[:200]}...\n   Source: {url}\n\n"
            
            return search_context
        except ImportError:
            logger.warning("duckduckgo-search package not installed. Web search disabled.")
            return ""
        except Exception as e:
            logger.error(f"Error performing web search: {e}", exc_info=True)
            return ""

    def _get_ai_study_notes(self, topic: str, project_goal: str, num_notes: int = 5, use_web_search: bool = False):
        if not OPENAI_API_KEY:
            return []
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            
            # Perform web search if enabled
            web_context = ""
            if use_web_search:
                logger.info(f"Performing web search for topic: {topic}")
                web_context = self._search_web(topic, max_results=5)
            
            system_prompt = f"You are an expert educator creating study materials for a project with the goal: '{project_goal}'. Generate {num_notes} concise, well-structured study notes. Each note should be a standalone piece of information. For key terms, use markdown bolding (e.g., **Term**). Return a JSON object: {{\"notes\": [\"Note 1 content...\", \"Note 2 content...\"]}}."
            
            if web_context:
                user_prompt = f"Generate {num_notes} study notes for the topic: '{topic}'.\n\n{web_context}\n\nUse the web search results above to enhance the accuracy and depth of the study notes. Ensure the notes are factually accurate based on the search results."
            else:
                user_prompt = f"Generate {num_notes} study notes for the topic: '{topic}'."
            
            completion = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"}
            )
            notes_data = json.loads(completion.choices[0].message.content)
            return notes_data.get('notes', [])
        except Exception as e:
            logger.error(f"Error calling OpenAI for study notes: {e}", exc_info=True)
            return []

    def _get_ai_quiz(self, notes_content: str, num_questions: int, question_type: str, difficulty: str, knowledge_source: str):
        if not OPENAI_API_KEY:
            return []
        if knowledge_source == 'notes_only':
            source_instruction = "Base your questions STRICTLY on the provided notes. Do not introduce any information not present in the text. If the notes are insufficient, state that you cannot create the quiz."
        else:
            source_instruction = "Use the provided notes as the primary source material, but supplement with your general knowledge to create a comprehensive quiz on the topic."
        if question_type == 'True/False':
            json_format = '{"quiz": [{"question": "...", "answer": true/false}]}'
        else:
            json_format = '{"quiz": [{"question": "...", "options": ["A", "B", "C", "D"], "correct_answer_index": N}]}'
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            system_prompt = f"""You are a quiz generation AI. Your task is to create a {num_questions}-question, {difficulty}-difficulty, {question_type} quiz.
{source_instruction}
Return the quiz as a JSON object with the exact format: {json_format}"""
            user_prompt = f"Notes:\n{notes_content}\n\nGenerate the quiz."
            completion = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"}
            )
            quiz_data = json.loads(completion.choices[0].message.content)
            return quiz_data.get('quiz', [])
        except Exception as e:
            logger.error(f"Error calling OpenAI for quiz generation: {e}", exc_info=True)
            return []

    def _get_ai_study_guide(self, notes_content: str, project_name: str):
        if not OPENAI_API_KEY:
            return ""
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            system_prompt = f"""You are an expert academic assistant. Your task is to synthesize the provided notes into a structured and comprehensive study guide for the project '{project_name}'.
The study guide should be well-organized using Markdown formatting. It must include:
1. **Summary:** A concise overview of the main topics.
2. **Key Concepts & Definitions:** A list of important terms with clear explanations.
3. **Core Themes:** An analysis of the overarching ideas connecting the notes.
4. **Potential Discussion Questions:** A few open-ended questions to stimulate critical thinking.
Return only the Markdown content for the study guide."""
            user_prompt = f"Notes:\n{notes_content}\n\nGenerate the study guide."
            completion = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            return completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Error calling OpenAI for study guide generation: {e}", exc_info=True)
            return "Error: Could not generate the study guide."

    def _get_ai_song(self, notes_content: str, project_name: str, genre: str, mood: str):
        if not OPENAI_API_KEY:
            return ""
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            system_prompt = """You are an expert songwriter. Your task is to transform a collection of notes into compelling song lyrics.
Use the provided notes as the core inspiration for themes, imagery, and narrative.
The lyrics should be well-structured. Use standard labels like [Verse 1], [Chorus], [Verse 2], [Bridge], and [Outro].
Capture the specified genre and mood in the lyrical style, tone, and content.
Return only the formatted song lyrics."""
            user_prompt = f"""Project Name: "{project_name}"
Genre: {genre}
Mood: {mood}
Based on these notes, write a complete song:
---
{notes_content}
---"""
            completion = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            return completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Error calling OpenAI for song generation: {e}", exc_info=True)
            return "Error: Could not generate the song lyrics."

    def _get_ai_story(self, formatted_notes: str, project_name: str, tone: str, story_format: str):
        if not OPENAI_API_KEY:
            return ""
        try:
            import openai
            openai.api_key = OPENAI_API_KEY
            system_prompt = "You are a master writer. Weave a collection of notes into a coherent, compelling narrative. If notes are from multiple contributors, synthesize them into a single voice or a structured dialogue, as appropriate."
            user_prompt = f"Synthesize these notes from the \"{project_name}\" project into a narrative with a \"{tone}\" tone, formatted as a \"{story_format}\". Connect the ideas, infer themes, and create a fluid arc.\n\nNotes:\n{formatted_notes}"
            completion = openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            return completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Error during story generation: {e}", exc_info=True)
            return "Error: Could not generate the story."

    # --- Email Helper ---
    def _send_notification_email(self, contributor_label: str, project_name: str, content_snippet: str, token: str, project_owner_email: str, base_url: str = "", is_shared: bool = False):
        if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
            logger.info("Notification skipped: Mailgun API key or domain is missing.")
            return
        try:
            import requests
            recipient_email = NOTIFICATION_EMAIL_TO_OVERRIDE or project_owner_email
            invite_url = f"{base_url}/experiments/storyweaver/invite/{token}" if not is_shared else f"{base_url}/experiments/storyweaver/share/{token}"
            email_subject = f"🔔 New Note in '{project_name}' from {contributor_label}"
            email_body_html = f"""<html><body>
<h2>A new note has been submitted to your Story Weaver project!</h2>
<p><strong>Project:</strong> {project_name}</p>
<p><strong>Contributor:</strong> {contributor_label}</p>
<p><strong>Content Snippet:</strong></p>
<div style="border: 1px solid #ccc; padding: 10px; margin: 10px 0; background-color: #f9f9f9; border-left: 4px solid #007bff;">
<em>"{content_snippet[:200]}..."</em>
</div>
<p>
<a href="{invite_url}" style="display: inline-block; padding: 10px 20px; background-color: #4CAF50; color: white; text-decoration: none; border-radius: 5px; font-weight: bold;">Continue the Conversation</a>
</p>
</body></html>"""
            response = requests.post(
                f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={
                    "from": f"Story Weaver Alert <{NOTIFICATION_EMAIL_FROM}>",
                    "to": recipient_email,
                    "subject": email_subject,
                    "html": email_body_html
                }
            )
            response.raise_for_status()
            logger.info(f"✅ Notification email sent. Status: {response.status_code}")
        except Exception as e:
            logger.error(f"❌ Mailgun Error: {e}", exc_info=True)

    # --- Template Rendering Methods ---
    # IMPORTANT: Authentication is handled by the top-level auth system
    # - Users MUST be created by admins at the top level (NO registration in Story Weaver)
    # - The user_id comes from the JWT token in get_current_user dependency
    # - Projects are scoped to the experiment but use top-level user IDs
    # - All user_id values reference the top-level users collection
    async def render_index(self, user_id: str):
        """Render the workspace index page."""
        self._check_ready()
        try:
            projects = await self.db.projects.find({"user_id": ObjectId(user_id)}).sort("created_at", -1).to_list(length=None)
            for p in projects:
                p['_id'] = str(p['_id'])
            
            return self.templates.TemplateResponse("index.html", {
                "request": type('Request', (), {'url': type('URL', (), {'path': '/'})()}),
                "projects": projects
            }).body.decode('utf-8')
        except Exception as e:
            logger.error(f"Error rendering index: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def render_project_view(self, user_id: str, project_id: str):
        """Render the project view page."""
        self._check_ready()
        try:
            project = await self.db.projects.find_one({"_id": ObjectId(project_id), "user_id": ObjectId(user_id)})
            if not project:
                return "<h1>Project not found</h1>"
            
            project['_id'] = str(project['_id'])
            project['project_type'] = project.get('project_type', 'story')
            
            quizzes = await self.db.quizzes.find({"project_id": ObjectId(project_id)}).sort("created_at", -1).to_list(length=None)
            for quiz in quizzes:
                quiz['_id'] = str(quiz['_id'])
            
            invited_users = await self.db.invited_users.find({"project_id": ObjectId(project_id)}).sort("created_at", -1).to_list(length=None)
            for invite in invited_users:
                invite['_id'] = str(invite['_id'])
            
            shared_invites = await self.db.shared_invites.find({"project_id": ObjectId(project_id)}).sort("created_at", -1).to_list(length=None)
            for invite in shared_invites:
                invite['_id'] = str(invite['_id'])
            
            return self.templates.TemplateResponse("project.html", {
                "request": type('Request', (), {})(),
                "project": project,
                "story_tones": STORY_TONES,
                "story_formats": STORY_FORMATS,
                "song_genres": SONG_GENRES,
                "song_moods": SONG_MOODS,
                "quizzes": quizzes,
                "is_atlas": self.is_atlas,
                "invited_users": invited_users,
                "shared_invites": shared_invites
            }).body.decode('utf-8')
        except Exception as e:
            logger.error(f"Error rendering project view: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def render_invite_page(self, token: str):
        """Render the invite page."""
        self._check_ready()
        try:
            invited_user = await self.db.invited_users.find_one({"token": token})
            if not invited_user:
                return "<h1>Invalid invitation token</h1>"
            
            project = await self.db.projects.find_one({"_id": invited_user['project_id']})
            if not project:
                return "<h1>Associated project not found</h1>"
            
            project['_id'] = str(project['_id'])
            
            return self.templates.TemplateResponse("invite.html", {
                "request": type('Request', (), {'url': type('URL', (), {'path': f'/invite/{token}'})()}),
                "project": project,
                "contributor_label": invited_user['label'],
                "invite_token": token,
                "invite_prompt": invited_user['prompt'],
                "follow_up_questions": invited_user.get('last_suggested_questions', [])
            }).body.decode('utf-8')
        except Exception as e:
            logger.error(f"Error rendering invite page: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def render_shared_invite_page(self, token: str):
        """Render the shared invite page."""
        self._check_ready()
        try:
            shared_invite = await self.db.shared_invites.find_one({"token": token})
            if not shared_invite:
                return "<h1>Invalid or expired shared invite link</h1>"
            
            project = await self.db.projects.find_one({"_id": shared_invite['project_id']})
            if not project:
                return "<h1>Associated project not found</h1>"
            
            project['_id'] = str(project['_id'])
            
            return self.templates.TemplateResponse("share.html", {
                "request": type('Request', (), {'url': type('URL', (), {'path': f'/share/{token}'})()}),
                "project": project,
                "invite_prompt": shared_invite['prompt'],
                "shared_token": token
            }).body.decode('utf-8')
        except Exception as e:
            logger.error(f"Error rendering shared invite page: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    async def render_shared_quiz_page(self, share_token: str):
        """Render the shared quiz page."""
        self._check_ready()
        try:
            quiz = await self.db.quizzes.find_one({"share_token": share_token})
            if not quiz:
                return "<h1>Invalid or expired quiz link</h1>"
            
            project = await self.db.projects.find_one({"_id": quiz['project_id']})
            if not project:
                return "<h1>Associated project not found</h1>"
            
            quiz['_id'] = str(quiz['_id'])
            project['_id'] = str(project['_id'])
            
            return self.templates.TemplateResponse("shared_quiz.html", {
                "request": type('Request', (), {'url': type('URL', (), {'path': f'/quiz/{share_token}'})()}),
                "quiz": quiz.get('quiz_data', []),
                "quiz_title": quiz.get('title', 'Quiz'),
                "project_name": project.get('name', 'Project'),
                "question_type": quiz.get('question_type', 'Multiple Choice')
            }).body.decode('utf-8')
        except Exception as e:
            logger.error(f"Error rendering shared quiz page: {e}", exc_info=True)
            return f"<h1>Error</h1><pre>{e}</pre>"

    # --- API Methods ---
    async def create_project(self, user_id: str, name: str, project_goal: str, project_type: str = "story"):
        """Create a new project."""
        self._check_ready()
        try:
            created_at = datetime.datetime.utcnow()
            project_doc = {
                'name': name,
                'project_goal': project_goal,
                'project_type': project_type,
                'created_at': created_at,
                'user_id': ObjectId(user_id)
            }
            result = await self.db.projects.insert_one(project_doc)
            return {
                "status": "success",
                "project": {
                    "_id": str(result.inserted_id),
                    "name": name,
                    "project_goal": project_goal,
                    "project_type": project_type,
                    "created_at": created_at.isoformat()
                }
            }
        except Exception as e:
            logger.error(f"Error creating project: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def delete_project(self, user_id: str, project_id: str):
        """Delete a project and all associated data."""
        self._check_ready()
        try:
            # Verify project exists and user owns it
            project = await self.db.projects.find_one({"_id": ObjectId(project_id), "user_id": ObjectId(user_id)})
            if not project:
                return {"status": "error", "message": "Project not found or unauthorized."}
            
            project_obj_id = ObjectId(project_id)
            
            # Delete all associated data
            # Delete notes
            notes_result = await self.db.notes.delete_many({"project_id": project_obj_id})
            logger.info(f"Deleted {notes_result.deleted_count} notes for project {project_id}")
            
            # Delete invited users
            invites_result = await self.db.invited_users.delete_many({"project_id": project_obj_id})
            logger.info(f"Deleted {invites_result.deleted_count} invites for project {project_id}")
            
            # Delete shared invites
            shared_invites_result = await self.db.shared_invites.delete_many({"project_id": project_obj_id})
            logger.info(f"Deleted {shared_invites_result.deleted_count} shared invites for project {project_id}")
            
            # Delete quizzes
            quizzes_result = await self.db.quizzes.delete_many({"project_id": project_obj_id})
            logger.info(f"Deleted {quizzes_result.deleted_count} quizzes for project {project_id}")
            
            # Delete the project itself
            project_result = await self.db.projects.delete_one({"_id": project_obj_id})
            
            if project_result.deleted_count == 1:
                return {"status": "success", "message": "Project deleted successfully."}
            return {"status": "error", "message": "Project could not be deleted."}
        except Exception as e:
            logger.error(f"Error deleting project: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def add_note(self, data: Dict[str, Any]):
        """Add a new note."""
        self._check_ready()
        try:
            content = data.get('content', '').strip()
            project_id = data.get('project_id')
            tags_string = data.get('tags', '').strip()
            
            if not all([content, project_id]):
                return {"status": "error", "message": "Missing content or project_id"}
            
            project = await self.db.projects.find_one({"_id": ObjectId(project_id)})
            if not project:
                return {"status": "error", "message": "Project not found"}
            
            tags = sorted(list(set([tag.strip().lower() for tag in tags_string.split(',') if tag.strip()])))
            
            invite_token = data.get('invite_token')
            shared_token = data.get('shared_token')
            contributor_label_from_post = data.get('contributor_label', '').strip()
            active_prompt = data.get('active_prompt')
            
            new_follow_ups, contributor_label, notify_me, is_shared = [], 'Me', False, False
            
            if invite_token:
                invited_user = await self.db.invited_users.find_one({"token": invite_token, "project_id": ObjectId(project_id)})
                if invited_user:
                    contributor_label = invited_user['label']
                    new_follow_ups = self._get_ai_follow_ups(project['project_goal'], active_prompt or invited_user.get('prompt', ''), content)
                    await self.db.invited_users.update_one({"token": invite_token}, {"$set": {"last_suggested_questions": new_follow_ups}})
                    notify_me = True
            elif shared_token:
                shared_invite = await self.db.shared_invites.find_one({"token": shared_token, "project_id": ObjectId(project_id)})
                if shared_invite and contributor_label_from_post:
                    contributor_label = contributor_label_from_post
                    notify_me = True
                    is_shared = True
                else:
                    return {"status": "error", "message": "Invalid shared token or missing name."}
            else:
                # This should be authenticated, but we'll check the user from context
                contributor_label = 'Me'
            
            new_note_doc = {
                'project_id': ObjectId(project_id),
                'user_id': project['user_id'],
                'content': content,
                'timestamp': datetime.datetime.utcnow(),
                'contributor_label': contributor_label,
                'answered_prompt': active_prompt,
                'tags': tags
            }
            
            if self.is_atlas:
                embedding = self._get_embedding(content)
                if embedding:
                    new_note_doc['content_embedding'] = embedding
            
            result = await self.db.notes.insert_one(new_note_doc)
            new_note_doc['_id'] = str(result.inserted_id)
            new_note_doc['project_id'] = str(new_note_doc['project_id'])
            new_note_doc['formatted_timestamp'] = new_note_doc['timestamp'].strftime('%B %d, %Y, %-I:%M %p')
            del new_note_doc['timestamp']  # Remove datetime object to prevent JSON serialization errors
            new_note_doc['user_id'] = str(new_note_doc.get('user_id', ''))
            
            if notify_me:
                # Note: Email notifications are disabled because we use top-level authentication
                # The user's email would need to come from the top-level users collection or be passed
                # from the route handler via the JWT token. For now, notifications are skipped.
                # TODO: If email notifications are needed, pass project_owner_email from route handler
                # (available in JWT token) or store it in the project document when created.
                pass
            
            if 'content_embedding' in new_note_doc:
                del new_note_doc['content_embedding']
            
            return {"status": "success", "note": new_note_doc, "new_follow_ups": new_follow_ups}
        except Exception as e:
            logger.error(f"Error adding note: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def get_notes(self, user_id: str, project_id: str, page: int = 1, contributor_filter: Optional[str] = None):
        """Get notes for a project."""
        self._check_ready()
        try:
            query = {
                'project_id': ObjectId(project_id),
                'user_id': ObjectId(user_id)
            }
            
            if contributor_filter and contributor_filter != 'All Contributors':
                query['contributor_label'] = contributor_filter
            
            skip_amount = (page - 1) * NOTES_PER_PAGE
            notes_cursor = self.db.notes.find(query).sort("timestamp", -1).skip(skip_amount).limit(NOTES_PER_PAGE)
            notes_data = []
            
            async for note in notes_cursor:
                note['_id'] = str(note['_id'])
                note['project_id'] = str(note['project_id'])
                note['user_id'] = str(note['user_id'])
                note['formatted_timestamp'] = note['timestamp'].strftime('%B %d, %Y, %-I:%M %p')
                del note['timestamp']  # Remove datetime object to prevent JSON serialization errors
                notes_data.append(note)
            
            return notes_data
        except Exception as e:
            logger.error(f"Error getting notes: {e}", exc_info=True)
            return []

    async def update_note(self, user_id: str, note_id: str, data: Dict[str, Any]):
        """Update a note."""
        self._check_ready()
        try:
            note = await self.db.notes.find_one({"_id": ObjectId(note_id)})
            if not note:
                return {"status": "error", "message": "Note not found."}
            
            # Check authorization
            project = await self.db.projects.find_one({"_id": note['project_id']})
            if not project or str(project['user_id']) != user_id:
                return {"status": "error", "message": "Unauthorized action."}
            
            content = data.get('content', '').strip()
            tags_string = data.get('tags', '')
            if not content:
                return {"status": "error", "message": "Content cannot be empty."}
            
            tags = sorted(list(set([tag.strip().lower() for tag in tags_string.split(',') if tag.strip()])))
            
            update_fields = {
                'content': content,
                'tags': tags,
                'updated_at': datetime.datetime.utcnow()
            }
            
            if self.is_atlas and note.get('content') != content:
                embedding = self._get_embedding(content)
                if embedding:
                    update_fields['content_embedding'] = embedding
            
            await self.db.notes.update_one({"_id": ObjectId(note_id)}, {"$set": update_fields})
            
            updated_note = await self.db.notes.find_one({"_id": ObjectId(note_id)})
            updated_note['_id'] = str(updated_note['_id'])
            updated_note['project_id'] = str(updated_note['project_id'])
            updated_note['user_id'] = str(updated_note['user_id'])
            updated_note['formatted_timestamp'] = updated_note['timestamp'].strftime('%B %d, %Y, %-I:%M %p')
            del updated_note['timestamp']  # Remove datetime object to prevent JSON serialization errors
            
            if 'content_embedding' in updated_note:
                del updated_note['content_embedding']
            
            return {"status": "success", "note": updated_note}
        except Exception as e:
            logger.error(f"Error updating note: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def delete_note(self, user_id: str, note_id: str):
        """Delete a note."""
        self._check_ready()
        try:
            note = await self.db.notes.find_one({"_id": ObjectId(note_id)})
            if not note:
                return {"status": "error", "message": "Note not found."}
            
            # Check authorization
            project = await self.db.projects.find_one({"_id": note['project_id']})
            if not project or str(project['user_id']) != user_id:
                return {"status": "error", "message": "Unauthorized action."}
            
            result = await self.db.notes.delete_one({"_id": ObjectId(note_id)})
            if result.deleted_count == 1:
                return {"status": "success", "message": "Note deleted successfully."}
            return {"status": "error", "message": "Note could not be deleted."}
        except Exception as e:
            logger.error(f"Error deleting note: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def generate_invite_token(self, user_id: str, project_id: str, label: str, prompt: str):
        """Generate an invite token."""
        self._check_ready()
        try:
            project = await self.db.projects.find_one({"_id": ObjectId(project_id), "user_id": ObjectId(user_id)})
            if not project:
                return {"status": "error", "message": "Project not found or unauthorized."}
            
            new_token = str(uuid.uuid4())
            await self.db.invited_users.insert_one({
                "token": new_token,
                "label": label,
                "project_id": ObjectId(project_id),
                "prompt": prompt,
                "created_at": datetime.datetime.utcnow()
            })
            
            invite_url = f"/experiments/storyweaver/invite/{new_token}"  # Base URL will be added by frontend
            return {"status": "success", "label": label, "invite_url": invite_url}
        except Exception as e:
            logger.error(f"Error generating invite token: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def generate_shared_token(self, user_id: str, project_id: str, prompt: str):
        """Generate a shared invite token."""
        self._check_ready()
        try:
            project = await self.db.projects.find_one({"_id": ObjectId(project_id), "user_id": ObjectId(user_id)})
            if not project:
                return {"status": "error", "message": "Project not found or unauthorized."}
            
            new_token = str(uuid.uuid4())
            await self.db.shared_invites.insert_one({
                "token": new_token,
                "project_id": ObjectId(project_id),
                "user_id": ObjectId(user_id),
                "prompt": prompt,
                "created_at": datetime.datetime.utcnow()
            })
            
            shared_url = f"/experiments/storyweaver/share/{new_token}"  # Base URL will be added by frontend
            return {"status": "success", "shared_url": shared_url}
        except Exception as e:
            logger.error(f"Error generating shared token: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    async def suggest_tags(self, user_id: str, project_id: str, content: str):
        """Get AI-suggested tags."""
        self._check_ready()
        try:
            project = await self.db.projects.find_one({"_id": ObjectId(project_id), "user_id": ObjectId(user_id)})
            if not project:
                return {"error": "Project not found or unauthorized."}
            
            suggested_tags = await self._get_ai_suggested_tags(project_id, user_id, content)
            return {"tags": suggested_tags}
        except Exception as e:
            logger.error(f"Error suggesting tags: {e}", exc_info=True)
            return {"error": str(e)}

    async def search_notes(self, user_id: str, project_id: str, search_query: Optional[str], tags_filter: Optional[str], search_type: str, page: int):
        """Search notes using vector search (if available) or text search (following data_imaging pattern)."""
        self._check_ready()
        try:
            per_page = 20
            skip = (page - 1) * per_page
            notes_data = []
            
            # Try vector search if available and search_type is "vector" or "semantic"
            use_vector_search = (
                self.is_atlas and 
                search_query and 
                search_type in ["vector", "semantic"] and
                OPENAI_API_KEY
            )
            
            if use_vector_search:
                try:
                    # Generate embedding for search query
                    query_embedding = self._get_embedding(search_query)
                    if query_embedding:
                        # Build filter for project_id and user_id
                        vector_filter = {
                            "project_id": ObjectId(project_id),
                            "user_id": ObjectId(user_id)
                        }
                        
                        # Add tags filter if provided
                        if tags_filter:
                            tags_list = [tag.strip().lower() for tag in tags_filter.split(',') if tag.strip()]
                            vector_filter['tags'] = {'$all': tags_list}
                        
                        # Vector search pipeline (following data_imaging pattern)
                        pipeline = [
                            {
                                "$vectorSearch": {
                                    "index": "notes_vector_embedding_index",
                                    "path": "content_embedding",
                                    "queryVector": query_embedding,
                                    "numCandidates": max(50, per_page * 3),
                                    "limit": per_page * 2,
                                    "filter": vector_filter
                                }
                            },
                            {
                                "$project": {
                                    "_id": 1,
                                    "project_id": 1,
                                    "user_id": 1,
                                    "content": 1,
                                    "timestamp": 1,
                                    "contributor_label": 1,
                                    "tags": 1,
                                    "answered_prompt": 1,
                                    "score": {"$meta": "vectorSearchScore"}
                                }
                            }
                        ]
                        
                        # Use raw access for vector search aggregation (following data_imaging pattern)
                        cur = self.db.raw.notes.aggregate(pipeline)
                        vector_results = await cur.to_list(length=None)
                        
                        # Process results
                        for note in vector_results[skip:skip + per_page]:
                            note['_id'] = str(note['_id'])
                            note['project_id'] = str(note['project_id'])
                            note['user_id'] = str(note['user_id'])
                            note['formatted_timestamp'] = note['timestamp'].strftime('%B %d, %Y, %-I:%M %p')
                            del note['timestamp']
                            notes_data.append(note)
                        
                        total_notes = len(vector_results)
                        total_pages = (total_notes + per_page - 1) // per_page if per_page > 0 else 0
                        
                        return {
                            "notes": notes_data,
                            "total_pages": total_pages,
                            "current_page": page,
                            "total_notes": total_notes
                        }
                except OperationFailure as oe:
                    err_msg = oe.details.get('errmsg', str(oe)) if hasattr(oe, 'details') else str(oe)
                    logger.warning(f"Vector search failed, falling back to text search: {err_msg}")
                    # Fall through to text search
                except Exception as e:
                    logger.warning(f"Vector search error, falling back to text search: {e}")
                    # Fall through to text search
            
            # Fall back to text search or use text search by default
            query = {
                'project_id': ObjectId(project_id),
                'user_id': ObjectId(user_id)
            }
            
            if tags_filter:
                tags_list = [tag.strip().lower() for tag in tags_filter.split(',') if tag.strip()]
                query['tags'] = {'$all': tags_list}
            
            if search_query:
                query['$text'] = {'$search': search_query}
            
            cursor = self.db.notes.find(query).sort([('score', {'$meta': 'textScore'})] if search_query else [("timestamp", -1)]).skip(skip).limit(per_page)
            
            async for note in cursor:
                note['_id'] = str(note['_id'])
                note['project_id'] = str(note['project_id'])
                note['user_id'] = str(note['user_id'])
                note['formatted_timestamp'] = note['timestamp'].strftime('%B %d, %Y, %-I:%M %p')
                del note['timestamp']  # Remove datetime object to prevent JSON serialization errors
                notes_data.append(note)
            
            total_notes = await self.db.notes.count_documents(query)
            total_pages = (total_notes + per_page - 1) // per_page if per_page > 0 else 0
            
            return {
                "notes": notes_data,
                "total_pages": total_pages,
                "current_page": page,
                "total_notes": total_notes
            }
        except Exception as e:
            logger.error(f"Error searching notes: {e}", exc_info=True)
            return {"notes": [], "total_pages": 0, "current_page": page, "total_notes": 0}

    async def get_tags(self, user_id: str, project_id: str):
        """Get all tags for a project."""
        self._check_ready()
        try:
            pipeline = [
                {'$match': {'project_id': ObjectId(project_id), 'user_id': ObjectId(user_id)}},
                {'$unwind': '$tags'},
                {'$group': {'_id': '$tags'}},
                {'$sort': {'_id': 1}}
            ]
            tags = []
            async for doc in self.db.notes.aggregate(pipeline):
                tags.append(doc['_id'])
            return tags
        except Exception as e:
            logger.error(f"Error getting tags: {e}", exc_info=True)
            return []

    async def get_contributors(self, user_id: str, project_id: str):
        """Get all contributors for a project."""
        self._check_ready()
        try:
            labels = set()
            async for note in self.db.notes.find({'project_id': ObjectId(project_id), 'user_id': ObjectId(user_id)}):
                if note.get('contributor_label'):
                    labels.add(note['contributor_label'])
            
            async for invite in self.db.invited_users.find({'project_id': ObjectId(project_id)}):
                if invite.get('label'):
                    labels.add(invite['label'])
            
            sorted_labels = sorted(list(labels - {'Me'}))
            if 'Me' in labels:
                sorted_labels.insert(0, 'Me')
            
            return ['All Contributors'] + sorted_labels
        except Exception as e:
            logger.error(f"Error getting contributors: {e}", exc_info=True)
            return ['All Contributors']

    async def generate_notes(self, user_id: str, project_id: str, topic: str, num_notes: int = 5, use_web_search: bool = False):
        """Generate AI study notes."""
        self._check_ready()
        try:
            project = await self.db.projects.find_one({"_id": ObjectId(project_id), "user_id": ObjectId(user_id)})
            if not project:
                return {"error": "Project not found or unauthorized."}
            
            generated_notes_content = self._get_ai_study_notes(topic, project.get('project_goal', ''), num_notes, use_web_search=use_web_search)
            if not generated_notes_content:
                return {"error": "AI failed to generate notes."}
            
            new_notes_docs = []
            for content in generated_notes_content:
                note_doc = {
                    'project_id': ObjectId(project_id),
                    'user_id': project['user_id'],
                    'content': content,
                    'timestamp': datetime.datetime.utcnow(),
                    'contributor_label': 'AI Assistant',
                    'tags': ['ai-generated', topic.lower().replace(' ', '-')]
                }
                
                if self.is_atlas:
                    embedding = self._get_embedding(content)
                    if embedding:
                        note_doc['content_embedding'] = embedding
                
                result = await self.db.notes.insert_one(note_doc)
                note_doc['_id'] = str(result.inserted_id)
                note_doc['project_id'] = str(note_doc['project_id'])
                note_doc['user_id'] = str(note_doc['user_id'])
                note_doc['formatted_timestamp'] = note_doc['timestamp'].strftime('%B %d, %Y, %-I:%M %p')
                del note_doc['timestamp']  # Remove datetime object to prevent JSON serialization errors
                
                if 'content_embedding' in note_doc:
                    del note_doc['content_embedding']
                
                new_notes_docs.append(note_doc)
            
            return {"status": "success", "notes": new_notes_docs}
        except Exception as e:
            logger.error(f"Error generating notes: {e}", exc_info=True)
            return {"error": str(e)}

    async def generate_quiz(self, user_id: str, data: Dict[str, Any]):
        """Generate a quiz from notes."""
        self._check_ready()
        try:
            selected_notes = data.get('notes', [])
            quiz_title = data.get('title', 'Generated Quiz').strip()
            num_questions = data.get('num_questions', 5)
            question_type = data.get('question_type', 'Multiple Choice')
            difficulty = data.get('difficulty', 'Medium')
            knowledge_source = data.get('knowledge_source', 'notes_and_ai')
            
            if not selected_notes:
                return {"error": "No notes were selected to generate the quiz."}
            
            first_note_id = selected_notes[0].get('_id')
            note_check = await self.db.notes.find_one({"_id": ObjectId(first_note_id), "user_id": ObjectId(user_id)})
            if not note_check:
                return {"error": "Unauthorized"}
            
            project_id = note_check['project_id']
            formatted_notes = "\n---\n".join([note.get('content', '') for note in selected_notes])
            
            quiz_data = self._get_ai_quiz(formatted_notes, num_questions, question_type, difficulty, knowledge_source)
            if not quiz_data:
                return {"error": "Failed to generate quiz from AI. The source notes might be insufficient."}
            
            share_token = str(uuid.uuid4())
            
            quiz_doc = {
                "user_id": ObjectId(user_id),
                "project_id": project_id,
                "title": quiz_title,
                "quiz_data": quiz_data,
                "question_type": question_type,
                "source_note_ids": [ObjectId(note['_id']) for note in selected_notes],
                "created_at": datetime.datetime.utcnow(),
                "share_token": share_token
            }
            
            await self.db.quizzes.insert_one(quiz_doc)
            
            shareable_url = f"/experiments/storyweaver/quiz/{share_token}"  # Base URL will be added by frontend
            return {"quiz": quiz_data, "question_type": question_type, "shareable_url": shareable_url}
        except Exception as e:
            logger.error(f"Error generating quiz: {e}", exc_info=True)
            return {"error": str(e)}

    async def generate_story(self, user_id: str, data: Dict[str, Any]):
        """Generate a story from notes."""
        self._check_ready()
        try:
            project_name = data.get('project_name')
            tone = data.get('tone')
            story_format = data.get('format')
            selected_notes = data.get('notes', [])
            
            if not all([project_name, tone, story_format, selected_notes]):
                return {"error": "Project name, tone, format, and selected notes are required."}
            
            first_note_id = selected_notes[0].get('_id')
            note_check = await self.db.notes.find_one({"_id": ObjectId(first_note_id), "user_id": ObjectId(user_id)})
            if not note_check:
                return {"error": "Unauthorized"}
            
            formatted_notes = "".join([f"- From {note.get('contributor_label', 'Me')}: \"{note.get('content', '')}\"\n" for note in selected_notes])
            
            story = self._get_ai_story(formatted_notes, project_name, tone, story_format)
            return {"story": story}
        except Exception as e:
            logger.error(f"Error generating story: {e}", exc_info=True)
            return {"error": str(e)}

    async def generate_study_guide(self, user_id: str, data: Dict[str, Any]):
        """Generate a study guide from notes."""
        self._check_ready()
        try:
            project_name = data.get('project_name')
            selected_notes = data.get('notes', [])
            
            if not all([project_name, selected_notes]):
                return {"error": "Project name and selected notes are required."}
            
            first_note_id = selected_notes[0].get('_id')
            note_check = await self.db.notes.find_one({"_id": ObjectId(first_note_id), "user_id": ObjectId(user_id)})
            if not note_check:
                return {"error": "Unauthorized"}
            
            formatted_notes = "\n---\n".join([note.get('content', '') for note in selected_notes])
            study_guide_md = self._get_ai_study_guide(formatted_notes, project_name)
            return {"study_guide": study_guide_md}
        except Exception as e:
            logger.error(f"Error generating study guide: {e}", exc_info=True)
            return {"error": str(e)}

    async def generate_song(self, user_id: str, data: Dict[str, Any]):
        """Generate song lyrics from notes."""
        self._check_ready()
        try:
            project_name = data.get('project_name')
            genre = data.get('genre')
            mood = data.get('mood')
            selected_notes = data.get('notes', [])
            
            if not all([project_name, genre, mood, selected_notes]):
                return {"error": "Project name, genre, mood, and selected notes are required."}
            
            first_note_id = selected_notes[0].get('_id')
            note_check = await self.db.notes.find_one({"_id": ObjectId(first_note_id), "user_id": ObjectId(user_id)})
            if not note_check:
                return {"error": "Unauthorized action."}
            
            formatted_notes = "\n---\n".join([f"Note from {note.get('contributor_label', 'Me')}: \"{note.get('content', '')}\"" for note in selected_notes])
            song_lyrics = self._get_ai_song(formatted_notes, project_name, genre, mood)
            
            if "Error:" in song_lyrics:
                return {"error": "Failed to generate song lyrics from AI."}
            
            return {"song_lyrics": song_lyrics}
        except Exception as e:
            logger.error(f"Error generating song: {e}", exc_info=True)
            return {"error": str(e)}

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
        logger.info("Checking for demo user seeding...")
        
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

