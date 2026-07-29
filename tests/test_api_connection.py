"""
API Connection Test Script

Run this script to verify that your Corning AI Platform Gateway
is accessible and the OpenAI-compatible API works correctly.

Usage:
    python test_api_connection.py

Before running:
    1. Copy .env.example to .env
    2. Fill in your actual OPENAI_API_KEY
    3. Adjust MODEL_NAME if needed
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plc_agent.config import OPENAI_API_BASE, OPENAI_API_KEY, MODEL_NAME


def test_with_openai_sdk():
    """Test using the official openai SDK (most common approach)."""
    print("=" * 60)
    print("Test 1: OpenAI SDK Compatibility")
    print("=" * 60)
    
    try:
        from openai import OpenAI
    except ImportError:
        print("[ERROR] openai package not installed. Run: pip install openai")
        return False
    
    print(f"  Base URL: {OPENAI_API_BASE}")
    print(f"  Model:    {MODEL_NAME}")
    print(f"  API Key:  {'***' + OPENAI_API_KEY[-4:] if OPENAI_API_KEY else 'NOT SET'}")
    print()
    
    if not OPENAI_API_KEY:
        print("[ERROR] OPENAI_API_KEY not set. Please configure .env file.")
        return False
    
    # Try different base URL patterns
    # Some gateways need /v1 appended, some don't
    base_urls_to_try = [
        OPENAI_API_BASE,
        f"{OPENAI_API_BASE}/v1",
        f"{OPENAI_API_BASE}/openai/v1",
    ]
    
    for base_url in base_urls_to_try:
        print(f"  Trying base_url: {base_url}")
        try:
            client = OpenAI(
                api_key=OPENAI_API_KEY,
                base_url=base_url,
                timeout=30.0,
            )
            
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "user", "content": "Reply with exactly: CONNECTION_OK"}
                ],
                max_tokens=20,
            )
            
            reply = response.choices[0].message.content.strip()
            print(f"  [SUCCESS] Got response: {reply}")
            print(f"  Model used: {response.model}")
            print(f"  Usage: {response.usage}")
            print()
            print(f"  >>> Working base_url: {base_url}")
            print(f"  >>> Update your .env: OPENAI_API_BASE={base_url}")
            return True
            
        except Exception as e:
            print(f"  [FAILED] {type(e).__name__}: {e}")
            print()
    
    return False


def test_list_models():
    """Try to list available models (may not be supported by all gateways)."""
    print("=" * 60)
    print("Test 2: List Available Models (optional)")
    print("=" * 60)
    
    try:
        from openai import OpenAI
        
        client = OpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE,
            timeout=15.0,
        )
        
        models = client.models.list()
        print("  Available models:")
        for model in models.data[:20]:  # Show first 20
            print(f"    - {model.id}")
        if len(models.data) > 20:
            print(f"    ... and {len(models.data) - 20} more")
        return True
        
    except Exception as e:
        print(f"  [INFO] Model listing not supported or failed: {e}")
        print("  (This is normal for some API gateways)")
        return False


def test_function_calling():
    """Test if the model supports function calling (required for Agent tools)."""
    print()
    print("=" * 60)
    print("Test 3: Function Calling Support (critical for Agent)")
    print("=" * 60)
    
    try:
        from openai import OpenAI
        
        client = OpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_API_BASE,
            timeout=30.0,
        )
        
        # Define a simple test tool
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_alarm_info",
                    "description": "Get information about a PLC alarm by its device name",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "device": {
                                "type": "string",
                                "description": "The alarm device name, e.g. F1, F65"
                            }
                        },
                        "required": ["device"]
                    }
                }
            }
        ]
        
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "You are a PLC diagnostic assistant."},
                {"role": "user", "content": "What causes alarm F1?"}
            ],
            tools=tools,
            tool_choice="auto",
            max_tokens=200,
        )
        
        msg = response.choices[0].message
        if msg.tool_calls:
            print(f"  [SUCCESS] Model made a tool call!")
            for tc in msg.tool_calls:
                print(f"    Function: {tc.function.name}")
                print(f"    Arguments: {tc.function.arguments}")
            print()
            print("  >>> Function calling is supported! Agent tools will work.")
            return True
        else:
            print(f"  [WARNING] Model responded with text instead of tool call:")
            print(f"    {msg.content[:200]}")
            print("  This might still work - the model may need stronger prompting.")
            return True
            
    except Exception as e:
        print(f"  [FAILED] {type(e).__name__}: {e}")
        print("  Function calling may not be supported with this model/gateway.")
        return False


if __name__ == "__main__":
    print()
    print("PLC Agent - API Connection Test")
    print("================================")
    print()
    
    # Test 1: Basic connection
    connected = test_with_openai_sdk()
    
    if connected:
        print()
        # Test 2: List models
        test_list_models()
        # Test 3: Function calling
        test_function_calling()
        print()
        print("=" * 60)
        print("ALL CRITICAL TESTS PASSED - Ready to build Agent!")
        print("=" * 60)
    else:
        print()
        print("=" * 60)
        print("CONNECTION FAILED")
        print("=" * 60)
        print()
        print("Troubleshooting steps:")
        print("  1. Check your API key in .env file")
        print("  2. Verify network access to the gateway URL")
        print("  3. Try different MODEL_NAME values")
        print("  4. Check if you need VPN/proxy")
        print("  5. Contact your AI Platform admin for correct base_url format")
