"""
PLC Agent - Unified Entry Point

Usage:
    python run.py cli          # Interactive CLI mode
    python run.py api          # Start FastAPI server
    python run.py ui           # Start Gradio UI
    python run.py test         # Test knowledge base loading
"""
import sys
import os

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    if len(sys.argv) < 2:
        print_usage()
        return
    
    command = sys.argv[1].lower()
    
    if command == "cli":
        from plc_agent.agent.graph import run_cli
        run_cli()
    
    elif command == "api":
        from plc_agent.api.server import start_server
        start_server()
    
    elif command == "ui":
        from plc_agent.ui.app import launch_ui
        share = "--share" in sys.argv
        launch_ui(share=share)
    
    elif command == "test":
        run_test()
    
    elif command in ("clear-cache", "clearcache"):
        clear_cache()
    
    else:
        print(f"Unknown command: {command}")
        print_usage()


def print_usage():
    print("""
PLC Intelligent Diagnostic Agent
=================================

Usage:
    python run.py <command> [options]

Commands:
    cli     Start interactive CLI chat mode
    api     Start FastAPI REST API server (default port 8000)
    ui      Start Gradio web chat interface (default port 7860)
    test    Test knowledge base loading and config
    clear-cache  Clear all cached data (cache + conversation history)

Options:
    --share   (ui only) Create a public Gradio share link

Examples:
    python run.py cli              # Chat in terminal
    python run.py api              # Start API at http://localhost:8000
    python run.py ui               # Start UI at http://localhost:7860
    python run.py ui --share       # Start UI with public share link
""")


def run_test():
    """Test that everything loads correctly."""
    print("=" * 50)
    print("PLC Agent - System Test")
    print("=" * 50)
    
    # Test 1: Config
    print("\n[1/4] Testing configuration...")
    try:
        from plc_agent.config import (
            OPENAI_API_BASE, OPENAI_API_KEY, MODEL_NAME, KNOWLEDGE_BASE_PATH
        )
        print(f"  API Base:  {OPENAI_API_BASE}")
        print(f"  Model:     {MODEL_NAME}")
        print(f"  API Key:   {'***' + OPENAI_API_KEY[-4:] if OPENAI_API_KEY else 'NOT SET'}")
        print(f"  KB Path:   {KNOWLEDGE_BASE_PATH}")
        print(f"  KB Exists: {KNOWLEDGE_BASE_PATH.exists()}")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAILED] {e}")
        return
    
    # Test 2: Knowledge base
    print("\n[2/4] Testing knowledge base loading...")
    try:
        from plc_agent.knowledge.loader import PLCKnowledgeBase
        kb = PLCKnowledgeBase(KNOWLEDGE_BASE_PATH)
        print(f"  Alarms:   {len(kb.alarm_traces)}")
        print(f"  Rules:    {len(kb.rules)}")
        print(f"  Edges:    {len(kb.edges)}")
        print(f"  Devices:  {len(kb.devices)}")
        print(f"  Sections: {len(kb.sections)}")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAILED] {e}")
        return
    
    # Test 3: Query functions
    print("\n[3/4] Testing knowledge base queries...")
    try:
        trace = kb.get_alarm_trace("F1")
        assert trace is not None, "F1 trace should exist"
        assert "set_causes" in trace, "trace should have set_causes"
        
        alarms = kb.list_alarms("SERVO")
        assert len(alarms) > 0, "SERVO section should have alarms"
        
        device = kb.get_device_info("M7")
        assert device is not None, "M7 should exist"
        
        print("  trace_alarm(F1):     OK")
        print("  list_alarms(SERVO):  OK")
        print("  query_device(M7):    OK")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAILED] {e}")
        return
    
    # Test 4: Tool imports
    print("\n[4/4] Testing tool imports...")
    try:
        from plc_agent.tools.plc_tools import ALL_TOOLS
        print(f"  Tools loaded: {len(ALL_TOOLS)}")
        for tool in ALL_TOOLS:
            print(f"    - {tool.name}: {tool.description[:60]}...")
        print("  [OK]")
    except Exception as e:
        print(f"  [FAILED] {e}")
        return
    
    print("\n" + "=" * 50)
    print("ALL TESTS PASSED!")
    print("=" * 50)
    print("\nNext steps:")
    print("  1. Ensure .env is configured with your API key")
    print("  2. Run: python test_api_connection.py  (verify LLM API)")
    print("  3. Run: python run.py cli              (start chatting!)")


def clear_cache():
    """Clear all persistent cache and conversation data."""
    import shutil
    from pathlib import Path
    
    data_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "data"
    
    if not data_dir.exists():
        print("No data directory found. Nothing to clear.")
        return
    
    print(f"Clearing all data in: {data_dir}")
    try:
        shutil.rmtree(data_dir)
        print("[OK] All cache and conversation history cleared.")
        print("     The data/ directory will be recreated on next startup.")
    except Exception as e:
        print(f"[ERROR] Could not delete: {e}")
        print("  Make sure no other instance (UI/API) is running.")
        print("  Close all instances first, then try again.")


if __name__ == "__main__":
    main()
