# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

供应链智能运营系统 — a supply chain intelligent operations platform for manufacturing. Solves multi-constraint order material planning (订单物料计划) using AI: BOM explosion, substitute material matching, inventory priority consumption, demand forecasting, and multi-objective optimization (NSGA-II + Q-Learning/DQN).

**Stack**: Django 5.0 + DRF (backend), Vue 3 + Element Plus + TypeScript (frontend), SQLite (dev).

## Start / Develop

```bash
# Backend (terminal 1)
cd system
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver          # → localhost:8000

# Frontend (terminal 2)
cd frontend
npm install
npm run dev                         # → localhost:3000, proxies /api → :8000

# One-click (Windows): 一键安装并启动.bat
```

No user accounts are seeded by default. Register via `/login` page or create a superuser: `python manage.py createsuperuser`.

## Architecture

### Backend (`system/`)

```
bp_prediction_system/          # Django project (settings, root URLs)
prediction/
  api/                         # DRF ViewSets + Serializers + custom permissions
  views/                       # Django function/class-based views (pages + JSON APIs)
  models/                      # base_models (core entities), supply_chain_models, notification_models
  material_planning.py         # Core MRP engine: MaterialPlanner, MultiObjectiveOptimizer
  ai_engine.py                 # DemandForecaster (Prophet), AnomalyDetector, AutoRetrainer
  rl_agent.py                  # Q-Learning/DQN agent with SupplyChainEnvironment
  multi_objective_optimizer.py # NSGA-II Pareto-front optimization
  what_if_scenarios.py         # What-If simulator (7 scenario types)
  scheduling.py                # AdvancedScheduler + gantt chart generation
  smart_swap_engine.py         # Substitute material swap logic
  stability_analyzer.py        # Plan stability scoring
  high_performance_planner.py  # Parallel MRP for 10k+ orders
  middleware/                   # Custom: CORS, CSRF disable, audit, rate-limit, error handling
  utils/                       # analytics, safe_cache, validation, export, field_regex
  tasks.py                     # Async tasks (material planning, cache refresh)
```

**URL routing**: Three URLconfs stacked in `bp_prediction_system/urls.py`:
- `/` → `prediction.urls` (Django template views + API v1)
- `/api/` → `prediction.api.urls` (DRF ViewSet router + custom REST endpoints)
- `/api-auth/` → DRF browsable auth

**Auth**: DRF TokenAuthentication (`rest_framework.authtoken`). Token obtained via `POST /api/auth/login/`. All `/api/` endpoints require `Authorization: Token xxx` header. Frontend stores token in `localStorage`.

**Pagination**: DRF `PageNumberPagination`, `PAGE_SIZE=15`, `MAX_PAGE_SIZE=15`. Responses wrap results in `{ count, next, previous, results }`.

**Database**: SQLite (`db.sqlite3` at repo root). Migration files in `prediction/migrations/`. Use `python manage.py migrate` to apply.

### Frontend (`frontend/`)

```
src/
  components/layout/AppLayout.vue   # Shell: sidebar nav + breadcrumb + <router-view>
  views/
    material/MaterialList.vue       # CRUD list pages (pattern repeated for all entities)
    order/SalesOrderList.vue
    inventory/InventoryList.vue
    supplier/SupplierList.vue
    bom/BOMList.vue
    purchase/PurchaseList.vue
    customer/CustomerList.vue
    capacity/CapacityList.vue
    plan/MaterialPlan.vue           # Complex planning dashboard (different pattern)
    screen/VisualScreen.vue         # Full-screen KPI dashboard
    ai/AIAnalysis.vue               # AI analysis page
    system/DataImport.vue, AuditLog.vue, HelpCenter.vue
  api/                             # Axios request functions per domain
    request.ts                      # Axios instance, interceptors, token injection, retry
    index.ts                        # Re-exports all API functions
  router/index.ts                  # Vue Router config + auth guard
  stores/user.ts                   # Pinia user store
  types/api.ts                     # All TypeScript interfaces
  assets/styles/global.scss        # Dark theme, Element Plus CSS variable overrides
```

**Key patterns**:
- All list pages (MaterialList, BOMList, InventoryList, etc.) share the identical template structure: `.page-header` (title + "新增" button) → `search-card` (inline filter form) → `table-card` (toolbar + `el-table` + `el-pagination`)
- API calls use the typed `request` wrapper from `api/request.ts`. Base URL is `/api`, auth token injected via interceptor.
- Vite dev server proxies `/api/*` → `http://localhost:8000`
- `KeepAlive` caches up to 10 page components for tab-like navigation feel.

### Dark Theme / Global Styles

The app uses a dark theme. Global `:root` CSS variables in `global.scss` override Element Plus defaults:
- `--el-fill-color-blank: rgba(255,255,255,0.05)` — input/select backgrounds
- `--el-bg-color-overlay: #1a1e29` — dropdown panels
- `--el-text-color-primary: #E8EAED`
- `--el-text-color-placeholder: #78849E`

**Important**: `global.scss` is plain CSS (no scoping). Never use `:deep()` in it — that's Vue SFC syntax that fails silently in global CSS.

### Data Import

CSV files in `数据集/` directory. Backend auto-detects import type from column headers using regex patterns in `utils/field_regex.py`. File upload max 50MB. Import pipeline: `ImportDataView` → `import_csv_data()` → type-specific `import_*_data()` functions that use `update_or_create()` for upsert.

### Layout / Scrolling

The layout chain for proper scroll containment:
```
html, body { height: 100%; overflow: hidden; }
  └── .app-layout { height: 100%; display: flex; overflow: hidden; }
        ├── .sidebar (240px / 64px collapsed)
        └── .main-content { flex: 1; min-height: 0; overflow-y: auto; }
```

**`min-height: 0` is critical** — without it, flex items default to `min-height: auto` which prevents `overflow-y: auto` from triggering scroll.

## Key Files Reference

| Concern | Files |
|---------|-------|
| API routes (REST) | `system/prediction/api/urls.py` |
| API routes (views/v1) | `system/prediction/urls.py` |
| MRP core engine | `system/prediction/material_planning.py` |
| AI forecast + anomaly | `system/prediction/ai_engine.py` |
| RL agent | `system/prediction/rl_agent.py` |
| NSGA-II optimizer | `system/prediction/multi_objective_optimizer.py` |
| What-If simulator | `system/prediction/what_if_scenarios.py` |
| All data models | `system/prediction/models/` |
| Frontend API layer | `frontend/src/api/` |
| Dark theme globals | `frontend/src/assets/styles/global.scss` |
| App shell layout | `frontend/src/components/layout/AppLayout.vue` |
| Vue routes | `frontend/src/router/index.ts` |
| API docs (Chinese) | `docs/API接口文档.md` |
| PRD / architecture | `.trae/documents/`, `docs/详细设计文档.md` |
