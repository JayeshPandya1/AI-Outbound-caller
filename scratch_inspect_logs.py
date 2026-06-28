import asyncio
import os
import sys

async def main():
    if not os.path.exists("agent_debug.log"):
        print("agent_debug.log does not exist")
        return
        
    with open("agent_debug.log", "r", encoding="utf-16", errors="ignore") as f:
        lines = f.readlines()
        
    print(f"Total log lines: {len(lines)}")
    
    # Filter lines that mention "7344" (part of the last room name) or "sip_"
    relevant_lines = []
    for line in lines:
        if "7344" in line or "sip_" in line or "dial" in line.lower() or "failed" in line.lower() or "answered" in line.lower():
            relevant_lines.append(line.strip())
            
    print(f"Found {len(relevant_lines)} relevant lines:")
    print("-" * 120)
    for line in relevant_lines[-50:]:  # Show last 50 matches
        sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
    print("-" * 120)

if __name__ == "__main__":
    asyncio.run(main())
