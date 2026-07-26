# NEma Campus Kiosk Frontend

Next.js 15 touchscreen application for the **NEma Raspberry Pi 4 Kiosk System**.

## Features
- **Local NLU Voice Interface**: Browser VAD + WebSocket communication to local Python ChromaDB NLU server (`ws://localhost:8765/ws/voice`).
- **Interactive Campus 3D Map**: Map navigation, room search, and pathfinding visualization.
- **Event Poster Showcase**: Touchscreen poster gallery, carousel, and automatic AI-extracted event detail modals.
- **Admin Upload Portal**: Live poster image uploading and status dashboard (`/upload-portal`).

## Getting Started

### Development Mode (with hot reload)
```bash
pnpm install
pnpm dev
```
Open [http://localhost:3000](http://localhost:3000) in your browser.

### Production Build & Run (Raspberry Pi 4)
```bash
pnpm build
pnpm start
```

## Environment Configuration (`.env.local`)
Create `.env.local` in the `frontend` directory:
```ini
NEXT_PUBLIC_LOCAL_SPEAKER=true
NEXT_PUBLIC_NLU_MODE=true
NEXT_PUBLIC_NLU_SERVER_URL=ws://127.0.0.1:8765/ws/voice
KIOSK_API_URL=http://127.0.0.1:8080
```
