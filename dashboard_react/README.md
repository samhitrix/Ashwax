# AshwaX dashboard

React 19 + TypeScript + Vite 8 + Tailwind 4 UI for the AshwaX trading agent. It reads
everything from the API served by `src/api/server.py` (started by `launcher.py`); it has no
mock data.

```bash
npm install
npm run dev      # dev server on http://localhost:5173, proxies /api to the backend
npm run build    # production bundle in dist/
```

Requires Node 20.19+ or 22+.
