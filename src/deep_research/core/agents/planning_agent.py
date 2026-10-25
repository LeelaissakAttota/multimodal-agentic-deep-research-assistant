"""
Planning Agent abstraction for decomposing research requests into tasks.
"""
import re
from abc import ABC, abstractmethod
from typing import Iterable
from uuid import UUID

from deep_research.context.context_builder import AgentContext
from deep_research.domain.research_request import ATTACHMENT_TOOLS, ResearchRequest
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research.research_task import ResearchTask


class PlanningAgent(ABC):
    """
    Abstract base class for planning agents that decompose research requests
    into actionable research tasks with dependencies.
    """

    @abstractmethod
    async def create_plan(
        self,
        request: ResearchRequest,
        state: ResearchState,
        context: AgentContext | None = None,
    ) -> ResearchPlan:
        """
        Create a research plan to fulfill the given research request.

        Args:
            request: The research request to fulfill
            state: Current research state (may contain historical context)

        Returns:
            A research plan containing tasks to execute
        """
        pass


class DeterministicPlanningAgent(PlanningAgent):
    """
    Deterministic implementation of PlanningAgent for Phases 2 and 4.
    Creates tasks based on the research request and evaluation gaps.

    ``source_tools`` lists the public-source tools (academic, video, social,
    image) this run may use; matching questions get one extra task routed to
    each relevant tool. Empty (default) keeps the web-only plan.
    """

    def __init__(self, source_tools: Iterable[str] = ()) -> None:
        self.source_tools = [t for t in source_tools if t in SOURCE_ROUTES]

    async def create_plan(
        self,
        request: ResearchRequest,
        state: ResearchState,
        context: AgentContext | None = None,
    ) -> ResearchPlan:
        """
        Create a plan from evaluation gaps, or an initial request-based plan.
        """
        gaps = list(
            dict.fromkeys(
                gap.strip() for gap in state.last_evaluation_gaps if gap.strip()
            )
        )
        if gaps:
            tasks = [
                ResearchTask(
                    plan_id=UUID(int=0),  # Replaced after plan creation
                    description=f"Address gap: {gap}",
                    objective=f"Gather information to address the gap: {gap}",
                    assigned_tool="web_search",
                    tool_input={"query": gap, "limit": 5},
                )
                for gap in gaps
            ]
        else:
            # Attachments first: their content becomes primary sources for later web tasks.
            tasks = [*_attachment_tasks(request), *_web_tasks(request.objective),
                     *source_tasks(request.objective, self.source_tools)]

        plan = ResearchPlan(
            request_id=request.id,
            objective=request.objective,
            tasks=tasks,
        )

        for task in plan.tasks:
            task.plan_id = plan.id

        return plan


#: (keywords, section heading, search suffix, synthesis focus); first matches win, max two aspects.
_ASPECTS: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (("benefit", "advantage", "pros", "upside", "opportunit"), "Benefits", "benefits advantages",
     "the benefits and advantages only"),
    (("risk", "drawback", "disadvantage", "cons", "downside", "danger", "challenge", "concern"), "Risks",
     "risks drawbacks challenges", "the risks, drawbacks and challenges only"),
    (("cost", "price", "econom", "afford"), "Costs and economics", "cost economics", "costs and economics only"),
    (("compare", "comparison", " vs", "versus", "difference"), "Comparison", "comparison",
     "how the options compare"),
    (("history", "origin", "evolution"), "History", "history", "the historical background"),
    (("future", "outlook", "trend", "recent", "latest", "advance"), "Recent developments", "recent developments",
     "recent developments and outlook"),
)
_DEFAULT_ASPECTS: tuple[tuple[str, str, str], ...] = (
    ("Recent developments", "recent developments", "recent developments and outlook"),
    ("Significance and impact", "implications and impact", "significance, implications and impact"),
)


def detect_aspects(question: str) -> list[tuple[str, str, str]]:
    """Keyword-based section plan: (heading, search suffix, focus) for up to two aspects of the question."""
    lowered = question.lower()
    found = [(heading, suffix, focus) for keys, heading, suffix, focus in _ASPECTS
             if any(_has_word(lowered, key) for key in keys)]
    for default in _DEFAULT_ASPECTS:
        if len(found) >= 2:
            break
        if default[0] not in {f[0] for f in found}:
            found.append(default)
    return found[:2]


def _has_word(text: str, key: str) -> bool:
    """Word-start match; short keys ('pros', 'cons', 'vs') must also end at a word boundary."""
    key = key.strip()
    tail = r"s?\b" if len(key) <= 4 else ""
    return re.search(rf"\b{re.escape(key)}{tail}", text) is not None


