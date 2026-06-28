import json
import os
import sys

def main():
    log_dir = r"C:\Users\Nitro 7\.gemini\antigravity-ide\brain\69223988-2827-42e6-8fc1-e9a863317ba4\.system_generated\logs"
    transcript_path = os.path.join(log_dir, "transcript.jsonl")
    
    if not os.path.exists(transcript_path):
        print(f"Transcript path not found: {transcript_path}")
        return
        
    print(f"Reading transcript from {transcript_path}")
    
    with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                step = json.loads(line)
                content = step.get("content", "")
                if "received job request" in content:
                    print("\nFOUND COOLIFY LOGS IN TRANSCRIPT:")
                    print("=" * 80)
                    sys.stdout.buffer.write(content.encode("utf-8", errors="replace"))
                    print("\n" + "=" * 80)
            except Exception as e:
                pass

if __name__ == "__main__":
    main()
