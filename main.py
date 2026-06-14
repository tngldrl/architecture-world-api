from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
import httpx
import os
import json
from typing import Optional

from database import engine, Base, get_db
import models
from auth import verify_token

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

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8001")
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

class AnalyzeRequest(BaseModel):
    repo_urls: list[str]
    project_name: Optional[str] = None

async def run_analysis_task(project_id: str, repo_urls: list[str]):
    callback_url = f"{API_BASE_URL}/api/projects/{project_id}/callback"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{MCP_URL}/analyze", 
                json={
                    "repo_urls": repo_urls,
                    "project_id": project_id,
                    "callback_url": callback_url
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
                db_repo = db.query(models.Repository).filter(
                    models.Repository.project_id == project.id,
                    models.Repository.url == repo_url
                ).first()
            
            if not db_repo:
                # Fallback: project's first repository
                db_repo = db.query(models.Repository).filter(
                    models.Repository.project_id == project.id
                ).first()
            
            repository_id = db_repo.id if db_repo else None

            # Use ms_id if provided, else generate one via DB or just use the name as fallback
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
                position_y=ms.get("position", {}).get("y", 0.0)
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
    urls = [u.strip() for u in req.repo_urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs provided")
        
    project = models.Project(
        status="analyzing",
        name=req.project_name,
        user_id=user["uid"]
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    
    # Save repositories to DB
    for url in urls:
        db_repo = models.Repository(
            project_id=project.id,
            url=url
        )
        db.add(db_repo)
    db.commit()
    
    background_tasks.add_task(run_analysis_task, project.id, urls)
    
    return {"project_id": project.id, "status": project.status}

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
            "position": {"x": ms.position_x, "y": ms.position_y}
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
    
    # Call MCP
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{MCP_URL}/chat", json={
                "system_prompt": ms.ai_prompt_context or "You are a helpful assistant.",
                "history": history,
                "new_message": req.message
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

@app.post("/api/webhooks/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    # This endpoint receives GitHub push events
    # In a real app, we would verify the X-Hub-Signature here
    payload = await request.json()
    
    # We only care about push events
    if "commits" not in payload:
        return {"status": "ignored", "reason": "not a push event"}
        
    repo_url = payload.get("repository", {}).get("clone_url")
    if not repo_url:
        return {"status": "ignored", "reason": "no repository url"}
        
    # Find a repository matching the clone_url
    db_repo = db.query(models.Repository).filter(models.Repository.url == repo_url).first()
    if not db_repo:
        return {"status": "ignored", "reason": "project not found"}
        
    target_project = db_repo.project
    if not target_project:
        return {"status": "ignored", "reason": "project not found"}
        
    # Update status to analyzing
    target_project.status = "analyzing"
    db.commit()
    
    # Trigger re-analysis
    urls = [r.url for r in target_project.repositories]
    background_tasks.add_task(run_analysis_task, target_project.id, urls)
    
    return {"status": "re-analyzing", "project_id": target_project.id}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
