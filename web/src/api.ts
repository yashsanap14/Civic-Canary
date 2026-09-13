export type Target = {
  target_id: string
  name: string
  active_version: 'v1' | 'v2'
  enabled: boolean
  kind?: 'demo' | 'live'
  fixture_namespace?: string
  change_summary?: string
  start_url?: string
  description?: string
  monitoring_objective?: string
  scan_frequency_minutes?: number
  recommended_sections?: string[]
  monitored_sections?: string[]
  setup_status?: 'ACTIVE' | 'PENDING' | 'AWAITING_CONFIRMATION' | 'FAILED'
  setup_run_id?: string
  next_scan_at?: string
}

export type NodeTiming = {
  node: string
  duration_ms: number
  status: 'SUCCEEDED' | 'FAILED'
}

export type Run = {
  run_id: string
  target_id: string
  trigger_type: 'MANUAL' | 'SCHEDULED'
  status: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED'
  started_at: string | null
  finished_at: string | null
  node_timings: NodeTiming[]
  summary: string
  error_category: string | null
  reasoning_source?: string
  review_memo?: { run_id: string; engine: string; model_id: string; packet: { summary: string } }
  notification_status?: string
}

export type Finding = {
  finding_id: string
  run_id: string
  target_id: string
  title: string
  category: string
  severity: 'LOW' | 'MEDIUM' | 'HIGH'
  materiality: 'COSMETIC' | 'MATERIAL' | 'NEEDS_REVIEW'
  evidence: string[]
  affected_playbook_sections: string[]
  proposed_patch: string
  evidence_urls: string[]
  screenshot_urls: string[]
  approved_artifact_key: string | null
  status: 'OPEN' | 'APPROVAL_PENDING' | 'APPROVED' | 'REJECTED'
  created_at: string
  decision_note: string | null
  before?: string
  after?: string
  why_it_matters?: string
  affected_people?: string
  current_guidance?: string
  agent_confidence?: number
  reasoning_source?: string
  reviewed_by?: string
  decided_at?: string
}

declare global {
  interface Window {
    CIVIC_CANARY_CONFIG?: { apiUrl?: string }
  }
}

const baseUrl = window.CIVIC_CANARY_CONFIG?.apiUrl ?? import.meta.env.VITE_API_URL ?? ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, init)
  const raw = await response.text()
  let payload: Record<string, unknown> = {}
  if (raw) {
    try {
      payload = JSON.parse(raw) as Record<string, unknown>
    } catch {
      if (!response.ok) {
        throw new Error(`Request failed with status ${response.status}`)
      }
      throw new Error('API returned a non-JSON response')
    }
  }
  if (!response.ok) {
    const detail = payload.detail
    throw new Error(
      typeof detail === 'string'
        ? detail
        : detail && typeof detail === 'object' && 'message' in detail && typeof (detail as { message: unknown }).message === 'string'
          ? (detail as { message: string }).message
          : `Request failed with status ${response.status}`,
    )
  }
  return payload as T
}

function protectedHeaders(token: string) {
  return {
    'Content-Type': 'application/json',
    'X-Review-Token': token,
  }
}

export const api = {
  target: (id: string, token = '') =>
    request<Target>(`/api/targets/${id}`, { headers: protectedHeaders(token) }),
  targets: (token = '') => request<Target[]>('/api/targets', { headers: protectedHeaders(token) }),
  runs: (token = '') => request<Run[]>('/api/runs', { headers: protectedHeaders(token) }),
  run: (runId: string, token = '') => request<Run>(`/api/runs/${runId}`, { headers: protectedHeaders(token) }),
  findings: (token = '') => request<Finding[]>('/api/findings', { headers: protectedHeaders(token) }),
  finding: (findingId: string, token = '') => request<Finding>(`/api/findings/${findingId}`, { headers: protectedHeaders(token) }),
  setVersion: (version: 'v1' | 'v2', token: string, targetId = 'benefits-demo') =>
    request<Target>('/api/demo/version', {
      method: 'POST',
      headers: protectedHeaders(token),
      body: JSON.stringify({ target_id: targetId, version }),
    }),
  startRun: (token: string, targetId = 'benefits-demo') =>
    request<{ run: Run; findings: Finding[] }>('/api/runs', {
      method: 'POST',
      headers: protectedHeaders(token),
      body: JSON.stringify({ target_id: targetId, idempotency_key: crypto.randomUUID() }),
    }),
  addWebsite: (data: Record<string, unknown>, token: string) => request<Target>('/api/targets', {
    method: 'POST', headers: protectedHeaders(token), body: JSON.stringify(data),
  }),
  deleteWebsite: (id: string, token: string) => request<{ ok: boolean; target_id: string }>(`/api/targets/${id}`, {
    method: 'DELETE', headers: protectedHeaders(token),
  }),
  confirm: (id: string, sections: string[], token: string) => request<Target>(`/api/targets/${id}/confirm`, {
    method: 'POST', headers: protectedHeaders(token), body: JSON.stringify({ monitored_sections: sections }),
  }),
  inspect: (id: string, token: string) => request<{ run: Run }>(`/api/targets/${id}/inspect`, {
    method: 'POST', headers: protectedHeaders(token),
  }),
  download: async (id: string, token: string) => {
    const response = await fetch(`${baseUrl}/api/findings/${id}/artifact`, { headers: protectedHeaders(token) })
    if (!response.ok) throw new Error('Approved artifact could not be downloaded')
    const url = URL.createObjectURL(await response.blob())
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'approved-guidance.md'
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  },
  decide: (findingId: string, action: 'APPROVE' | 'REJECT', note: string, token: string) =>
    request<{ finding: Finding; approved_artifact_key: string | null }>(
      `/api/findings/${findingId}/decision`,
      {
        method: 'POST',
        headers: protectedHeaders(token),
        body: JSON.stringify({ action, note }),
      },
    ),
}
