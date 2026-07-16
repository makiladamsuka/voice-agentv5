"""Watchdog to automatically re-run the intent compiler when events change."""

import time
import threading
import traceback
from pathlib import Path

from voice.compiler.intent_compiler import build_cache

APP_DIR = Path(__file__).resolve().parent.parent.parent

def run_watchdog(interval: int = 10, daemon: bool = True) -> threading.Thread:
    """
    Start a background thread that monitors extracted_events.json and
    re-runs the intent compiler (build_cache) whenever the file is updated.
    """
    events_file = APP_DIR / "voice" / "event_db" / "extracted_events.json"
    
    def _watch():
        print("[IntentWatchdog] Started watching for event DB changes...")
        last_mtime = 0.0
        
        # Initial check
        if events_file.exists():
            last_mtime = events_file.stat().st_mtime
            
        while True:
            time.sleep(interval)
            try:
                if events_file.exists():
                    current_mtime = events_file.stat().st_mtime
                    if current_mtime > last_mtime:
                        print("\n[IntentWatchdog] Detected changes in extracted_events.json. Rebuilding NLU cache...")
                        last_mtime = current_mtime
                        try:
                            build_cache()
                            print("[IntentWatchdog] NLU cache rebuild complete.")
                            
                            # Tell the NLU server to reload the runtime
                            try:
                                from voice.nlu_server import reload_nlu_runtime
                                reload_nlu_runtime()
                            except ImportError:
                                pass
                            
                        except Exception as e:
                            print(f"[IntentWatchdog] Error building cache: {e}")
                            traceback.print_exc()
            except Exception as e:
                print(f"[IntentWatchdog] Watcher error: {e}")

    thread = threading.Thread(target=_watch, daemon=daemon, name="IntentWatchdog")
    thread.start()
    return thread

if __name__ == "__main__":
    # Run in foreground for testing
    run_watchdog(interval=5, daemon=False)
