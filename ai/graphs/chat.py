from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from ai.constants import GRADER_MODEL, MAX_REWRITES, MAX_TOOL_ROUNDS
from ai.llm import get_chat_model
from ai.prompts import CHAT_GENERATE_SYSTEM, CHAT_GRADE_SYSTEM, CHAT_REWRITE_SYSTEM
from ai.retriever import format_doc, get_retriever
from ai.schemas import RelevanceGrade
from ai.tools import read_file, retrieve_code, grep_symbol

AGENTIC_TOOLS = [retrieve_code, read_file, grep_symbol]


class ChatState(TypedDict):
    """Corrective + agentic RAG state; `messages` is the per-thread conversation
    memory, `query`/`rewrites` drive the bounded corrective loop. `query` and
    `rewrites` are reset by the caller each turn so a fresh question never inherits
    the prior turn's rewritten query from the checkpoint."""

    messages: Annotated[list[AnyMessage], add_messages]
    query: str
    documents: list[str]
    relevant: bool
    rewrites: int


def _latest_question(messages: list[AnyMessage]) -> str:
    """Return the content of the most recent human turn (the current question)."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _tool_rounds(messages: list[AnyMessage]) -> int:
    """Count tool-calling AI turns since the last human message; bounds the
    agentic generate loop so the model cannot call tools indefinitely."""
    rounds = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and message.tool_calls:
            rounds += 1
    return rounds


async def retrieve(state: ChatState, config: RunnableConfig) -> dict:
    """Repo-scoped semantic retrieval for the current (possibly rewritten) query."""
    repo = config["configurable"]["repo"]
    query = state.get("query") or _latest_question(state["messages"])
    docs = await get_retriever(repo).ainvoke(query, config)
    return {"query": query, "documents": [format_doc(d) for d in docs]}


async def grade_documents(state: ChatState, config: RunnableConfig) -> dict:
    """Corrective-RAG grade: decide whether the retrieved snippets are usable."""
    if not state["documents"]:
        return {"relevant": False}
    grader = get_chat_model(GRADER_MODEL).with_structured_output(RelevanceGrade)
    context = "\n\n".join(state["documents"])
    grade: RelevanceGrade = await grader.ainvoke(
        [
            SystemMessage(CHAT_GRADE_SYSTEM),
            HumanMessage(
                f"Question:\n{_latest_question(state['messages'])}\n\n"
                f"Retrieved snippets:\n{context}"
            ),
        ],
        config,
    )
    return {"relevant": grade.relevant}


async def rewrite_query(state: ChatState, config: RunnableConfig) -> dict:
    """Reformulate the search query when retrieval was weak (bounded by MAX_REWRITES)."""
    model = get_chat_model(GRADER_MODEL)
    response = await model.ainvoke(
        [
            SystemMessage(CHAT_REWRITE_SYSTEM),
            HumanMessage(
                f"Original question: {_latest_question(state['messages'])}\n"
                f"Current query: {state['query']}"
            ),
        ],
        config,
    )
    return {"query": str(response.content), "rewrites": state["rewrites"] + 1}


async def generate(state: ChatState, config: RunnableConfig) -> dict:
    """Tool-using answer step: grounds on retrieved context and may read_file /
    retrieve_code for fuller context, until the tool budget is spent — then it
    answers without tools so the turn always ends with a streamable text reply."""
    base = get_chat_model()
    model = base.bind_tools(AGENTIC_TOOLS) if _tool_rounds(state["messages"]) < MAX_TOOL_ROUNDS else base
    context = "\n\n".join(state["documents"]) or "No code was retrieved; use your tools."
    prompt = [SystemMessage(CHAT_GENERATE_SYSTEM.format(context=context)), *state["messages"]]
    response = await model.ainvoke(prompt, config)
    return {"messages": [response]}


def _route_after_grade(state: ChatState) -> str:
    """Relevant context → answer; weak context → rewrite (bounded), else answer anyway."""
    if state["relevant"] or state["rewrites"] >= MAX_REWRITES:
        return "generate"
    return "rewrite_query"


def _route_after_generate(state: ChatState) -> str:
    """Loop to tools while the model requests them; otherwise the turn is done."""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


def build_chat_graph() -> StateGraph:
    """Build the uncompiled corrective + agentic RAG graph; the caller compiles it
    with a checkpointer (chat memory keyed by thread_id) inside the event loop."""
    builder = StateGraph(ChatState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("grade_documents", grade_documents)
    builder.add_node("rewrite_query", rewrite_query)
    builder.add_node("generate", generate)
    builder.add_node("tools", ToolNode(AGENTIC_TOOLS))

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "grade_documents")
    builder.add_conditional_edges(
        "grade_documents", _route_after_grade, ["generate", "rewrite_query"]
    )
    builder.add_edge("rewrite_query", "retrieve")
    builder.add_conditional_edges("generate", _route_after_generate, ["tools", END])
    builder.add_edge("tools", "generate")
    return builder
