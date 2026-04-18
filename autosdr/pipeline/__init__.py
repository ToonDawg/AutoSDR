"""Outreach and reply pipelines."""

from autosdr.pipeline.outreach import run_outreach_for_campaign_lead
from autosdr.pipeline.reply import process_incoming_message

__all__ = [
    "process_incoming_message",
    "run_outreach_for_campaign_lead",
]
