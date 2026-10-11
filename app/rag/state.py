import operator
from typing import Annotated, Dict, List, Literal

from typing_extensions import TypedDict
from pydantic import BaseModel, Field
from langchain_core.documents import Document

STATUSES = ("Strong Match", "Possible - Credit Check Required", "Not Suitable", "More Information Required")


class EvidenceGrade(BaseModel):
    grade: Literal["good", "weak"]


class LenderOption(BaseModel):
    lender: str
    program: str
    status: str = ""


class Explanation(BaseModel):
    """LLM output on the structured path: wording only. Status, lender, contacts and the list of concerns
    come from the rules engine."""
    summary: str = Field(description="Two sentences for the channel partner")
    why_suitable: List[str] = Field(description="Short reasons the top program fits, citing the passed checks")
    next_steps: List[str] = Field(description="Concrete actions to close the open checks")


class Recommendation(BaseModel):
    """LLM output on the text-only path (lender has no structured policy yet)."""
    status: Literal["Strong Match", "Possible - Credit Check Required", "Not Suitable", "More Information Required"]
    top_lender: str = Field(description="Lender name from the evidence, or 'None'")
    program: str
    indicative_eligibility: str
    why_suitable: List[str]
    concerns_or_deviations: List[str]
    banker_rm: str = Field(description="Copy exactly from evidence, else 'Not available in KB'")
    mobile: str = Field(description="Copy exactly from evidence, else 'Not available in KB'")
    urban_money_code: str = Field(description="Copy exactly from evidence, else 'Not available in KB'")
    alternative_lenders: List[LenderOption]
    policy_last_verified: str = Field(description="Copy from evidence, else 'Unknown'")


class WebGuidance(BaseModel):
    summary: str
    general_criteria: List[str]


class Extracted(BaseModel):
    fields: Dict[str, str]


class AgentState(TypedDict):
    case: dict
    missing: List[str]
    eligibility: dict
    flags: List[str]
    matches: List[dict]
    query: str
    kb_docs: List[Document]
    web_results: str
    kb_grade: str
    retry_count: int
    evidence_source: str
    result: dict
    trace: Annotated[List[str], operator.add]
