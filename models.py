from sqlalchemy import Column, String, Float, ForeignKey, DateTime, Integer
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime

from database import Base

def generate_uuid():
    return str(uuid.uuid4())

class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    name = Column(String, nullable=True)
    status = Column(String, default="analyzing") # analyzing, ready, error
    github_installation_id = Column(String, nullable=True)  # GitHub App installation ID (non-sensitive)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="projects")
    repositories = relationship("Repository", back_populates="project", cascade="all, delete-orphan")
    microservices = relationship("Microservice", back_populates="project", cascade="all, delete-orphan")
    dependencies = relationship("Dependency", back_populates="project", cascade="all, delete-orphan")

class Repository(Base):
    __tablename__ = "repositories"

    id = Column(String, primary_key=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.id"))
    url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="repositories")
    microservices = relationship("Microservice", back_populates="repository")

class Microservice(Base):
    __tablename__ = "microservices"

    id = Column(String, primary_key=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.id"))
    repository_id = Column(String, ForeignKey("repositories.id"), nullable=True)
    ms_id = Column(String) # The id returned from MCP (e.g. frontend-web)
    name = Column(String)
    description = Column(String)
    ai_prompt_context = Column(String)
    avatar_visual_prompt = Column(String)
    avatar_image_url = Column(String)
    position_x = Column(Float, default=0.0)
    position_y = Column(Float, default=0.0)
    scale_tier = Column(Integer, default=3, nullable=False)
    # JSON string: [{"path": str, "perspective": str, "reason": str}]
    # Populated during analysis (Phase 2), used for chat-time code retrieval
    key_files = Column(String, nullable=True)

    project = relationship("Project", back_populates="microservices")
    repository = relationship("Repository", back_populates="microservices")
    chat_histories = relationship("ChatHistory", back_populates="microservice", cascade="all, delete-orphan")

class Dependency(Base):
    __tablename__ = "dependencies"

    id = Column(String, primary_key=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.id"))
    dep_id = Column(String) # The id returned from MCP
    source_service_id = Column(String, ForeignKey("microservices.id"))
    target_service_id = Column(String, ForeignKey("microservices.id"))
    relationship_type = Column(String)

    project = relationship("Project", back_populates="dependencies")

class ChatHistory(Base):
    __tablename__ = "chat_histories"
    
    id = Column(String, primary_key=True, default=generate_uuid)
    microservice_id = Column(String, ForeignKey("microservices.id"))
    messages = Column(String, default="[]") # JSON stringified list of messages
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    microservice = relationship("Microservice", back_populates="chat_histories")

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True) # Firebase UID
    email = Column(String)
    display_name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    projects = relationship("Project", back_populates="user", cascade="all, delete-orphan")
