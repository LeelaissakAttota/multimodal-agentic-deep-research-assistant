# Architecture Overview

Based on the approved master architecture (see `docs/architecture/master_architecture.png`), this document describes the software architecture of the Multimodal Agentic Deep Research Assistant.

## Major Components

### 1. Master Research Orchestrator
- **Responsibility**: Coordinating the entire research workflow, delegating to specialized agents, and managing research state.
- **Boundaries**: Interfaces with Planning Agent, Research Agents, Analysis Agent, Evaluation Agent, and Report Agent.
- **Data Flow**: Receives user research question, outputs research plan, manages task delegation, synthesizes final report.

### 2. Planning Agent
- **Responsibility**: Decomposing research questions into actionable research tasks and creating research plans.
- **Boundaries**: Receives research question from Orchestrator, outputs structured research plan.

### 3. Research Agents (Specialized)
- **Responsibility**: Executing individual research tasks using appropriate tools (web search, document analysis, etc.).
- **Boundaries**: Interacts with Tool Registry, executes tool calls, returns raw findings and evidence.

### 4. Analysis/Synthesis Agent
- **Responsibility**: Extracting evidence from raw findings, analyzing information, and synthesizing insights.
- **Boundaries**: Receives raw research outputs, outputs analyzed evidence and claims.

### 5. Evaluation/Reflection Agent
- **Responsibility**: Evaluating research quality, checking for evidence gaps, and determining if replanning is needed.
- **Boundaries**: Receives synthesized analysis, outputs evaluation score and gap report; can trigger replanning.

### 6. Report Agent
- **Responsibility**: Generating final traceable report with citations from validated evidence and claims.
- **Boundaries**: Receives evaluated analysis, outputs final report in multiple formats.

### 7. Tool Registry
- **Responsibility**: Managing available research tools and enabling dynamic tool selection.
- **Boundaries**: Provides tool interfaces to Research Agents; tools include web search, document parsers, image/video analyzers, API clients.

### 8. Evidence Pipeline
- **Responsibility**: Normalizing evidence from diverse sources, establishing provenance, and supporting citation generation.
- **Boundaries**: Connects Research Agents to Analysis Agent; ensures evidence is traceable to source.

### 9. Context & Memory System
- **Responsibility**: Managing research context, compressing information, and maintaining research history across iterations.
- **Boundaries**: Serves all agents; stores intermediate findings, research plans, and evaluation results.

### 10. Model Gateway
- **Responsibility**: Abstracting interactions with various LLM providers (development and runtime).
- **Boundaries**: Used by all agents for LLM calls; provider-agnostic interface.

### 11. Observability & Logging
- **Responsibility**: Tracking agent actions, tool usage, token consumption, and system performance.
- **Boundaries**: Cross-cutting concern; integrated into all major components.

## Data Flow Summary
1. User submits research question to Orchestrator.
2. Orchestrator delegates to Planning Agent to create research plan.
3. Orchestrator delegates research tasks to specialized Research Agents.
4. Research Agents use tools to collect multimodal information, returning raw findings.
5. Analysis/Synthesis Agent processes findings to extract evidence and form claims.
6. Evaluation/Reflection Agent assesses quality and identifies gaps.
7. If gaps found, Orchestrator may replan; otherwise, proceeds to Report Agent.
8. Report Agent generates final report with citations from validated evidence.

## Failure Boundaries
- Tool execution failures are caught and reported to Orchestrator for retry/replanning.
- Model gateway includes fallback and retry logic.
- Evidence validation prevents propagation of unverified claims.
- Context compression includes summarization to prevent overflow.

## Configuration
- Environment-based configuration for API keys, model endpoints, and system limits.
- Separate configuration for development vs. production.
