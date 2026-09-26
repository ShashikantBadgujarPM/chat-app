# Chat App — frontend

React + TypeScript + Vite. In M00 this is only a scaffold that proves the dev proxy
reaches the backend. Features arrive from M02 onward.

```sh
npm install
npm run dev        # http://localhost:5173, proxies /api and /ws to the backend
npm run typecheck
npm run lint
npm run build
```

The proxy target defaults to `http://localhost:8000`. Docker Compose sets
`VITE_API_PROXY_TARGET=http://api:8000`.
