from sqlalchemy import Column, String, Float, ForeignKey, DateTime, Integer, Boolean
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
    has_update = Column(Boolean, default=False, nullable=False)  # True when a tracked repo received a push
    is_demo = Column(Boolean, default=False, nullable=False)
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
    webhook_enabled = Column(Boolean, default=False, nullable=False)  # Enable push notification for this repo
    watch_branch = Column(String, nullable=True)  # Branch to monitor (e.g. "main")
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="repositories")
    microservices = relationship("Microservice", back_populates="repository")
    webhook_deliveries = relationship("WebhookDelivery", back_populates="repository", cascade="all, delete-orphan")

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
    avatar_chat_visual_prompt = Column(String, nullable=True)
    avatar_chat_image_url = Column(String, nullable=True)
    position_x = Column(Float, default=0.0)
    position_y = Column(Float, default=0.0)
    scale_tier = Column(Integer, default=3, nullable=False)
    # JSON string: [{"path": str, "perspective": str, "reason": str}]
    # Populated during analysis (Phase 2), used for chat-time code retrieval
    key_files = Column(String, nullable=True)
    # JSON string list: ["PostgreSQL", "FastAPI", "Python"]
    # Populated during analysis, displayed as tags in chat details drawer
    technologies = Column(String, nullable=True)

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
    github_username = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    projects = relationship("Project", back_populates="user", cascade="all, delete-orphan")


class WebhookDelivery(Base):
    """Records each GitHub push event received for a tracked repository."""
    __tablename__ = "webhook_deliveries"

    id = Column(String, primary_key=True, default=generate_uuid)
    repository_id = Column(String, ForeignKey("repositories.id"), nullable=False)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    branch = Column(String, nullable=False)  # Branch name without refs/heads/ prefix
    commit_sha = Column(String, nullable=True)  # HEAD commit SHA of the push
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    matched = Column(Boolean, default=False, nullable=False)  # True if branch matched watch_branch

    repository = relationship("Repository", back_populates="webhook_deliveries")
