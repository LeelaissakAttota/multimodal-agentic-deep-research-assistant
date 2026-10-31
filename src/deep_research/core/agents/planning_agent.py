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

    def __init__(self, source_tools: Iterable[str] = (), preferred: Iterable[str] = (),
                 saved_topics: Iterable[str] = ()) -> None:
        self.source_tools = [t for t in source_tools if t in SOURCE_ROUTES]
        self.preferred = [t for t in preferred if t in self.source_tools]
        self.saved_topics = [t for t in saved_topics if t.strip()]

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
                     *topic_tasks(request.objective, self.saved_topics),
                     *source_tasks(request.objective, self.source_tools, self.preferred)]

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


_TOPIC_STOP = frozenset(
    "about after also among and are because been before being between both but can could does doing during each from "
    "have how into its more most much other over same should some such than that their them then there these they "
    "this those through under until very what when where which while who whom why will with would your".split()
)


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def _topic_terms(text: str) -> dict[str, str]:
    """Light-stemmed meaningful terms -> original word (first occurrence)."""
    terms: dict[str, str] = {}
    for word in re.findall(r"[a-z0-9][a-z0-9+#-]{2,}", text.lower()):
        if word not in _TOPIC_STOP:
            terms.setdefault(_stem(word), word)
    return terms


def related_topic(question: str, topics: Iterable[str]) -> tuple[str, list[str]] | None:
    """Best saved topic sharing a meaningful term with the question, plus its terms the question lacks.

    ``None`` when no topic is related or the related topic adds nothing new (no plan change).
    """
    q = _topic_terms(question)
    best: tuple[int, str, list[str]] | None = None
    for topic in topics:
        terms = _topic_terms(topic)
        overlap = len(q.keys() & terms.keys())
        extra = [word for stem, word in terms.items() if stem not in q]
        if overlap and extra and (best is None or overlap > best[0]):
            best = (overlap, topic.strip(), extra)
    return (best[1], best[2]) if best else None


def topic_tasks(question: str, topics: Iterable[str]) -> list[ResearchTask]:
    """At most one extra web task biased toward a related saved topic (user profile); none when unrelated/empty."""
    match = related_topic(question, topics)
    if match is None:
        return []
    topic, extra = match
    return [ResearchTask(
        plan_id=UUID(int=0),
        description=f"Relate to saved topic '{topic}': {question}",
        objective=f"Find how {question} relates to {topic}",
        assigned_tool="web_search",
        tool_input={"query": f"{question} {' '.join(extra[:4])}", "limit": 5},
        metadata={"section": f"Related to {topic}"[:80], "question": question, "saved_topic": topic,
                  "focus": f"how the question connects to {topic}"},
    )]


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
    "hackernews_search": (("hacker news", "hackernews", "show hn", "ask hn", "y combinator", "startup*",
                           "developer*", "programmer*", "engineers think"),
                          "Hacker News discussion", "Survey Hacker News discussion on"),
    "bluesky_search": (("bluesky", "bsky", "social media", "tweet*", "twitter", "trending", "reaction*",
                        "public reaction*", "posts about"),
                       "Bluesky posts", "Survey Bluesky posts on"),
    "stackexchange_search": (("stack overflow", "stackoverflow", "stack exchange", "stackexchange", "error message",
                              "exception", "how do i", "how to fix", "bug@", "compile*", "python@", "javascript",
                              "sql", "p-value", "theorem", "black-scholes", "q&a"),
                             "Stack Exchange Q&A", "Find Stack Exchange questions and answers on"),
}


def route_source_tools(question: str, enabled: Iterable[str]) -> list[str]:
    """Source tools whose keywords appear in the question (in SOURCE_ROUTES order), max two."""
    allowed = set(enabled)
    lowered = question.lower()
    picked = [tool for tool, (keys, _, _) in SOURCE_ROUTES.items()
              if tool in allowed and any(_key_matches(k, lowered) for k in keys)]
    return picked[:2]


#: Programming-context words that must also appear for ambiguous keys marked with a trailing ``@``
#: (e.g. ``python@``: "python snake habitat" and "bed bug infestation" are not coding questions).
_CODE_CONTEXT = re.compile(
    r"\b(code|coding|error|errors|exception|exceptions|traceback|stack ?trace|library|libraries|package|module|"
    r"script|scripts|function|functions|api|apis|debug\w*|compil\w*|programming|programmer\w*|syntax|crash\w*|"
    r"software|pip|numpy|pandas|django|flask|asyncio|regex|runtime|segfault|unit tests?)\b")


def _key_matches(key: str, lowered: str) -> bool:
    if key.endswith("@"):
        return bool(re.search(_key_pattern(key[:-1]), lowered)) and bool(_CODE_CONTEXT.search(lowered))
    return bool(re.search(_key_pattern(key), lowered))


def _key_pattern(key: str) -> str:
    """Whole word (plural allowed); a trailing ``*`` marks a prefix stem."""
    if key.endswith("*"):
        return rf"\b{re.escape(key[:-1])}"
    return rf"\b{re.escape(key)}(?:s|es)?\b"


def source_tasks(question: str, enabled: Iterable[str], preferred: Iterable[str] = ()) -> list[ResearchTask]:
    """One task per routed public-source tool; each is cited like a web task.

    ``preferred`` (user profile) adds the first preferred enabled tool that routing did not pick,
    so a preferred source is consulted for every question (still at most three source tasks).
    """
    enabled = list(enabled)
    tools = route_source_tools(question, enabled)
    extra = [t for t in preferred if t in enabled and t in SOURCE_ROUTES and t not in tools]
    tools += extra[:1]
    tasks: list[ResearchTask] = []
    for tool in tools:
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
