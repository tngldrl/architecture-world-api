from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
import httpx
import os
import json
import hashlib
import hmac
import logging
from typing import Optional, Dict, List
from dotenv import load_dotenv

load_dotenv()

from database import engine, Base, get_db
import models
from auth import verify_token
from github_app import (
    get_installation_access_token,
    get_github_file_content,
    parse_github_repo_url,
    build_authenticated_clone_url,
)

logger = logging.getLogger(__name__)

GITHUB_APP_WEBHOOK_SECRET = os.environ.get("GITHUB_APP_WEBHOOK_SECRET", "")
GITHUB_APP_INSTALL_URL = os.environ.get("GITHUB_APP_INSTALL_URL", "")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
VERTEX_AI_LOCATION = os.environ.get("VERTEX_AI_LOCATION", "us-central1")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Max hops for agentic code retrieval during chat
MAX_RETRIEVAL_HOPS = 3
# Max additional files to fetch per hop
MAX_FILES_PER_HOP = 3

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Architecture World API")

allowed_origins = [
    "http://localhost:3000",
    "https://architecture-world-web-ulti3dddka-an.a.run.app",
]
allowed_origins_env = os.environ.get("ALLOWED_ORIGINS")
if allowed_origins_env:
    allowed_origins.extend([origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()])

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def find_repository_by_url(db: Session, url: str, project_id: Optional[str] = None) -> Optional[models.Repository]:
    """
    Finds a Repository in the database matching a given URL by comparing their normalized
    (owner, repo) pairs, ignoring casing and the '.git' suffix.
    """
    try:
        target_owner, target_repo = parse_github_repo_url(url)
    except ValueError:
        return None

    query = db.query(models.Repository)
    if project_id:
        query = query.filter(models.Repository.project_id == project_id)

    all_repos = query.all()
    for repo in all_repos:
        try:
            owner, name = parse_github_repo_url(repo.url)
            if owner.lower() == target_owner.lower() and name.lower() == target_repo.lower():
                return repo
        except ValueError:
            continue
    return None

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8001")
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

class RepositoryInput(BaseModel):
    url: str
    webhook_enabled: bool = False
    watch_branch: Optional[str] = None

class AnalyzeRequest(BaseModel):
    repo_urls: Optional[list[str]] = None  # Legacy: plain list of URLs
    repositories: Optional[list[RepositoryInput]] = None  # New: per-repo settings
    project_name: Optional[str] = None
    github_installation_id: Optional[str] = None  # From GitHub App callback

class CheckAccessRequest(BaseModel):
    url: str
    installation_id: Optional[str] = None


async def run_analysis_task(
    project_id: str,
    repo_urls: list[str],
    github_installation_id: Optional[str] = None,
):
    callback_url = f"{API_BASE_URL}/api/projects/{project_id}/callback"

    iat: Optional[str] = None
    if github_installation_id:
        try:
            iat = get_installation_access_token(github_installation_id)
        except Exception as e:
            logger.warning(
                "Failed to obtain IAT for installation %s: %s – falling back to unauthenticated",
                github_installation_id, e,
            )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{MCP_URL}/analyze",
                json={
                    "repo_urls": repo_urls,
                    "project_id": project_id,
                    "callback_url": callback_url,
                    "github_installation_access_token": iat,
                }
            )
            response.raise_for_status()
    except Exception as e:
        print(f"Failed to queue analysis task on MCP: {e}")
        from database import SessionLocal
        db = SessionLocal()
        project = db.query(models.Project).filter(models.Project.id == project_id).first()
        if project:
            project.status = "error"
            db.commit()
        db.close()

class CallbackPayload(BaseModel):
    project_id: str
    status: str
    data: dict = None
    error: str = None

