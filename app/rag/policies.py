"""Structured lender policies (data/policies/*.yaml) used by the deterministic rules engine."""
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field

from app.core.config import get_settings

log = logging.getLogger(__name__)


class Program(BaseModel):
    id: str
    lender: str = ""
    name: str
    loan_type: str
    sample: bool = False
    profiles: Optional[List[str]] = None
    age_min: Optional[float] = None
    age_max: Optional[float] = None
    min_cibil: Optional[int] = None
    cibil_deviation_floor: Optional[int] = None
    min_monthly_income: Optional[float] = None
    min_job_years: Optional[float] = None
    min_total_exp_years: Optional[float] = None
    min_business_years: Optional[float] = None
    min_turnover: Optional[float] = None
    amount_min: Optional[float] = None
    amount_max: Optional[float] = None
    max_ltv: Optional[float] = None
    ltv_slabs: Optional[List[List[Optional[float]]]] = None   # [[upto_amount or null, ltv], ...]
    foir: Optional[float] = None
    roi: Optional[float] = None
    tenure_years: Optional[float] = None
    legal_allowed: Optional[List[str]] = None
    legal_case_to_case: List[str] = Field(default_factory=list)
    rera_required_under_construction: bool = False
    salary_modes: Optional[List[str]] = None
    max_unsecured_loans: Optional[int] = None
    max_enquiries_3m: Optional[int] = None
    no_bounces: bool = False
    no_overdue: bool = False
    no_settled: bool = False
    banker_rm: str = "Not available in KB"
    mobile: str = "Not available in KB"
    urban_money_code: str = "Not available in KB"
    policy_last_verified: Optional[date] = None
    source: str = ""


class PolicyFile(BaseModel):
    lender: str
    sample: bool = False
    programs: List[Program]


def parse_policy(text: str) -> PolicyFile:
    """Validate one YAML policy file; raises ValueError with a readable message."""
    try:
        data = yaml.safe_load(text)
        pf = PolicyFile.model_validate(data)
    except Exception as e:
        raise ValueError(f"Invalid policy YAML: {e}") from e
    for p in pf.programs:
        p.lender = p.lender or pf.lender
        p.sample = p.sample or pf.sample
    return pf


@lru_cache
def load_programs() -> List[Program]:
    d: Path = get_settings().policy_dir
    out: List[Program] = []
    for f in sorted(d.glob("*.y*ml")) if d.exists() else []:
        try:
            out.extend(parse_policy(f.read_text(encoding="utf-8")).programs)
        except ValueError:
            log.exception("Skipping invalid policy file %s", f.name)
    return out


def programs_for(loan_type: str) -> List[Program]:
    return [p for p in load_programs() if p.loan_type == loan_type]


def reload():
    load_programs.cache_clear()
