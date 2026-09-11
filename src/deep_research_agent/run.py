import asyncio
import sys
import os
from dotenv import load_dotenv
from langchain.messages import HumanMessage
from src.deep_research_agent.graph import deep_researcher

load_dotenv()

async def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.deep_research_agent.run \"Your query here\"")
        sys.exit(1)
        
    query = sys.argv[1]
    print(f"Starting research on: {query}")
    print("-" * 50)
    
    # New input state expects a list of messages
    initial_state = {"messages": [HumanMessage(content=query)]}
    
    # Stream the execution
    try:
        async for event in deep_researcher.astream(initial_state):
            for key, value in event.items():
                if key == "clarify_with_user":
                    # Check if we got a question back or verification
                    messages = value.get("messages", [])
                    if messages:
                        print(f"\n--- Clarification Phase ---\n{messages[-1].content}")
                        
                elif key == "write_research_brief":
                    print("\n--- Research Brief Generated ---")
                    brief = value.get("research_brief")
                    if brief:
                        print(brief)
                        
                elif key == "research_supervisor":
                    # Supervisor updates
                    print(".", end="", flush=True)
                    
                elif key == "final_report_generation":
                    print("\n\n=== FINAL REPORT ===\n")
                    print(value.get("final_report"))
                    
    except Exception as e:
        print(f"\nError running agent: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
