import { Platform } from 'react-native';

import type { GatewayStatus, Pairing, VantaEvent } from '@/types';

const STORAGE_KEY = 'vanta.mobile.pairing.v1';

function storage() {
  return Platform.OS === 'web' && typeof window !== 'undefined' ? window.localStorage : null;
}

function cleanBaseUrl(value: string) {
  return value.trim().replace(/\/+$/, '');
}

function networkError(error: unknown, operation: string) {
  if (error instanceof Error && error.name === 'AbortError') {
    return new Error(
      `${operation} timed out. Keep Tailscale connected on this iPhone and keep VANTA running on the laptop.`,
    );
  }
  if (error instanceof TypeError) {
    return new Error(
      'Cannot reach the VANTA gateway. Check the private HTTPS address and confirm Tailscale is connected.',
    );
  }
  return error instanceof Error ? error : new Error(`${operation} failed.`);
}

async function fetchWithTimeout(url: string, init: RequestInit, timeoutMs: number, operation: string) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, cache: 'no-store', signal: controller.signal });
  } catch (error) {
    throw networkError(error, operation);
  } finally {
    clearTimeout(timeout);
  }
}

export function loadPairing(): Pairing | null {
  try {
    const raw = storage()?.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Pairing;
    return parsed.baseUrl && parsed.token ? parsed : null;
  } catch {
    return null;
  }
}

export function savePairing(pairing: Pairing | null) {
  const target = storage();
  if (!target) return;
  if (pairing) target.setItem(STORAGE_KEY, JSON.stringify(pairing));
  else target.removeItem(STORAGE_KEY);
}

async function jsonRequest<T>(
  pairing: Pairing,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetchWithTimeout(
    `${cleanBaseUrl(pairing.baseUrl)}${path}`,
    {
      ...init,
      headers: {
        Authorization: `Bearer ${pairing.token}`,
        ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...init.headers,
      },
    },
    20_000,
    'VANTA request',
  );
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body as T;
}

export async function pair(baseUrl: string, password: string): Promise<Pairing> {
  const normalized = cleanBaseUrl(baseUrl);
  if (!/^https:\/\//i.test(normalized)) {
    throw new Error('Use the private HTTPS address printed by Tailscale Serve.');
  }

  const health = await fetchWithTimeout(
    `${normalized}/health`,
    { method: 'GET' },
    10_000,
    'Gateway check',
  );
  if (!health.ok) {
    throw new Error(`VANTA gateway check failed (${health.status}).`);
  }

  const response = await fetchWithTimeout(
    `${normalized}/api/pair`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, device_name: "Himanshu's iPhone" }),
    },
    15_000,
    'Pairing',
  );
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Pairing failed (${response.status})`);
  const result = { baseUrl: normalized, token: body.token, expiresAt: body.expires_at };
  savePairing(result);
  return result;
}

export function getStatus(pairing: Pairing) {
  return jsonRequest<GatewayStatus>(pairing, '/api/status');
}

export function getEvents(pairing: Pairing, after = 0) {
  return jsonRequest<{ events: VantaEvent[] }>(pairing, `/api/events?after=${after}`);
}

export function sendMessage(pairing: Pairing, text: string) {
  return jsonRequest<{ queued: boolean }>(pairing, '/api/messages', {
    method: 'POST', body: JSON.stringify({ text }),
  });
}

export function settleApproval(pairing: Pairing, id: string, decision: 'approve' | 'reject') {
  return jsonRequest(pairing, `/api/approvals/${encodeURIComponent(id)}`, {
    method: 'POST', body: JSON.stringify({ decision }),
  });
}

async function uploadBlob(pairing: Pairing, path: string, blob: Blob, name: string) {
  const form = new FormData();
  form.append('file', blob, name);
  return jsonRequest(pairing, path, { method: 'POST', body: form });
}

export async function uploadUri(pairing: Pairing, uri: string, name: string, path = '/api/files') {
  const response = await fetch(uri);
  if (!response.ok) throw new Error('Could not read the selected file.');
  return uploadBlob(pairing, path, await response.blob(), name);
}

export async function downloadAttachment(pairing: Pairing, path: string, name: string) {
  const response = await fetch(`${cleanBaseUrl(pairing.baseUrl)}${path}`, {
    headers: { Authorization: `Bearer ${pairing.token}` },
  });
  if (!response.ok) throw new Error('That file is no longer available.');
  const blobUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = blobUrl;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 30_000);
}

export function connectEvents(
  pairing: Pairing,
  onEvent: (event: VantaEvent) => void,
  onState: (online: boolean) => void,
) {
  const wsUrl = cleanBaseUrl(pairing.baseUrl).replace(/^http/i, 'ws') + '/api/events/ws';
  let socket: WebSocket | null = null;
  let stopped = false;
  let retry = 1000;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;

  const open = () => {
    if (stopped) return;
    onState(false);
    socket = new WebSocket(wsUrl);
    socket.onopen = () => socket?.send(JSON.stringify({ token: pairing.token }));
    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(String(message.data)) as VantaEvent;
        if (event.kind === 'connected' || event.kind === 'pong') {
          retry = 1000;
          onState(true);
        } else onEvent(event);
      } catch {
        // Ignore malformed transport frames; visible failures arrive as events.
      }
    };
    socket.onclose = () => {
      onState(false);
      if (!stopped) {
        retryTimer = setTimeout(open, retry);
        retry = Math.min(Math.round(retry * 1.8), 30_000);
      }
    };
    socket.onerror = () => socket?.close();
  };
  open();
  return () => {
    stopped = true;
    if (retryTimer) clearTimeout(retryTimer);
    socket?.close();
  };
}
