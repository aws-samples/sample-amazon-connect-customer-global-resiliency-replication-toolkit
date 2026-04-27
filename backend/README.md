# Connect ACGR Resource Replicator — Backend

Python (FastAPI) backend for the Connect ACGR Resource Replicator tool.

## Overview

This backend orchestrates AWS API calls for discovering and replicating AWS resources associated with an Amazon Connect instance to its ACGR-paired DR region.

## Structure

```
backend/
├── main.py              # FastAPI application entry point
├── api/                 # REST API routes
├── discovery/           # Resource discovery modules
├── replication/         # Resource replication modules
├── models/              # Pydantic data models
├── aws/                 # AWS client utilities
└── tests/               # pytest + hypothesis tests
```

## Setup

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
