# Connect ACGR Resource Replicator — Frontend

React + TypeScript SPA using Cloudscape Design System.

## Overview

Web-based UI providing a guided 5-step wizard workflow for discovering and replicating AWS resources associated with an Amazon Connect instance.

## Structure

```
frontend/
├── src/
│   ├── App.tsx           # App shell with Cloudscape AppLayout
│   ├── components/
│   │   ├── Wizard/       # 5-step replication wizard (and the Associate step)
│   │   ├── Inventory/    # Resource inventory table and filters
│   │   ├── Session/      # Session list and live status
│   │   ├── ContactFlows/ # Contact flow analysis
│   │   ├── Discover/     # Discover & Associate for existing target-Region resources
│   │   ├── Quota/        # Quota comparison
│   │   └── Status/       # Status badges, progress tracker
│   ├── api/              # Backend API client
│   └── types/            # TypeScript interfaces
├── package.json
└── vite.config.ts
```

## Setup

```bash
npm install
npm run dev
```
