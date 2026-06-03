from sqlalchemy import Column, String, Float, ForeignKey, DateTime
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime

from database import Base

def generate_uuid():
    return str(uuid.uuid4())

class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, default=generate_uuid)
    repo_paths = Column(String) # Comma separated list of paths
    status = Column(String, default="analyzing") # analyzing, ready, error
    created_at = Column(DateTime, default=datetime.utcnow)

    microservices = relationship("Microservice", back_populates="project", cascade="all, delete-orphan")
    dependencies = relationship("Dependency", back_populates="project", cascade="all, delete-orphan")

class Microservice(Base):
    __tablename__ = "microservices"

    id = Column(String, primary_key=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.id"))
    ms_id = Column(String) # The id returned from MCP (e.g. frontend-web)
    name = Column(String)
    description = Column(String)
    avatar_visual_prompt = Column(String)
    avatar_image_url = Column(String)
    position_x = Column(Float, default=0.0)
    position_y = Column(Float, default=0.0)

    project = relationship("Project", back_populates="microservices")

class Dependency(Base):
    __tablename__ = "dependencies"

    id = Column(String, primary_key=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.id"))
    dep_id = Column(String) # The id returned from MCP
    source_service_id = Column(String, ForeignKey("microservices.id"))
    target_service_id = Column(String, ForeignKey("microservices.id"))
    relationship_type = Column(String)

    project = relationship("Project", back_populates="dependencies")
