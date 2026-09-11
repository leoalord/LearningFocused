import operator
from typing import Annotated, List, TypedDict, Optional, Literal, Union, Dict, Any
from pydantic import BaseModel, Field
from langchain.messages import AnyMessage

class ResearchTopic(BaseModel):
    topic: str = Field(description="The specific sub-topic or question to research")
    description: str = Field(description="Detailed description of what information is needed")

class ResearchBrief(BaseModel):
    topics: List[ResearchTopic] = Field(description="List of research topics to investigate")
    summary: str = Field(description="High-level summary of the research goal")
    research_brief: str = Field(description="The text of the research brief")

class ClarifyWithUser(BaseModel):
    need_clarification: bool = Field(description="Whether clarification is needed")
    question: str = Field(description="The clarifying question to ask the user")
    verification: str = Field(description="Verification message if no clarification is needed")

class ResearchQuestion(BaseModel):
    research_brief: str = Field(description="The research brief")

class AgentInputState(TypedDict):
    messages: List[AnyMessage]

class AgentState(TypedDict):
    """The overall state of the research agent."""
    messages: List[AnyMessage]
    research_brief: str
    supervisor_messages: List[AnyMessage]
    research_iterations: int
    notes: List[str]
    final_report: str

class SupervisorState(TypedDict):
    supervisor_messages: List[AnyMessage]
    research_iterations: int
    research_brief: str
    notes: List[str]

class ResearcherState(TypedDict):
    """State for the researcher subgraph (worker)."""
    researcher_messages: List[AnyMessage]
    tool_call_iterations: int
    research_topic: str

class ResearcherOutputState(TypedDict):
    compressed_research: str
    raw_notes: List[str]

class ConductResearch(BaseModel):
    research_topic: str = Field(description="The specific topic to research")

class ResearchComplete(BaseModel):
    """Marker to indicate research is complete."""
    pass
