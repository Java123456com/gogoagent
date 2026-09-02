from backend.agents.base import BaseSubAgent
from backend.infrastructure.llm import stable_model
from backend.tools.live import destination_tools
from backend.tools.memory import tools as memory_tools
from backend.tools.policy import tools as policy_tools
from backend.tools.review import tools as review_tools


class ItineraryReviewAgent(BaseSubAgent):
    prompt_file = "itinerary-review-agent-system.md"
    model_factory = staticmethod(stable_model)
    max_iterations = 8
    def __init__(self): super().__init__(review_tools() + destination_tools() + policy_tools() + memory_tools())


itinerary_review_agent = ItineraryReviewAgent()
