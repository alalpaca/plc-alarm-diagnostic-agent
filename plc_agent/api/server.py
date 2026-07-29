"""
PLC Agent - FastAPI Backend

Provides REST API endpoints for the PLC diagnostic agent.
Supports multi-program knowledge base selection via program_key parameter.
"""
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from plc_agent.agent.graph import create_plc_agent, chat_with_agent, get_conversation_history
from plc_agent.knowledge.loader import get_knowledge_base, list_available_programs
from plc_agent.config import PROGRAM_REGISTRY, DEFAULT_PROGRAM_KEY


# Global agent instances (keyed by program_key)
_agents: dict[str, object] = {}


def get_or_create_agent(program_key: str):
    """Get existing agent or create a new one for the given program."""
    if program_key not in _agents:
        _agents[program_key] = create_plc_agent(program_key=program_key)
    return _agents[program_key]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize default agent on startup."""
    print("Initializing PLC Agent...")
    
    # Pre-load default knowledge base and agent
    get_knowledge_base(DEFAULT_PROGRAM_KEY)
    get_or_create_agent(DEFAULT_PROGRAM_KEY)
    print(f"PLC Agent ready! (Default: {DEFAULT_PROGRAM_KEY})")
    
    yield
    
    # Cleanup on shutdown
    print("Shutting down PLC Agent...")


app = FastAPI(
    title="PLC Diagnostic Agent API",
    description="Intelligent PLC program analysis agent powered by LangGraph. "
                "Supports multiple production lines via program_key parameter.",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================
# Request/Response Models
# ================================================================

class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = "default"
    program_key: str = DEFAULT_PROGRAM_KEY


class ChatResponse(BaseModel):
    response: str
    thread_id: str
    program_key: str


class HistoryResponse(BaseModel):
    thread_id: str
    messages: list[dict]


class HealthResponse(BaseModel):
    status: str
    available_programs: dict
    default_program: str


class ProgramInfo(BaseModel):
    key: str
    name: str
    type: str
    stats: dict


# ================================================================
# Endpoints
# ================================================================

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Check if the service is running and list available programs."""
    try:
        programs = list_available_programs()
        return HealthResponse(
            status="healthy",
            available_programs=programs,
            default_program=DEFAULT_PROGRAM_KEY,
        )
    except Exception as e:
        return HealthResponse(
            status=f"degraded: {str(e)}",
            available_programs={},
            default_program=DEFAULT_PROGRAM_KEY,
        )


@app.get("/programs")
async def get_programs():
    """List all available production line programs with their stats."""
    result = []
    for key, cfg in PROGRAM_REGISTRY.items():
        try:
            kb = get_knowledge_base(key)
            stats = kb.get_summary()
            result.append({
                "key": key,
                "name": cfg["name"],
                "type": cfg["type"],
                "stats": {
                    "rules": stats["loaded"]["rules"],
                    "devices": stats["loaded"]["devices"],
                    "sections": stats["loaded"]["sections"],
                    "alarm_traces": stats["loaded"]["alarm_traces"],
                },
            })
        except Exception as e:
            result.append({
                "key": key,
                "name": cfg["name"],
                "type": cfg["type"],
                "stats": {"error": str(e)},
            })
    return result


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a message to the PLC diagnostic agent for a specific program."""
    # Validate program_key
    if request.program_key not in PROGRAM_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown program_key '{request.program_key}'. "
                   f"Available: {list(PROGRAM_REGISTRY.keys())}"
        )
    
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    
    try:
        agent = get_or_create_agent(request.program_key)
        
        # Use program_key as prefix in thread_id to isolate conversations
        thread_id = f"{request.program_key}:{request.thread_id or 'default'}"
        
        response = chat_with_agent(agent, request.message, thread_id=thread_id)
        return ChatResponse(
            response=response,
            thread_id=request.thread_id or "default",
            program_key=request.program_key,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")


@app.get("/chat/history", response_model=HistoryResponse)
async def chat_history(thread_id: str = "default", program_key: str = DEFAULT_PROGRAM_KEY):
    """Get the conversation history for a given thread."""
    if program_key not in PROGRAM_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown program_key '{program_key}'")
    
    agent = get_or_create_agent(program_key)
    full_thread_id = f"{program_key}:{thread_id}"
    history = get_conversation_history(agent, full_thread_id)
    return HistoryResponse(thread_id=thread_id, messages=history)


@app.get("/alarms")
async def get_alarms(section: Optional[str] = None, program_key: str = DEFAULT_PROGRAM_KEY):
    """Get list of alarms, optionally filtered by section."""
    kb = get_knowledge_base(program_key)
    return kb.list_alarms(section)


@app.get("/alarms/{device}")
async def get_alarm_trace(device: str, max_depth: Optional[int] = None, program_key: str = DEFAULT_PROGRAM_KEY):
    """Get the backward trace for a specific alarm."""
    kb = get_knowledge_base(program_key)
    trace = kb.get_alarm_trace(device, max_depth=max_depth)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Alarm {device} not found")
    return trace


@app.get("/devices/{device}")
async def get_device(device: str, program_key: str = DEFAULT_PROGRAM_KEY):
    """Get information about a specific device."""
    kb = get_knowledge_base(program_key)
    info = kb.get_device_info(device)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Device {device} not found")
    return info


@app.get("/sections")
async def get_sections(program_key: str = DEFAULT_PROGRAM_KEY):
    """Get all program sections overview."""
    kb = get_knowledge_base(program_key)
    return kb.get_sections_overview()


@app.get("/cache/stats")
async def cache_stats():
    """Get cache statistics (hit rate, size, etc.)."""
    from plc_agent.knowledge.cache import get_all_cache_stats
    return get_all_cache_stats()


@app.post("/cache/clear")
async def cache_clear():
    """Clear all caches."""
    from plc_agent.knowledge.cache import clear_all_caches
    clear_all_caches()
    return {"status": "cleared"}


def start_server():
    """Start the FastAPI server."""
    import uvicorn
    from plc_agent.config import API_HOST, API_PORT
    
    uvicorn.run(
        "plc_agent.api.server:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
    )


if __name__ == "__main__":
    start_server()
