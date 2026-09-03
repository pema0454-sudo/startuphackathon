"""Pydantic request/response models for the Bema Farm API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ApproveRequest(BaseModel):
    officer_name: str = Field(..., min_length=2, max_length=100)


class RejectRequest(BaseModel):
    officer_name: str = Field(..., min_length=2, max_length=100)
    reason: str = Field(..., min_length=2, max_length=500)


class SpikeRequest(BaseModel):
    station: str = Field(..., min_length=1, max_length=64)
    parameter: str = Field("river_level", pattern="^(river_level|rainfall_3day|rainfall_30day)$")
    value: Optional[float] = Field(None, description="Absolute new reading value")
    delta: Optional[float] = Field(None, description="Amount to add to the last reading")
    trend: Optional[str] = Field("rising", pattern="^(rising|falling|steady)$")


class ManualReadingRequest(BaseModel):
    station: str = Field(..., min_length=1, max_length=64)
    parameter: str = Field(..., pattern="^(river_level|rainfall_3day|rainfall_30day)$")
    value: float
    note: Optional[str] = Field(None, max_length=200)


class CheckedParameter(BaseModel):
    """One parameter Bema Farm checked while evaluating a policy's trigger
    (spec §2B multi-parameter triggers, §12 schema)."""

    model_config = ConfigDict(extra="allow")  # lets internal code carry the
    # comparison operator (>/<) alongside each entry for richer UI display,
    # without loosening the four fields the spec actually requires below.

    name: str
    value: Optional[float] = None
    threshold: float
    met: bool


class ClaimDraft(BaseModel):
    """Strict schema every AI- or rule-drafted claim must match (spec §12)
    before it is allowed to be saved. Anything that fails validation is
    logged and discarded — never silently coerced into the database."""

    policy_id: str
    hazard_type: str
    checked_parameters: list[CheckedParameter]
    station: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    borderline: bool
    claim_text: str = Field(..., min_length=10, max_length=1000)
    recommended_amount: float = Field(..., gt=0)
