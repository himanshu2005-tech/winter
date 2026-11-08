# VANTA mobile web app

`vanta-mobile/` is an installable Expo web app. It is a thin, authenticated
client for the existing VANTA process; it does not run a second assistant or
duplicate action policy.

## Connection model

1. Firebase Hosting serves only the static app shell.
2. VANTA runs `mobile_gateway.py` on `127.0.0.1:8765`.
3. Tailscale Serve provides private HTTPS from the iPhone to that loopback
   service. Do not expose port 8765 directly to the public internet.
4. The first pairing verifies `AUTH_PASSWORD` locally. The password is not
   retained by the app; VANTA returns a revocable, hashed-at-rest session.

## Local development

```powershell
cd D:\winter\vanta-mobile
Copy-Item .env.example .env.local
npm run web
```

Set `EXPO_PUBLIC_VANTA_API_URL` to the HTTPS address from Tailscale Serve.

After installing and signing into Tailscale on Windows, configure the private
gateway with:

```powershell
powershell -ExecutionPolicy Bypass -File D:\winter\scripts\setup_mobile_access.ps1
```

## Production build

```powershell
cd D:\winter\vanta-mobile
npm run lint
npm run typecheck
npm run build:web
cd D:\winter
firebase deploy --only hosting
```

The deployed VANTA origin (`https://vanta-himanshu-2026.web.app`) is already
allowlisted by the gateway. Restart VANTA, open that URL in Safari, pair it
with the private Tailscale HTTPS address, then use Share > Add to Home Screen.
Set `VANTA_MOBILE_ALLOWED_ORIGINS` only if you later deploy the app at another
origin.

## Browser limitations

The PWA can record only after a user gesture and only while the page is active.
It cannot use HealthKit or maintain an always-on background microphone. Those
capabilities require a signed native iOS app later. The current design keeps
the transport/API compatible with that future client.
