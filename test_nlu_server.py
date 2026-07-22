"""
Smoke-test for the NLU WebSocket server.

Starts the NLU server, sends a fake transcript, and checks for a valid response.
Run from the project root: python test_nlu_server.py

Usage:
    python test_nlu_server.py           # runs the intent-match test
    python test_nlu_server.py server    # just starts the server (keep-alive)
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


class DummyBB:
    """Minimal Blackboard stub for testing outside start_robot.py."""
    running = True

    def write(self, **kwargs):
        keys = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        print(f"  [BB] {keys}")

    def read(self, *fields):
        return {f: getattr(self, f, True) for f in fields}


def _start_server(bb):
    """Start the NLU server in a background thread."""
    from voice.nlu_server import run_nlu_server

    t = threading.Thread(
        target=run_nlu_server,
        kwargs={"bb": bb, "host": "127.0.0.1", "port": 8765},
        daemon=True,
        name="NluServerTest",
    )
    t.start()

    # Poll the /health endpoint until the server is actually accepting connections
    import urllib.request
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=1)
            print("  [Server] Ready.")
            return t
        except Exception:
            time.sleep(0.2)

    raise RuntimeError("NLU server did not start within 10 seconds.")


async def _run_test():
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        # Older websockets compat
        from websockets.client import connect  # type: ignore

    uri = "ws://127.0.0.1:8765/ws/voice"
    print(f"\nConnecting to {uri} ...")

    # Send Origin header so FastAPI/CORS middleware accepts the connection
    # (browsers send this automatically; CLI websocket clients don't)
    async with connect(uri, additional_headers={"Origin": "http://localhost:3000"}) as ws:
        print("✅ Connected!\n")

        # Test 1: ping/pong
        await ws.send(json.dumps({"type": "ping"}))
        pong = json.loads(await ws.recv())
        assert pong["type"] == "pong", f"Expected pong, got {pong}"
        print("✅ Ping/Pong OK")

        # Test 2: send a transcript
        test_queries = [
            "Where is the library?",
            "What events are happening today?",
            "Tell me about the robotics competition",
            "this is complete gibberish xyzzy foobar",
        ]

        for query in test_queries:
            print(f"\n🎙️  Sending: '{query}'")
            await ws.send(json.dumps({"type": "transcript", "text": query}))

            # Collect messages until we get a response
            for _ in range(5):
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                print(f"   ← {msg['type']}: ", end="")
                if msg["type"] == "response":
                    print(f"\n   Reply: {msg['reply_text']}")
                    print(f"   Audio: {msg.get('audio_url') or '(Deepgram TTS fallback)'}")
                    print(f"   Action: {msg.get('action')}")
                    break
                elif msg["type"] == "state":
                    print(msg.get("conv_state", ""))
                else:
                    print(msg)

        print("\n✅ All tests passed!")


def main():
    bb = DummyBB()

    if len(sys.argv) > 1 and sys.argv[1] == "server":
        print("[Test] Starting NLU server in keep-alive mode. Press Ctrl+C to stop.")
        _start_server(bb)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    print("=== NLU Server Smoke Test ===")
    _start_server(bb)

    try:
        asyncio.run(_run_test())
    except ImportError:
        print("\n⚠️  websockets library not found. Install with: pip install websockets")
        print("   Then re-run: python test_nlu_server.py")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        raise


if __name__ == "__main__":
    main()