@app.post("/api/projects/{project_id}/callback")
async def analysis_callback(project_id: str, payload: CallbackPayload, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    if payload.status == "error":
        print(f"Analysis callback reported error for project {project_id}: {payload.error}")
        project.status = "error"
        db.commit()
        return {"status": "error_recorded"}
        
    data = payload.data or {}
    microservices = data.get("microservices", [])
    
    try:
        # 1. Insert microservices and build name -> id map
        name_to_id = {}
        for ms in microservices:
            # Resolve repository_id based on repository_url from LLM response
            repo_url = ms.get("repository_url")
            db_repo = None
            if repo_url:
                db_repo = find_repository_by_url(db, repo_url, project_id=project.id)
            
            if not db_repo:
                # Fallback: project's first repository
                db_repo = db.query(models.Repository).filter(
                    models.Repository.project_id == project.id
                ).first()
            
            repository_id = db_repo.id if db_repo else None

            # Use ms_id if provided, else generate one via DB or just use the name as fallback
            # Serialize key_files for DB storage
            raw_key_files = ms.get("key_files", [])
            key_files_json = json.dumps(raw_key_files) if raw_key_files else None

            db_ms = models.Microservice(
                project_id=project.id,
                repository_id=repository_id,
                ms_id=ms.get("id"),
                name=ms.get("name"),
                description=ms.get("description"),
                ai_prompt_context=(
                    f"You are the {ms.get('role_type', 'staff')} in a restaurant. "
                    f"Your component is '{ms.get('name', 'Unknown')}'. "
                    f"Your description is: {ms.get('description', 'No description')} "
                    f"Scale/Complexity: {ms.get('scale_and_complexity', 'Unknown')} "
                    f"Importance: {ms.get('importance_and_centrality', 'Unknown')} "
                    "Respond to the user as this persona, providing helpful architectural information."
                ),
                avatar_visual_prompt=ms.get("avatar_prompt"),
                avatar_image_url=ms.get("avatar_image_url"),
                position_x=ms.get("position", {}).get("x", 0.0),
                position_y=ms.get("position", {}).get("y", 0.0),
                scale_tier=ms.get("scale_tier", 3),
                key_files=key_files_json,
            )
            db.add(db_ms)
            db.flush() # To get the db_ms.id
            
            resolved_id = db_ms.ms_id or db_ms.id
            name_to_id[ms.get("name")] = resolved_id

        # 2. Extract nested dependencies and map source/target to the resolved IDs
        for ms in microservices:
            source_id = name_to_id.get(ms.get("name"))
            if not source_id:
                continue
                
            deps = ms.get("dependencies", [])
            for dep in deps:
                target_name = dep.get("service_name")
                target_id = name_to_id.get(target_name)
                
                if target_id:
                    db_dep = models.Dependency(
                        project_id=project.id,
                        dep_id=None,
                        source_service_id=source_id,
                        target_service_id=target_id,
                        relationship_type=dep.get("description")
                    )
                    db.add(db_dep)
            
        project.status = "ready"
        db.commit()
        print(f"Project {project_id} analysis results successfully saved via callback.")
        return {"status": "processed"}
        
    except Exception as e:
        print(f"Error processing callback data for project {project_id}: {e}")
        project.status = "error"
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/projects/analyze")
async def start_analysis(
    req: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token)
):
    # Normalize input: support both legacy repo_urls and new repositories format
    if req.repositories:
        repo_inputs = req.repositories
    elif req.repo_urls:
        repo_inputs = [RepositoryInput(url=u.strip()) for u in req.repo_urls if u.strip()]
    else:
        raise HTTPException(status_code=400, detail="No URLs provided")

    repo_inputs = [r for r in repo_inputs if r.url.strip()]
    if not repo_inputs:
        raise HTTPException(status_code=400, detail="No URLs provided")

    # Validate: webhook_enabled=True requires watch_branch
    for r in repo_inputs:
        if r.webhook_enabled and not r.watch_branch:
            raise HTTPException(
                status_code=400,
                detail=f"watch_branch is required when webhook_enabled=true (url: {r.url})"
            )

    project = models.Project(
        status="analyzing",
        name=req.project_name,
        user_id=user["uid"],
        github_installation_id=req.github_installation_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    # Save repository settings to DB
    urls = []
    for r in repo_inputs:
        db_repo = models.Repository(
            project_id=project.id,
            url=r.url.strip(),
            webhook_enabled=r.webhook_enabled,
            watch_branch=r.watch_branch,
        )
        db.add(db_repo)
        urls.append(r.url.strip())
    db.commit()

    background_tasks.add_task(
        run_analysis_task,
        project.id,
        urls,
        req.github_installation_id,
    )

    return {"project_id": project.id, "status": project.status}


@app.post("/api/projects/{project_id}/re-analyze")
async def re_analyze_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token),
):
    """
    Triggers re-analysis of an existing project. Called when the user clicks
    the 'Update' button after a push notification is received.
    Immediately resets has_update to False before starting analysis.
    """
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.user_id == user["uid"],
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if project.status == "analyzing":
        raise HTTPException(status_code=409, detail="Project is already being analyzed")

    # Reset has_update immediately (idempotent – another push can set it back to True)
    project.status = "analyzing"
    project.has_update = False
    db.commit()

    # Delete existing microservices and dependencies so re-analysis writes fresh data
    db.query(models.Microservice).filter(models.Microservice.project_id == project_id).delete()
    db.query(models.Dependency).filter(models.Dependency.project_id == project_id).delete()
    db.commit()

    urls = [r.url for r in project.repositories]
    background_tasks.add_task(
        run_analysis_task,
        project.id,
        urls,
        project.github_installation_id,
    )

    return {"project_id": project.id, "status": "analyzing"}