def _web_tasks(question: str) -> list[ResearchTask]:
    tasks = [
        ResearchTask(
            plan_id=UUID(int=0),  # Replaced after plan creation
            description=f"Gather background information on: {question}",
            objective=f"Understand the fundamentals of {question}",
            assigned_tool="web_search",
            tool_input={"query": question, "limit": 5},
            metadata={"section": "Fundamentals", "question": question,
                      "focus": "brief background: what it is and how it works, plus the key answer"},
        )
    ]
    for heading, suffix, focus in detect_aspects(question):
        if heading == "Recent developments":
            description = f"Find recent developments in: {question}"
            objective = f"Identify recent trends and advances in {question}"
        elif heading == "Significance and impact":
            description = f"Analyze implications of: {question}"
            objective = f"Determine the significance and impact of {question}"
        else:
            description = f"Research {heading.lower()} for: {question}"
            objective = f"Identify the {heading.lower()} relevant to {question}"
        tasks.append(
            ResearchTask(
                plan_id=UUID(int=0),
                description=description,
                objective=objective,
                assigned_tool="web_search",
                tool_input={"query": f"{suffix} {question}", "limit": 5},
                metadata={"section": heading, "question": question, "focus": focus},
            )
        )
    return tasks


def _attachment_tasks(request: ResearchRequest) -> list[ResearchTask]:
    """One task per PDF/image attachment, routed to its multimodal tool."""
    tasks: list[ResearchTask] = []
    for index, attachment in enumerate(request.attachments, 1):
        tool_id, param = ATTACHMENT_TOOLS[attachment.kind]
        kind = "web page" if attachment.kind == "url" else attachment.kind
        label = attachment.name or f"{kind} attachment {index}"
        tasks.append(
            ResearchTask(
                plan_id=UUID(int=0),
                description=f"Analyze {kind} input '{label}' for: {request.objective}",
                objective=f"Extract evidence from the provided {kind} relevant to {request.objective}",
                assigned_tool=tool_id,
                tool_input={param: attachment.data, "source_title": label},
                metadata={"section": f"Attached {kind}: {label}", "question": request.objective},
            )
        )
    return tasks


#: Source tool id -> (keyword stems that route a question to it, section heading, task verb).
SOURCE_ROUTES: dict[str, tuple[tuple[str, ...], str, str]] = {
    "academic_search": (("paper", "study", "studies", "research", "academic", "scientific", "journal", "arxiv",
                         "peer-review*", "literature", "clinical", "trial", "meta-analy*", "survey of",
                         "state of the art", "benchmark", "preprint", "citation"),
                        "Academic literature", "Find peer-reviewed papers and preprints on"),
    "youtube_transcript": (("youtube", "video", "lecture", "talk", "keynote", "tutorial", "podcast", "interview",
                            "explainer", "watch"),
                           "Video sources", "Read transcripts of videos about"),
    "reddit_search": (("reddit", "opinion", "community", "communities", "experience", "people think", "users say",
                       "discussion", "forum", "sentiment", "anecdot*", "real-world", "worth it", "reviews"),
                      "Community discussion", "Survey community discussion on"),
    "commons_image_search": (("image", "picture", "photo", "photograph", "diagram", "illustration", "look like", "looks like",
                              "map of", "visual", "chart"),
                             "Images", "Find freely licensed images of"),
    "hackernews_search": (("hacker news", "hackernews", "hn", "show hn", "ask hn", "y combinator", "startup*",
                           "developer*", "programmer*", "engineers think"),
                          "Hacker News discussion", "Survey Hacker News discussion on"),
    "bluesky_search": (("bluesky", "bsky", "social media", "tweet*", "twitter", "trending", "reaction*",
                        "public reaction*", "posts about"),
                       "Bluesky posts", "Survey Bluesky posts on"),
}


def route_source_tools(question: str, enabled: Iterable[str]) -> list[str]:
    """Source tools whose keywords appear in the question (in SOURCE_ROUTES order), max two."""
    allowed = set(enabled)
    lowered = question.lower()
    picked = [tool for tool, (keys, _, _) in SOURCE_ROUTES.items()
              if tool in allowed and any(re.search(_key_pattern(k), lowered) for k in keys)]
    return picked[:2]


def _key_pattern(key: str) -> str:
    """Whole word (plural allowed); a trailing ``*`` marks a prefix stem."""
    if key.endswith("*"):
        return rf"\b{re.escape(key[:-1])}"
    return rf"\b{re.escape(key)}(?:s|es)?\b"


def source_tasks(question: str, enabled: Iterable[str]) -> list[ResearchTask]:
    """One task per routed public-source tool; each is cited like a web task."""
    tasks: list[ResearchTask] = []
    for tool in route_source_tools(question, enabled):
        _, heading, verb = SOURCE_ROUTES[tool]
        tasks.append(
            ResearchTask(
                plan_id=UUID(int=0),
                description=f"{verb}: {question}",
                objective=f"{heading} relevant to {question}",
                assigned_tool=tool,
                tool_input={"query": question, "limit": 5 if tool != "youtube_transcript" else 2},
                metadata={"section": heading, "question": question, "source_tool": tool,
                          "focus": f"what the {heading.lower()} say about the question"},
            )
        )
    return tasks
