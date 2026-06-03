from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
import httpx
import os

from database import engine, Base, get_db
import models

# Create tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Architecture World API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8001")

class AnalyzeRequest(BaseModel):
    repo_paths: str # comma separated paths

async def run_analysis_task(project_id: str, repo_paths: list[str]):
    from database import SessionLocal
    db = SessionLocal()
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(f"{MCP_URL}/analyze", json={"repo_paths": repo_paths})
            response.raise_for_status()
            data = response.json()
            
            # Save to DB
            project = db.query(models.Project).filter(models.Project.id == project_id).first()
            if not project:
                return

            microservices = data.get("microservices", [])
            
            # 1. Insert microservices and build name -> id map
            name_to_id = {}
            for ms in microservices:
                # Use ms_id if provided, else generate one via DB or just use the name as fallback
                db_ms = models.Microservice(
                    project_id=project.id,
                    ms_id=ms.get("id"),
                    name=ms.get("name"),
                    description=ms.get("description"),
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
            
    except Exception as e:
        print(f"Analysis failed: {e}")
        project = db.query(models.Project).filter(models.Project.id == project_id).first()
        if project:
            project.status = "error"
            db.commit()
    finally:
        db.close()

@app.post("/api/projects/analyze")
async def start_analysis(req: AnalyzeRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    paths = [p.strip() for p in req.repo_paths.split(",") if p.strip()]
    if not paths:
        raise HTTPException(status_code=400, detail="No paths provided")
        
    project = models.Project(
        repo_paths=",".join(paths),
        status="analyzing"
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    
    background_tasks.add_task(run_analysis_task, project.id, paths)
    
    return {"project_id": project.id, "status": project.status}

@app.get("/api/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    if project.status != "ready":
        return {"id": project.id, "status": project.status}
        
    microservices = [
        {
            "id": ms.ms_id or ms.id,
            "name": ms.name,
            "description": ms.description,
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
        "status": project.status,
        "microservices": microservices,
        "dependencies": dependencies
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
