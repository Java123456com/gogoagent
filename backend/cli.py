import argparse
import json
from backend.infrastructure.bootstrap import bootstrap
from backend.services.travel_service import TravelAgentService
from backend.runtime.agent_executor import close_subagent_executor


def main():
    parser = argparse.ArgumentParser(description="GoGo LangChain + LangGraph multi-agent travel assistant")
    parser.add_argument("request", nargs="?", default="帮我查一下差旅政策")
    parser.add_argument("--user-id", default="u_001")
    args = parser.parse_args()
    bootstrap()
    try:
        result = TravelAgentService().run(args.request, args.user_id, "cli-session")
        print(json.dumps({"final": result.get("final", ""), "intent": result.get("intent_json", {}), "trace": result.get("trace", [])}, ensure_ascii=False, indent=2, default=str))
    finally:
        close_subagent_executor()


if __name__ == "__main__": main()
