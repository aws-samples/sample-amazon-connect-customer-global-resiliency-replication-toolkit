# Connect ACGR Resource Replicator — Backend

Python (FastAPI) backend for the Connect ACGR Resource Replicator tool.

## Overview

This backend orchestrates AWS API calls for discovering and replicating AWS resources associated with an Amazon Connect instance to its ACGR-paired DR region.

## Structure

```
backend/
├── main.py              # FastAPI application entry point
├── api/                 # REST API routes
├── discovery/           # Resource discovery modules (source Region, and existing target-Region resources)
├── replication/         # Resource replication modules (Lambda, Lex via ALGR, S3, streams, ...)
├── association/         # Links replicated resources to the DR Connect instance
├── audit/               # Checks which resources already exist in the target Region
├── cleanup/             # Selective cleanup of replicated resources
├── diff/                # Source vs target configuration comparison
├── quota/               # Service quota comparison between the two Regions
├── store/               # Session storage (DynamoDB)
├── models/              # Pydantic data models
├── aws/                 # AWS client utilities
└── tests/               # pytest + hypothesis tests
```

Lex bots are replicated with Amazon Lex Global Resiliency (ALGR); the backend never creates or edits a Lex bot, version or alias. See the top-level [README](../README.md#what-acgr-already-does-and-what-this-toolkit-adds).

## Setup

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
