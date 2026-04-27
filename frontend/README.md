# Connect ACGR Resource Replicator — Frontend

React + TypeScript SPA using Cloudscape Design System.

## Overview

Web-based UI providing a guided wizard workflow for discovering and replicating AWS resources associated with an Amazon Connect instance.

## Structure

```
frontend/
├── src/
│   ├── App.tsx           # App shell with Cloudscape AppLayout
│   ├── components/
│   │   ├── Wizard/       # 6-step wizard workflow
│   │   ├── Inventory/    # Resource inventory table and filters
│   │   └── Status/       # Status badges, progress tracker
│   ├── api/              # Backend API client
│   └── types/            # TypeScript interfaces
├── public/
├── package.json
└── vite.config.ts
```

## Setup

```bash
npm install
npm run dev
```
