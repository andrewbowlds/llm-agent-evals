"""Evaluation harness for EDP Realty's production LLM agents.

The agent under test is the rental lead autoresponder: it reads an inbound
rental inquiry, looks up the unit in Firestore, and drafts a reply. It runs
unsupervised twice a day against real prospective tenants, which means a bad
output is not a bad demo -- it is a fair housing exposure.

This package treats that agent the way you would treat any other production
system: with a fixed test set, deterministic assertions, and a gate that
blocks a release when a safety criterion regresses.
"""

__version__ = "0.1.0"
