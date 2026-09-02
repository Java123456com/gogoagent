import functools
import operator

from .apikey import tools as api_key_tools
from .booking import tools as booking_tools
from .conflict import tools as conflict_tools
from .interaction import tools as interaction_tools
from .knowledge import tools as knowledge_tools
from .live import tools as live_tools
from .order import tools as order_tools
from .plan_html import tools as plan_html_tools
from .planner import tools as planner_tools
from .policy import tools as policy_tools
from .reimbursement import tools as reimbursement_tools
from .review import tools as review_tools
from .skills import tools as skill_tools
from .user_info import tools as user_info_tools


def all_tools():
    return functools.reduce(operator.iadd, (factory() for factory in (api_key_tools, booking_tools, conflict_tools, interaction_tools, knowledge_tools, live_tools, order_tools, plan_html_tools, planner_tools, policy_tools, reimbursement_tools, review_tools, skill_tools, user_info_tools)), [])


__all__ = ["all_tools"]
