# Story Weaver Experiment

A collaborative writing and research platform for collecting, organizing, and synthesizing stories and study notes with AI-powered features.

## Structure

- `manifest.json` - Experiment configuration with index definitions
- `__init__.py` - FastAPI routes that delegate to the Ray Actor
- `actor.py` - Ray Actor handling all business logic using ExperimentDB
- `requirements.txt` - Python dependencies
- `templates/` - Jinja2 templates (needs to be populated with templates from Flask app)
- `static/` - Static files (CSS, JS, images)

## Templates Needed

The following templates from the Flask app should be placed in the `templates/` directory:

- `base.html` - Base template with header/navigation
- `index.html` - Workspace view with project list
- `project.html` - Project detail view
- `invite.html` - Invite page for contributors
- `share.html` - Shared invite page
- `shared_quiz.html` - Shared quiz page

**Note:** Story Weaver does NOT include login/register templates - authentication is handled at the top level. Users must be created by admins via the admin panel.

## Static Files

Static files (CSS, JS) from the Flask app should be placed in the `static/` directory:

- `static/css/styles.css` - Main stylesheet
- `static/js/main.js` - Main JavaScript file

## Environment Variables

The following environment variables are needed:

- `OPENAI_API_KEY` - OpenAI API key for AI features
- `MONGO_URI` - MongoDB connection string
- `FLASK_SECRET_KEY` - Secret key for JWT tokens
- `MAILGUN_API_KEY` - (Optional) Mailgun API key for email notifications
- `MAILGUN_DOMAIN` - (Optional) Mailgun domain for email notifications

## Indexes

The experiment automatically manages indexes defined in `manifest.json`:

- Vector search index for semantic note search
- Lucene text search index for keyword search
- Regular indexes for performance optimization

## Authentication

**IMPORTANT:** Story Weaver uses top-level authentication only. **Registration is disabled.**

### User Management (Admin Only)
- **Users MUST be created by admins** at the top level (via admin panel)
- Story Weaver does NOT provide any registration functionality
- Users cannot self-register - all accounts must be created by administrators
- Users login at `/login` (root level), not within Story Weaver

### Technical Details
- All `user_id` values reference the top-level `users` collection (not experiment-scoped)
- Unauthenticated users are automatically redirected to the top-level login page
- The JWT token contains `user_id` and `email` from the top-level auth system
- Story Weaver projects are scoped to the experiment but use top-level user IDs

## Features

- **Top-level authentication** (users managed by admins only)
- Project management (story/study types)
- Collaborative note-taking with invites
- AI-powered features:
  - Embedding generation for vector search
  - Follow-up question generation
  - Tag suggestions
  - Study note generation
  - Quiz generation
  - Story generation
  - Study guide generation
  - Song lyric generation