@app.get("/api/github-app/install-url")
def get_github_app_install_url(user: dict = Depends(verify_token)):
    """Return the GitHub App installation URL for the frontend."""
    if not GITHUB_APP_INSTALL_URL:
        raise HTTPException(status_code=501, detail="GitHub App is not configured.")
    return {"install_url": GITHUB_APP_INSTALL_URL}


@app.post("/api/github-app/save-installation")
async def save_github_app_installation(
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token),
):
    """
    Called by the frontend after the user completes GitHub App installation.
    Receives the installation_id from the GitHub App callback redirect
    and associates it with the user's latest pending project (if any).
    """
    body = await request.json()
    installation_id = str(body.get("installation_id", "")).strip()
    project_id = body.get("project_id")  # optional: associate with specific project

    if not installation_id:
        raise HTTPException(status_code=400, detail="installation_id is required")

    if project_id:
        project = db.query(models.Project).filter(
            models.Project.id == project_id,
            models.Project.user_id == user["uid"],
        ).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        project.github_installation_id = installation_id
        db.commit()

    return {"status": "saved", "installation_id": installation_id}

@app.post("/api/github-app/check-access")
async def check_github_app_access(
    body: CheckAccessRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token),
):
    """
    Checks if any GitHub App installation associated with the user (or passed in the request)
    has access to the target repository.
    """
    repo_url = body.url.strip()
    if not repo_url:
        raise HTTPException(status_code=400, detail="Repository URL is required")

    try:
        owner, repo = parse_github_repo_url(repo_url)
    except ValueError:
        return {"has_access": False}

    # Gather user's unique installation IDs from past projects
    user_projects = db.query(models.Project).filter(
        models.Project.user_id == user["uid"],
        models.Project.github_installation_id.isnot(None),
    ).all()

    installation_ids = list(set([p.github_installation_id for p in user_projects if p.github_installation_id]))

    # Append request-provided installation_id if present (e.g. from redirect before project is saved)
    if body.installation_id:
        installation_ids.append(body.installation_id)
        installation_ids = list(set(installation_ids))

    if not installation_ids:
        return {"has_access": False}

    # Query GitHub API using each installation ID to see if any token has access to the repo
    target_full_name = f"{owner}/{repo}".lower()
    for inst_id in installation_ids:
        try:
            iat = get_installation_access_token(inst_id)
            headers = {
                "Authorization": f"Bearer {iat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            api_url = "https://api.github.com/installation/repositories"
            params = {"per_page": 100}
            
            async with httpx.AsyncClient(timeout=5.0) as client:
                while api_url:
                    resp = await client.get(
                        api_url, 
                        headers=headers, 
                        params=params if "installation/repositories" in api_url else None
                    )
                    if resp.status_code != 200:
                        break
                    
                    data = resp.json()
                    repos = data.get("repositories", [])
                    for r_item in repos:
                        r_full_name = r_item.get("full_name", "").lower()
                        if r_full_name == target_full_name:
                            return {"has_access": True}
                    
                    # Pagination support
                    next_url = None
                    if "Link" in resp.headers:
                        links = resp.headers["Link"].split(",")
                        for link in links:
                            if 'rel="next"' in link:
                                next_url = link.split(";")[0].strip("<> ")
                                break
                    api_url = next_url
        except Exception as e:
            logger.warning("Failed checking access for installation %s on %s/%s: %s", inst_id, owner, repo, e)
            continue

    return {"has_access": False}


@app.get("/api/projects")
def list_projects(
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token)
):
    projects = db.query(models.Project).filter(
        models.Project.user_id == user["uid"]
    ).order_by(models.Project.created_at.desc()).all()
    
    return [
        {
            "id": proj.id,
            "name": proj.name,
            "status": proj.status,
            "has_update": proj.has_update,
            "created_at": proj.created_at.isoformat() if proj.created_at else None
        }
        for proj in projects
    ]

