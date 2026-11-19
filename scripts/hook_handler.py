#!/usr/bin/env python3
"""Antigravity PostInvocation / Stop hook handler for local-usage-monitor.

Reads hook JSON payload from stdin, incrementally ingests the active transcript.jsonl
into ~/.gemini/local-usage-monitor/usage.db, and returns the exact JSON contract expected
by Antigravity without adding any context tokens or making network calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure sibling modules are importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker import ingest_transcript


def main() -> None:
    event_name = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        event_name = str(payload.get("hookEventName") or "")
        transcript_path = str(payload.get("transcriptPath") or "")
        conversation_id = str(payload.get("conversationId") or "")
        model_name = str(payload.get("modelName") or "")

        if transcript_path:
            ingest_transcript(
                transcript_path=transcript_path,
                conversation_id=conversation_id,
                hint_model=model_name,
                force=False,
            )
    except Exception:
        # Never break or block the Antigravity agent execution loop
        pass

    if event_name == "Stop":
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
    else:
        sys.stdout.write("{}\n")


if __name__ == "__main__":
    main()
