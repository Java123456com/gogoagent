"""L1 规则/关键词匹配器（对应 Java IntentRuleMatcher，规则表 1:1 移植）。

针对意图清晰、表达高度模板化的高频场景做关键词/正则匹配，目标延迟 < 50ms。
求值策略：对全部规则求值后再裁决，以识别「多类命中」→ 跨 ≥2 个目标子智能体即判
AMBIGUOUS（疑似多意图），放行 L3；同目标子智能体内的多类命中按规则优先级取首个。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from backend.intent.category import IntentCategory
from backend.intent.result import Confidence, IntentRecognitionResult, Source


class Verdict(Enum):
    HIT = "HIT"
    AMBIGUOUS = "AMBIGUOUS"
    MISS = "MISS"


@dataclass
class Outcome:
    verdict: Verdict
    result: Optional[IntentRecognitionResult] = None
    ambiguous_categories: list[IntentCategory] | None = None

    @classmethod
    def hit(cls, result: IntentRecognitionResult) -> "Outcome":
        return cls(Verdict.HIT, result=result)

    @classmethod
    def ambiguous(cls, categories: list[IntentCategory]) -> "Outcome":
        return cls(Verdict.AMBIGUOUS, ambiguous_categories=categories)

    @classmethod
    def miss(cls) -> "Outcome":
        return cls(Verdict.MISS)


# 多意图信号较强的并列/顺承连词（与 Router 的 L0 结构启发共用）
STRONG_CONJUNCTIONS = "然后|接着|顺便|顺带|以及|并且|另外|同时|完了再|之后再|再帮我|再给我|外加"

# 子句切分符：标点 + 强连词 + 弱连接词
CLAUSE_SPLITTER = re.compile(
    r"[，。；！？!?;,、]|" + STRONG_CONJUNCTIONS + r"|还要|还想|再帮|再给|再查|再订|再看|和|跟")


class _Rule:
    def __init__(self, category: IntentCategory, keyword: str, negative_keywords: list[str] | None = None):
        self.category = category
        self.keyword = re.compile(keyword, re.IGNORECASE)
        self.negative_keywords = [re.compile(n, re.IGNORECASE) for n in (negative_keywords or [])]

    def matches(self, text: str) -> bool:
        if not self.keyword.search(text):
            return False
        for negative in self.negative_keywords:
            if negative.search(text):
                return False
        return True


class IntentRuleMatcher:
    def __init__(self) -> None:
        self.rules: list[_Rule] = []

        greet = "你好|您好|哈喽|哈啰|嗨|hi|hello|hey|早上好|早安|上午好|中午好|下午好|晚上好" \
                "|在吗|在不在|在么|在不|有人吗|有人在吗|你在吗|请问|请教一下|打扰一下|打扰了|方便吗"
        sep = r"[\\s,，。.!！?？～~、]*"
        self._add(IntentCategory.GREETING,
                  "^(?:" + greet + ")(?:" + sep + "(?:" + greet + "))*" + sep + "$")

        self._add(IntentCategory.REIMBURSEMENT,
                  "(报销|报账|报帐|贴票|发票|报销单|费用报销|差旅报销|出差费用|生成报销单|识别发票|"
                  "发票识别|提交报销|报一下|帮我报|报个销|走报销|电子发票|机票行程单)",
                  ["政策", "标准", "规定", "制度", "额度", "限额", "能不能报", "能报吗",
                   "报销吗", "可以报", "怎么报", "报销范围", "报销比例"])

        self._add(IntentCategory.POLICY_QUERY,
                  "(差旅政策|差旅规定|差旅制度|差旅标准|差标|超标|餐标|餐费标准|住宿标准|酒店标准|"
                  "机票标准|舱位标准|高铁标准|座位标准|费用标准|报销标准|报销政策|报销规定|报销额度|"
                  "报销范围|能不能报|可以报销吗|能报销吗|预订规定|预定规定|订票规定|购票规定|签证|"
                  "入境政策|出差政策|出行政策|差旅管理|出差规定|出差标准)")

        self._add(IntentCategory.APPROVAL_QUERY,
                  "(审批进度|审批状态|审批结果|审批通过了?吗?|审批到哪|审批到哪个|审批环节|审批意见|"
                  "审批人|审批流程|我的审批|审批单状态|批了吗|批没批|审没审|通过了没|领导.*批|谁.*审批)")

        self._add(IntentCategory.TRAVEL_CANCEL,
                  "(取消出差|取消差旅|取消审批|取消我的(差旅|出差)|撤回(差旅|出差|审批)?申请|"
                  "撤销(差旅|出差|审批)?申请|撤回审批|这次不去了?|不出差了|出差取消了?|把.*(差旅|出差|申请).*撤了?)")

        self._add(IntentCategory.TRAVEL_MODIFY,
                  "((修改|变更).*(差旅|出差|申请|行程|订单)|改期|延期|(差旅|出差).*改一?下?|"
                  "改一下.*(日期|时间|目的地|行程)|调整.*(日期|时间|行程)|把.*(日期|时间|目的地).*改)",
                  ["取消", "撤回", "撤销", "改签", "退票", "退订"])

        self._add(IntentCategory.TRAVEL_ORDER_QUERY,
                  "(差旅单|出差单|差旅订单|差旅详情|差旅记录|出差记录|我的差旅|我的出差"
                  "|(我的|上次|最近|近期|历史|本周|本月|下周|有|查|看)[^，。；！？、]{0,6}(出差|差旅)(安排|行程)"
                  "|差旅单详情|出差单状态|差旅单状态|上次的?(差旅|出差)|历史(差旅|出差))",
                  ["提交", "发起", "提个", "新建", "报备", "取消", "规划", "报销", "做一份",
                   "做个", "做一下", "出一份", "方案", "处理", "帮我办", "我要办", "安排一下"])

        self._add(IntentCategory.ATTRACTIONS_QUERY,
                  "(有什么好玩|好玩的地方|景点|景区|风景区|名胜|游玩|游览|打卡|必去|必玩|"
                  "一日游|周边游|当地特色|有什么好吃|美食推荐|特产)")

        self._add(IntentCategory.GENERAL_INFO,
                  "(天气|气温|多少度|冷不冷|热不热|下雨|下雪|限行|路况|堵不堵|怎么去|怎么走|"
                  "地铁|公交|打车|时差|汇率|新闻|资讯)",
                  ["机票", "航班", "火车票", "高铁票", "订", "预订", "报销"])

        self._add(IntentCategory.ITINERARY_PLANNING,
                  "(规划.*行程|安排.*行程|做.*行程|行程规划|行程安排|行程方案|出行方案|"
                  "做一份行程|出一份行程|做个行程|帮我规划|规划一下|帮我安排一下)")

        self._add(IntentCategory.FLIGHT_SEARCH,
                  "(查机票|订机票|搜机票|看机票|买机票|机票|航班|飞机票|航班信息|航班时刻|"
                  "头等舱|经济舱|公务舱|往返机票|单程机票|直飞|廉价航班)",
                  ["发票", "报销", "标准", "政策", "取消", "退票", "改签", "预订", "订这个", "订下来", "下单"])

        self._add(IntentCategory.TRAIN_SEARCH,
                  "(查火车|订火车|搜火车|看火车|买火车票|高铁|动车|火车票|火车|车次|列车|"
                  "高铁票|城际|二等座|一等座|商务座)",
                  ["标准", "政策", "取消", "退票", "改签", "预订", "订这个", "订下来", "下单"])

        self._add(IntentCategory.HOTEL_SEARCH,
                  "(查酒店|订酒店|搜酒店|看酒店|住酒店|附近.*酒店|酒店|住宿|住哪里?|住哪儿|"
                  "入住|宾馆|民宿|快捷酒店|连锁酒店|标间|大床房)",
                  ["标准", "政策", "报销", "取消", "退订", "预订", "订这个", "订下来", "下单"])

        self._add(IntentCategory.BOOKING,
                  "(预订|下单|订这个|订下来|就订(这个|它)|帮我订(这个|下)|确认(预订|下单|预定)|"
                  "改签|退票|退订|取消(预订|订单|机票|酒店|火车票))")

        self._add(IntentCategory.TRAVEL_APPLICATION,
                  "(申请出差|出差申请|申请.{0,10}出差|发起(差旅|出差)|提个.*(出差|申请)|"
                  "提交.*(出差|差旅|申请)|帮我提.*(出差|申请)|我要出差|我想出差|我需要出差|"
                  "我要去.*出差|(下周|下个月|明天|后天|下下周).*出差|新建(差旅|出差)|报备出差|出差报备)",
                  ["审批进度", "审批状态", "审批结果", "取消", "查", "规划", "报销"])

    def _add(self, category: IntentCategory, keyword: str,
             negative_keywords: list[str] | None = None) -> None:
        self.rules.append(_Rule(category, keyword, negative_keywords))

    # ---------------- 求值 ----------------

    def evaluate(self, text: str | None) -> Outcome:
        if not text or not text.strip():
            return Outcome.miss()
        normalized = text.strip()

        # 步骤 1：多意图守卫——按标点/连词拆子句，逐句匹配
        clause_categories = self._match_clause_categories(normalized)
        if not clause_categories:
            return Outcome.miss()

        distinct_agents = {c.default_target_agent for c in clause_categories}
        if len(distinct_agents) >= 2:
            return Outcome.ambiguous(clause_categories)

        # 步骤 2：全文按优先级取首个命中规则
        hit = self._first_match(normalized)
        if hit is None:
            return Outcome.miss()
        return Outcome.hit(IntentRecognitionResult.single(
            Source.RULE, hit.category, Confidence.HIGH,
            f"L1 规则命中：关键词匹配到「{hit.category.description}」", None))

    def match(self, text: str) -> IntentRecognitionResult | None:
        outcome = self.evaluate(text)
        return outcome.result if outcome.verdict == Verdict.HIT else None

    def _first_match(self, text: str) -> _Rule | None:
        if not text:
            return None
        for rule in self.rules:
            if rule.matches(text):
                return rule
        return None

    def _match_clause_categories(self, text: str) -> list[IntentCategory]:
        clauses = CLAUSE_SPLITTER.split(text)
        categories: list[IntentCategory] = []
        hit_greeting = False
        for clause in clauses:
            hit = self._first_match(clause.strip())
            if hit is None:
                continue
            if hit.category == IntentCategory.GREETING:
                hit_greeting = True
            elif hit.category not in categories:
                categories.append(hit.category)

        if hit_greeting and not categories:
            return [IntentCategory.GREETING]
        return categories