@app.get("/api/projects/{project_id}")
def get_project(
    project_id: str, 
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token)
):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    if project.status != "ready":
        return {"id": project.id, "name": project.name, "status": project.status}
        
    repositories = [
        {
            "id": repo.id,
            "url": repo.url
        }
        for repo in project.repositories
    ]

    microservices = [
        {
            "id": ms.ms_id or ms.id,
            "name": ms.name,
            "description": ms.description,
            "repository_id": ms.repository_id,
            "avatar_visual_prompt": ms.avatar_visual_prompt,
            "avatar_image_url": ms.avatar_image_url,
            "position": {"x": ms.position_x, "y": ms.position_y},
            "scale_tier": ms.scale_tier
        }
        for ms in project.microservices
    ]
    
    dependencies = [
        {
            "id": dep.dep_id or dep.id,
            "source": dep.source_service_id,
            "target": dep.target_service_id,
            "type": dep.relationship_type
        }
        for dep in project.dependencies
    ]
    
    return {
        "id": project.id,
        "name": project.name,
        "status": project.status,
        "has_update": project.has_update,
        "repositories": repositories,
        "microservices": microservices,
        "dependencies": dependencies
    }

class ChatMessage(BaseModel):
    message: str

@app.get("/api/microservices/{ms_id}/chat")
def get_chat_history(
    ms_id: str, 
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token)
):
    ms = db.query(models.Microservice).filter(models.Microservice.id == ms_id).first()
    if not ms:
        raise HTTPException(status_code=404, detail="Microservice not found")
        
    chat = db.query(models.ChatHistory).filter(models.ChatHistory.microservice_id == ms.id).first()
    messages = json.loads(chat.messages) if chat else []
    return {"messages": messages}

