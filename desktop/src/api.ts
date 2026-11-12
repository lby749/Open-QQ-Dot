export type Overview = {
  service: string;
  version: number;
  read_only: boolean;
  root: string;
  friends: number;
  agents: number;
  tasks: number;
  mail_messages: number;
  memories: number;
  generated_at: string;
};

export type Friend = {
  qq: string;
  name: string;
  provider: string;
  model: string;
  voice: string;
};

export type Agent = {
  backend: string;
  display_name: string;
  transport: string;
  installed: boolean | null;
  session_support: boolean;
  capabilities: string[];
};

export type Task = {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
};

export type AccountRecord = { id: string; label: string; provider: string; base_url: string; model: string; key_id: string; effort: string; enabled: boolean };
export type StickerItem = { key: string; text: string; meaning: string; emotion: string; usage: string; by: string; is_meme: boolean | null; file: string; data_url: string };
export type StickerSnapshot = { qq: string; items: StickerItem[]; count: number };

export type AccountsPayload = { items: AccountRecord[]; discovered: { qq: string; provider: string; model: string; key_id: string }[] };

export type DeviceInfo = { id: string; name: string; scopes: string[]; created_at: number; last_seen: number; expires_at?: number | null };

export type HealthStatus = {
  adapters: { adapter: string; label: string; supported: boolean; authorized_scopes: string[]; note: string }[];
  event_count: number;
  has_real_events: boolean;
  message: string;
};

export type ExperienceSnapshot = {
  available: boolean;
  total: number;
  verified: number;
  by_state: Record<string, number>;
  by_confidence: Record<string, number>;
  recent: { id: string; task_id: string; owner: string; provider: string; model: string; problem: string; action: string; result: string; confidence: string; state: string; created_at: string }[];
  note: string;
};

export type ProviderSummary = {
  providers: { provider: string; friends: number; kind: string; installed: boolean | null }[];
  accounts: { qq: string; name: string; provider: string; model: string; api_key_id: string; effort: string; approval: string }[];
};

export type Capabilities = {
  features: Record<string, string>;
};

const BASE = import.meta.env.VITE_BRIDGE_URL || 'http://127.0.0.1:8791';

async function get<T>(path: string): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 4000);
  try {
    const response = await fetch(`${BASE}${path}`, {
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}


async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

export const api = {
  base: BASE,
  overview: () => get<Overview>('/api/overview'),
  friends: () => get<{ items: Friend[] }>('/api/friends').then((x) => x.items),
  agents: () => get<{ items: Agent[] }>('/api/agents').then((x) => x.items),
  tasks: () => get<{ items: Task[] }>('/api/tasks').then((x) => x.items),
  capabilities: () => get<Capabilities>('/api/capabilities'),
  providers: () => get<ProviderSummary>('/api/providers'),
  experiences: () => get<ExperienceSnapshot>('/api/experiences'),
  stickers: (qq: string) => get<StickerSnapshot>(`/api/stickers?qq=${encodeURIComponent(qq)}`),
  addExperienceCandidate: (problem: string, action: string, cause = '') => post('/api/experiences/candidate', { problem, action, cause }),
  searchExperiences: (q: string, owner = '') => get<{ items: ExperienceSnapshot['recent'] }>(`/api/experiences/search?q=${encodeURIComponent(q)}&owner=${encodeURIComponent(owner)}`).then((x) => x.items),
  health: () => get<HealthStatus>('/api/health-bridge'),
  accounts: () => get<AccountsPayload>('/api/accounts'),
  addAccount: (body: { label: string; provider: string; model?: string; key_id?: string; api_key?: string }) => post<AccountRecord>('/api/accounts/add', body),
  removeAccount: (id: string) => post('/api/accounts/remove', { id }),
  testAccount: (id: string) => post<{ ok: boolean; model_count?: number; models?: string[]; error?: string }>('/api/accounts/test', { id }),
  bindAccount: (qq: string, account_id: string) => post('/api/accounts/bind', { qq, account_id }),
  chatAccount: (id: string, prompt: string) => post<{ ok: boolean; content: string; model?: string; error?: string }>('/api/accounts/chat', { id, prompt }),
  routeAccount: (qq: string) => get<{ qq: string; account: AccountRecord | null; bound: boolean }>(`/api/accounts/route?qq=${encodeURIComponent(qq)}`),
  devices: () => get<{ items: DeviceInfo[] }>('/api/devices').then((x) => x.items),
  revokeDevice: (device_id: string) => post('/api/device/revoke', { device_id }),
  authorizeHealth: (adapter: string, scopes: string[]) => post('/api/health/authorize', { adapter, scopes }),
  pairingCode: () => post<{ code: string; expires_in: number }>('/api/device/pairing-code', {}),
  openLegacy: async (): Promise<string> => {
    const internals = (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    if (!internals) return 'only-available-in-tauri';
    const { invoke } = await import('@tauri-apps/api/core');
    return invoke<string>('open_legacy');
  },
  startBridge: async (): Promise<string> => {
    const internals = (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    if (!internals) return 'only-available-in-tauri';
    const { invoke } = await import('@tauri-apps/api/core');
    return invoke<string>('start_bridge');
  },
};
