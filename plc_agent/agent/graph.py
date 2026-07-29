"""
PLC Agent - LangGraph Agent Definition

This module defines the core Agent using LangGraph's ReAct pattern.
The agent receives user messages, decides which tools to call,
executes them, and generates a final response.

Multi-turn conversation memory is handled via LangGraph's checkpointer:
- Each conversation is identified by a thread_id
- The checkpointer persists all messages within a thread
- When the same thread_id is used, the agent has access to full conversation history

Multi-program support:
- create_plc_agent() accepts a program_key to bind the agent to a specific KB
- The program_key determines which tools and system prompt are used
- Program switching is driven by UI/API (user selects production line)
"""
from typing import Optional

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.sqlite import SqliteSaver

from plc_agent.config import OPENAI_API_BASE, OPENAI_API_KEY, MODEL_NAME, PROJECT_ROOT, PROGRAM_REGISTRY, DEFAULT_PROGRAM_KEY
from plc_agent.tools.plc_tools import get_tools_for_type
from plc_agent.agent.prompts import get_system_prompt
from plc_agent.knowledge.loader import set_active_kb


# Global checkpointer instance - persists conversation state to SQLite
# The database file is stored at project_root/data/conversations.db
_DB_DIR = PROJECT_ROOT / "data"
_DB_DIR.mkdir(exist_ok=True)
_DB_PATH = str(_DB_DIR / "conversations.db")

# SqliteSaver uses a connection object
import sqlite3
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
_checkpointer = SqliteSaver(_conn)


def create_plc_agent(
    program_key: Optional[str] = None,
    model_name: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 1.0,
    checkpointer=None,
):
    """
    Create and return a LangGraph ReAct agent configured for PLC analysis.
    
    The agent is bound to a specific production line/program via program_key,
    which determines:
    - Which knowledge base is loaded (alarm program vs control program)
    - Which tools are available to the LLM
    - What system prompt is used
    
    Args:
        program_key: Production line identifier (e.g. "WH202_CG1", "WH201_CG1").
                    If None, uses DEFAULT_PROGRAM_KEY from config.
        model_name: LLM model to use (default from config)
        api_base: API base URL (default from config)
        api_key: API key (default from config)
        temperature: LLM temperature. Note: some models on Corning platform
                     only support temperature=1 (default).
        checkpointer: LangGraph checkpointer for conversation memory.
                      If None, uses the global SqliteSaver instance.
    
    Returns:
        A compiled LangGraph agent ready for invocation with memory.
    """
    # Determine program key and type
    key = program_key or DEFAULT_PROGRAM_KEY
    if key not in PROGRAM_REGISTRY:
        raise ValueError(f"Unknown program_key '{key}'. Available: {list(PROGRAM_REGISTRY.keys())}")
    
    kb_type = PROGRAM_REGISTRY[key]["type"]
    
    # Activate the corresponding knowledge base
    set_active_kb(key)
    
    # Get type-specific tools and prompt
    tools = get_tools_for_type(kb_type)
    prompt = get_system_prompt(kb_type)
    
    # Initialize LLM
    llm = ChatOpenAI(
        model=model_name or MODEL_NAME,
        base_url=api_base or OPENAI_API_BASE,
        api_key=api_key or OPENAI_API_KEY,
        temperature=temperature,
    )
    
    # Use provided checkpointer or global default
    cp = checkpointer if checkpointer is not None else _checkpointer
    
    # Create ReAct agent with tools and memory
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=prompt,
        checkpointer=cp,
    )
    
    return agent


def chat_with_agent(agent, user_message: str, thread_id: str = "default") -> str:
    """
    Send a message to the agent and get a response.
    The agent remembers all previous messages within the same thread_id.
    
    Includes query-level caching: if the same question (normalized) has been
    asked before, returns the cached response immediately without calling LLM.
    Note: Only first-turn questions are cached (not context-dependent follow-ups).
    
    Args:
        agent: The compiled LangGraph agent
        user_message: User's input message
        thread_id: Conversation thread ID for memory.
                   Same thread_id = same conversation context.
                   Different thread_id = independent conversation.
    
    Returns:
        Agent's text response
    """
    from plc_agent.knowledge.cache import get_query_cache
    
    query_cache = get_query_cache()
    
    # Check if this is a first-turn query (no prior context in this thread)
    # Only cache first-turn queries since follow-ups depend on context
    config = {"configurable": {"thread_id": thread_id}}
    
    is_first_turn = True
    try:
        state = agent.get_state(config)
        existing_messages = state.values.get("messages", [])
        is_first_turn = len(existing_messages) == 0
    except Exception:
        is_first_turn = True
    
    # Try query cache for first-turn, context-independent queries
    if is_first_turn:
        cached_response = query_cache.get(user_message)
        if cached_response is not None:
            return cached_response
    
    # No cache hit - invoke the agent normally
    result = agent.invoke(
        {"messages": [("user", user_message)]},
        config=config,
    )
    
    # Extract the final AI message
    messages = result["messages"]
    for msg in reversed(messages):
        # Find the last AIMessage that has content but no active tool_calls
        if (hasattr(msg, "content") and msg.content 
            and hasattr(msg, "type") and msg.type == "ai"
            and not getattr(msg, "tool_calls", None)):
            # Cache the response for future identical queries
            if is_first_turn:
                query_cache.put(user_message, msg.content)
            return msg.content
    
    return "抱歉，我无法生成回答。请重新提问。"