@app.post("/api/microservices/{ms_id}/chat")
async def send_chat_message(
    ms_id: str,
    req: ChatMessage,
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token)
):
    ms = db.query(models.Microservice).filter(models.Microservice.id == ms_id).first()
    if not ms:
        raise HTTPException(status_code=404, detail="Microservice not found")

    chat = db.query(models.ChatHistory).filter(models.ChatHistory.microservice_id == ms.id).first()
    if not chat:
        chat = models.ChatHistory(microservice_id=ms.id, messages="[]")
        db.add(chat)
        db.commit()
        db.refresh(chat)

    history = json.loads(chat.messages)

    # -----------------------------------------------------------------------
    # Agentic code retrieval: fetch source code from GitHub and inject into
    # the system prompt so the LLM can answer based on actual implementation.
    # -----------------------------------------------------------------------
    enriched_system_prompt = ms.ai_prompt_context or "You are a helpful assistant."
    try:
        project = db.query(models.Project).filter(models.Project.id == ms.project_id).first()
        code_context = await _retrieve_code_context(ms, project, req.message)
        if code_context:
            code_section = "\n\n".join(
                f"### {path}\n```\n{content[:4000]}\n```"  # truncate very large files
                for path, content in code_context.items()
            )
            enriched_system_prompt = (
                enriched_system_prompt
                + "\n\n## Source Code Reference\n"
                + "The following source files are provided for reference. "
                + "Use them to give accurate, implementation-specific answers.\n\n"
                + code_section
            )
            logger.info(
                "Injected %d source files into chat context for microservice %s",
                len(code_context), ms_id,
            )
    except Exception as e:
        logger.warning("Code retrieval failed for microservice %s: %s", ms_id, e)
        # Fall through – chat still works without source code context

    # Call MCP
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{MCP_URL}/chat", json={
                "system_prompt": enriched_system_prompt,
                "history": history,
                "new_message": req.message,
            })
            response.raise_for_status()
            data = response.json()
            reply = data.get("response", "I'm sorry, I couldn't process that.")
    except Exception as e:
        reply = f"Error calling MCP: {str(e)}"

    # Append to history
    history.append({"role": "user", "content": req.message})
    history.append({"role": "model", "content": reply})

    chat.messages = json.dumps(history)
    db.commit()

    return {"reply": reply, "messages": history}


# ---------------------------------------------------------------------------
# Agentic code retrieval helpers
# ---------------------------------------------------------------------------

async def _retrieve_code_context(
    ms: models.Microservice,
    project: models.Project,
    question: str,
) -> Dict[str, str]:
    """
    Fetch relevant source files from GitHub for a chat message.

    Strategy:
      Hop 0  – Fetch all key_files stored during analysis (deterministic)
      Hop 1-3 – Ask Gemini which additional files are needed; fetch them

    Returns a dict of {relative_path: file_content}.
    Returns {} if no key_files are stored or GitHub access is unavailable.
    """
    key_files: list[dict] = json.loads(ms.key_files or "[]")
    if not key_files:
        return {}  # No exploration hints available yet

    # Determine repository URL for this microservice
    repo = db_repo = None
    if ms.repository_id:
        from database import SessionLocal
        _db = SessionLocal()
        try:
            db_repo = _db.query(models.Repository).filter(
                models.Repository.id == ms.repository_id
            ).first()
        finally:
            _db.close()

    if not db_repo:
        return {}

    repo_url = db_repo.url
    try:
        owner, repo_name = parse_github_repo_url(repo_url)
    except ValueError:
        return {}

    # Obtain GitHub token (IAT for private, or None for public repos)
    github_token: Optional[str] = None
    if project and project.github_installation_id:
        try:
            github_token = get_installation_access_token(project.github_installation_id)
        except Exception as e:
            logger.warning("Failed to get IAT, will attempt unauthenticated: %s", e)

    if not github_token:
        # For public repos, GitHub Contents API works without auth (lower rate limit)
        github_token = os.environ.get("GITHUB_PUBLIC_TOKEN", "")

    fetched: Dict[str, str] = {}

    # Hop 0: fetch key_files deterministically
    for kf in key_files:
        path = kf.get("path", "").strip()
        if not path:
            continue
        content = get_github_file_content(owner, repo_name, path, github_token)
        if content:
            fetched[path] = content

    if not fetched:
        return {}

    # Hop 1-3: Gemini identifies additional files needed to answer the question
    for hop in range(MAX_RETRIEVAL_HOPS):
        additional_paths = await _identify_additional_files(
            question=question,
            fetched=fetched,
            ms_description=ms.description or "",
        )
        if not additional_paths:
            break  # Gemini says it has enough context

        fetched_this_hop = 0
        for path in additional_paths:
            if fetched_this_hop >= MAX_FILES_PER_HOP:
                break
            if path in fetched:
                continue  # Already have it
            content = get_github_file_content(owner, repo_name, path, github_token)
            if content:
                fetched[path] = content
                fetched_this_hop += 1

        if fetched_this_hop == 0:
            break  # No new files were fetchable

    return fetched


