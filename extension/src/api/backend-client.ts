import type { Chart, GenerateRequest } from "@/types/chart";

const DEFAULT_BACKEND = "http://localhost:8000";

export function backendUrl(): string {
  const fromEnv = (import.meta as ImportMeta & { env: Record<string, string> }).env
    .VITE_BACKEND_URL;
  return (fromEnv && fromEnv.trim()) || DEFAULT_BACKEND;
}

export class BackendError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

// True when the backend handed us the Stage-1 demo chart (20 evenly spaced
// notes) instead of running the real pipeline. Detected via the pipeline
// version which the backend tags with a `-placeholder` suffix in that case.
export function isPlaceholderChart(chart: Chart): boolean {
  return chart.metadata.pipelineVersion.endsWith("-placeholder");
}

export async function generateChart(req: GenerateRequest): Promise<Chart> {
  const url = `${backendUrl()}/charts/generate`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new BackendError(
      `generateChart failed (${resp.status}): ${detail.slice(0, 200)}`,
      resp.status,
    );
  }
  return (await resp.json()) as Chart;
}

export async function pingHealth(): Promise<boolean> {
  const { ok } = await pingHealthDetailed();
  return ok;
}

export interface HealthPing {
  ok: boolean;
  error?: string;
  url: string;
}

export async function pingHealthDetailed(): Promise<HealthPing> {
  const url = `${backendUrl()}/healthz`;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    let resp: Response;
    try {
      resp = await fetch(url, { method: "GET", cache: "no-store", signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
    if (!resp.ok) return { ok: false, error: `HTTP ${resp.status}`, url };
    return { ok: true, url };
  } catch (e) {
    const msg = (e as Error).message;
    return { ok: false, error: msg.includes("abort") ? "Timed out (4s)" : msg, url };
  }
}