def get_conversation_history(agent, thread_id: str) -> list[dict]:
    """
    Retrieve the conversation history for a given thread.
    
    Args:
        agent: The compiled LangGraph agent
        thread_id: Conversation thread ID
    
    Returns:
        List of message dicts with 'role' and 'content' keys
    """
    config = {"configurable": {"thread_id": thread_id}}
    
    try:
        state = agent.get_state(config)
        messages = state.values.get("messages", [])
        
        history = []
        for msg in messages:
            if hasattr(msg, "type") and hasattr(msg, "content") and msg.content:
                if msg.type == "human":
                    history.append({"role": "user", "content": msg.content})
                elif msg.type == "ai" and not getattr(msg, "tool_calls", None):
                    history.append({"role": "assistant", "content": msg.content})
        return history
    except Exception:
        return []


def clear_conversation(agent, thread_id: str) -> bool:
    """
    Clear the conversation history for a given thread.
    Creates a fresh state for the thread.
    
    Args:
        agent: The compiled LangGraph agent  
        thread_id: Conversation thread ID to clear
    
    Returns:
        True if cleared successfully
    """
    # For SqliteSaver, the simplest approach is to use a new thread_id
    # This is handled at the UI level by generating a new session_id
    return True


def stream_chat_with_agent(agent, user_message: str, thread_id: str = "default"):
    """
    Stream a response from the agent (yields chunks).
    Also persists conversation memory via checkpointer.
    
    Args:
        agent: The compiled LangGraph agent
        user_message: User's input message
        thread_id: Conversation thread ID
    
    Yields:
        Response text chunks as they become available
    """
    config = {"configurable": {"thread_id": thread_id}}
    
    for event in agent.stream(
        {"messages": [("user", user_message)]},
        config=config,
        stream_mode="messages",
    ):
        # event is typically a tuple of (message, metadata)
        # but some events may have different formats
        if not isinstance(event, tuple) or len(event) < 2:
            continue
        msg, metadata = event
        if hasattr(msg, "content") and msg.content:
            # Only yield AI response chunks, not tool calls/results
            if metadata.get("langgraph_node") == "agent":
                yield msg.content


# ================================================================
# CLI Interactive Mode
# ================================================================

def run_cli():
    """Run the agent in interactive CLI mode for testing."""
    import sys
    
    print("=" * 60)
    print("  PLC Intelligent Diagnostic Agent")
    print("  Multi-turn conversation enabled (context is remembered)")
    print("")
    print("  Commands:")
    print("    quit/exit/q  - Exit")
    print("    /new         - Start a new conversation (clear memory)")
    print("    /history     - Show conversation history")
    print("    /switch      - Switch production line")
    print("=" * 60)
    print()
    
    # Validate config before starting
    from plc_agent.config import validate_config
    try:
        validate_config()
    except ValueError as e:
        print(f"[CONFIG ERROR] {e}")
        sys.exit(1)
    
    # Let user select program
    print("Available production lines:")
    program_keys = list(PROGRAM_REGISTRY.keys())
    for i, key in enumerate(program_keys):
        cfg = PROGRAM_REGISTRY[key]
        print(f"  {i+1}. [{key}] {cfg['name']} (type={cfg['type']})")
    
    print(f"\nDefault: {DEFAULT_PROGRAM_KEY}")
    choice = input("Select (number or Enter for default): ").strip()
    
    if choice and choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(program_keys):
            selected_key = program_keys[idx]
        else:
            selected_key = DEFAULT_PROGRAM_KEY
    else:
        selected_key = DEFAULT_PROGRAM_KEY
    
    print(f"\nLoading knowledge base for {selected_key}...")
    agent = create_plc_agent(program_key=selected_key)
    print(f"Agent ready! (Program: {PROGRAM_REGISTRY[selected_key]['name']})\n")
    
    thread_id = "cli-session-1"
    session_count = 1
    
    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break
        
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break
        
        # Handle special commands
        if user_input.lower() == "/new":
            session_count += 1
            thread_id = f"cli-session-{session_count}"
            print("[System] New conversation started. Previous context cleared.\n")
            continue
        
        if user_input.lower() == "/history":
            history = get_conversation_history(agent, thread_id)
            if history:
                print(f"\n[History - {len(history)} messages in thread '{thread_id}']")
                for h in history:
                    role = "You" if h["role"] == "user" else "Agent"
                    content_preview = h["content"][:100] + "..." if len(h["content"]) > 100 else h["content"]
                    print(f"  [{role}]: {content_preview}")
                print()
            else:
                print("[System] No conversation history yet.\n")
            continue
        
        if user_input.lower() == "/switch":
            print("\nAvailable production lines:")
            for i, key in enumerate(program_keys):
                cfg = PROGRAM_REGISTRY[key]
                marker = " (current)" if key == selected_key else ""
                print(f"  {i+1}. [{key}] {cfg['name']}{marker}")
            choice = input("Select: ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(program_keys):
                    selected_key = program_keys[idx]
                    print(f"\nSwitching to {selected_key}...")
                    agent = create_plc_agent(program_key=selected_key)
                    session_count += 1
                    thread_id = f"cli-session-{session_count}"
                    print(f"Switched! New session started. (Program: {PROGRAM_REGISTRY[selected_key]['name']})\n")
            continue
        
        print("\nAgent: ", end="", flush=True)
        try:
            response = chat_with_agent(agent, user_input, thread_id)
            print(response)
        except Exception as e:
            print(f"\n[ERROR] {type(e).__name__}: {e}")
        print()


if __name__ == "__main__":
    run_cli()