async def _identify_additional_files(
    question: str,
    fetched: Dict[str, str],
    ms_description: str,
) -> List[str]:
    """
    Ask Gemini (via MCP) which additional source files are needed to answer
    the user's question, given the already-fetched files.

    Returns a list of relative file paths (max MAX_FILES_PER_HOP items).
    Returns [] if Gemini says no more files are needed.
    """
    if not GCP_PROJECT_ID:
        return []

    already_fetched_summary = "\n".join(
        f"- {path} ({len(content)} chars)" for path, content in fetched.items()
    )
    fetched_snippets = "\n\n".join(
        f"=== {path} ===\n{content[:1500]}"  # show first 1500 chars per file
        for path, content in fetched.items()
    )

    prompt = f"""You are helping answer a user's question about a microservice.

Service description: {ms_description}

User question: {question}

Files already fetched:
{already_fetched_summary}

Content of fetched files (truncated):
{fetched_snippets}

Based on the above, are there additional source files in the same repository that would 
help answer the question more accurately? If yes, list up to {MAX_FILES_PER_HOP} file paths 
(relative to repo root). If the already-fetched files are sufficient, return an empty list.

Respond ONLY with a JSON object in this format:
{{"additional_files": ["path/to/file.py", ...]}}
If no more files are needed: {{"additional_files": []}}
"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{MCP_URL}/identify-files",
                json={"prompt": prompt, "project_id": GCP_PROJECT_ID},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("additional_files", [])
            # Fall through to direct Vertex call if MCP endpoint not found
    except Exception:
        pass

    # Fallback: call Vertex AI directly from the API server
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel
        vertexai.init(project=GCP_PROJECT_ID, location=VERTEX_AI_LOCATION)
        model = GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.1,
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "OBJECT",
                    "properties": {
                        "additional_files": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"}
                        }
                    },
                    "required": ["additional_files"]
                }
            }
        )
        result = json.loads(response.text)
        return result.get("additional_files", [])
    except Exception as e:
        logger.warning("Failed to identify additional files via Vertex AI: %s", e)
        return []

@app.post("/api/webhooks/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Legacy GitHub push webhook (unauthenticated). Kept for backward compatibility."""
    payload = await request.json()

    if "commits" not in payload:
        return {"status": "ignored", "reason": "not a push event"}

    repo_url = payload.get("repository", {}).get("clone_url")
    if not repo_url:
        return {"status": "ignored", "reason": "no repository url"}

    db_repo = find_repository_by_url(db, repo_url)
    if not db_repo:
        return {"status": "ignored", "reason": "project not found"}

    target_project = db_repo.project
    if not target_project:
        return {"status": "ignored", "reason": "project not found"}

    target_project.status = "analyzing"
    db.commit()

    urls = [r.url for r in target_project.repositories]
    background_tasks.add_task(
        run_analysis_task,
        target_project.id,
        urls,
        target_project.github_installation_id,
    )

    return {"status": "re-analyzing", "project_id": target_project.id}


@app.post("/api/webhooks/github-app")
async def github_app_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Receives GitHub App events (installation, push, etc.).
    Verifies the X-Hub-Signature-256 HMAC before processing.
    """
    body = await request.body()
    event_type = request.headers.get("X-GitHub-Event", "")
    signature = request.headers.get("X-Hub-Signature-256", "")

    # Verify HMAC signature
    if GITHUB_APP_WEBHOOK_SECRET:
        expected_digest = hmac.new(
            GITHUB_APP_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        expected = "sha256=" + expected_digest
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")


    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info("GitHub App webhook received: event=%s", event_type)

    # --- Installation event: store installation_id ---
    if event_type == "installation":
        action = payload.get("action", "")
        installation_id = str(payload.get("installation", {}).get("id", ""))
        sender_login = payload.get("sender", {}).get("login", "")

        logger.info(
            "GitHub App installation event: action=%s installation_id=%s sender=%s",
            action, installation_id, sender_login,
        )
        # installation_id is stored by the frontend flow via /api/github-app/save-installation
        # This webhook is logged for audit purposes
        return {"status": "acknowledged", "event": event_type, "action": action}

    # --- Push event: branch filtering + has_update flag + delivery logging ---
    if event_type == "push":
        installation_id = str(payload.get("installation", {}).get("id", ""))
        repo_url = payload.get("repository", {}).get("clone_url", "")
        ref = payload.get("ref", "")  # e.g. "refs/heads/main"
        pushed_branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref
        commit_sha = payload.get("after", None)  # HEAD commit SHA after push

        logger.info(
            "Push event received: repo_url=%s, ref=%s, commit_sha=%s, installation_id=%s",
            repo_url, ref, commit_sha, installation_id
        )

        if not repo_url:
            logger.warning("Push event ignored: repo_url is empty")
            return {"status": "ignored", "reason": "no repository url"}

        db_repo = find_repository_by_url(db, repo_url)
        if not db_repo:
            logger.warning("Push event ignored: no tracked repository found in DB for URL '%s'", repo_url)
            return {"status": "ignored", "reason": "repository not tracked"}

        target_project = db_repo.project
        if not target_project:
            logger.warning("Push event ignored: project not found for repository ID %s", db_repo.id)
            return {"status": "ignored", "reason": "project not found"}

        # Update installation_id if we now have it from the push event
        if installation_id and not target_project.github_installation_id:
            target_project.github_installation_id = installation_id

        # Branch filtering: only act if webhook is enabled and branch matches
        matched = (
            db_repo.webhook_enabled
            and bool(db_repo.watch_branch)
            and pushed_branch == db_repo.watch_branch
        )

        # Always record the delivery for audit/news-feed purposes
        delivery = models.WebhookDelivery(
            repository_id=db_repo.id,
            project_id=target_project.id,
            branch=pushed_branch or "unknown",
            commit_sha=commit_sha,
            matched=matched,
        )
        db.add(delivery)

        if matched:
            target_project.has_update = True
            db.commit()
            logger.info(
                "push matched watch_branch '%s' for project %s – has_update set to True",
                pushed_branch, target_project.id,
            )
            return {"status": "update_flagged", "project_id": target_project.id}
        else:
            db.commit()
            reason = "webhook_disabled" if not db_repo.webhook_enabled else f"branch '{pushed_branch}' != watch_branch '{db_repo.watch_branch}'"
            logger.info("push ignored for project %s: %s", target_project.id, reason)
            return {"status": "ignored", "reason": reason}

    return {"status": "ignored", "event": event_type}


@app.get("/api/webhook-deliveries")
def list_webhook_deliveries(
    db: Session = Depends(get_db),
    user: dict = Depends(verify_token),
):
    """
    Returns the latest 20 matched push event deliveries for the logged-in user's projects.
    Used by the dashboard news-feed component.
    """
    # Get all project IDs for this user
    user_projects = db.query(models.Project.id).filter(
        models.Project.user_id == user["uid"]
    ).all()
    project_ids = [p.id for p in user_projects]

    if not project_ids:
        return []

    deliveries = (
        db.query(models.WebhookDelivery)
        .filter(
            models.WebhookDelivery.project_id.in_(project_ids),
            models.WebhookDelivery.matched == True,
        )
        .order_by(models.WebhookDelivery.received_at.desc())
        .limit(20)
        .all()
    )

    result = []
    for d in deliveries:
        repo_url = d.repository.url if d.repository else None
        result.append({
            "id": d.id,
            "repository_url": repo_url,
            "project_id": d.project_id,
            "branch": d.branch,
            "commit_sha": d.commit_sha,
            "received_at": d.received_at.isoformat() if d.received_at else None,
        })
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
