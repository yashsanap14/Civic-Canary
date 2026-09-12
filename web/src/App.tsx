import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Check,
  Clock3,
  FileCheck2,
  Globe2,
  KeyRound,
  Play,
  RefreshCw,
  ShieldCheck,
  X,
} from 'lucide-react'

import { api, type Finding, type Run, type Target } from './api'
import './styles.css'

const SESSION_KEYS = {
  token: 'civic-canary.review-token',
  targetId: 'civic-canary.target-id',
  demoFocusId: 'civic-canary.demo-focus-id',
  liveFocusId: 'civic-canary.live-focus-id',
} as const

function readSession(key: string) {
  try {
    return window.sessionStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

function writeSession(key: string, value: string) {
  try {
    if (value) window.sessionStorage.setItem(key, value)
    else window.sessionStorage.removeItem(key)
  } catch {
    // Private mode or blocked storage should not break the dashboard.
  }
}

function formatTime(value: string | null) {
  if (!value) return 'Not completed'
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function isDemoTarget(target: Target | null | undefined) {
  if (!target) return false
  return target.kind === 'demo' || target.target_id.endsWith('-demo') || target.target_id === 'benefits-demo'
}

function targetLabel(target: Target) {
  return isDemoTarget(target) ? target.name : target.name
}

function demoPortalHref(target: Target | null) {
  if (!target) return '/portal/v1/index.html'
  const version = target.active_version ?? 'v1'
  const namespace = target.fixture_namespace?.trim()
  return namespace ? `/portal/${namespace}/${version}/index.html` : `/portal/${version}/index.html`
}

export default function App() {
  const [target, setTarget] = useState<Target | null>(null)
  const [targets, setTargets] = useState<Target[]>([])
  const [demoFocusId, setDemoFocusId] = useState(() => readSession(SESSION_KEYS.demoFocusId) || 'benefits-demo')
  const [liveFocusId, setLiveFocusId] = useState(() => readSession(SESSION_KEYS.liveFocusId))
  const [showAdd, setShowAdd] = useState(false)
  const [sections, setSections] = useState('')
  const [runs, setRuns] = useState<Run[]>([])
  const [findings, setFindings] = useState<Finding[]>([])
  const [selectedFinding, setSelectedFinding] = useState<Finding | null>(null)
  const [selectedRun, setSelectedRun] = useState<Run | null>(null)
  const [token, setToken] = useState(() => readSession(SESSION_KEYS.token))
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = async (authToken = token) => {
    const [targetRows, runRows, findingRows] = await Promise.all([
      api.targets(authToken),
      api.runs(authToken),
      api.findings(authToken),
    ])
    setTargets(targetRows)
    const preferredId = readSession(SESSION_KEYS.targetId)
    setTarget((current) => {
      const preferred = preferredId
        ? targetRows.find((row) => row.target_id === preferredId)
        : undefined
      return preferred
        ?? targetRows.find((row) => row.target_id === current?.target_id)
        ?? targetRows[0]
        ?? null
    })
    const demos = targetRows.filter((item) => isDemoTarget(item))
    const lives = targetRows.filter((item) => !isDemoTarget(item))
    setDemoFocusId((current) => {
      const next = demos.some((item) => item.target_id === current)
        ? current
        : demos[0]?.target_id ?? 'benefits-demo'
      writeSession(SESSION_KEYS.demoFocusId, next)
      return next
    })
    setLiveFocusId((current) => {
      const next = lives.some((item) => item.target_id === current)
        ? current
        : lives[0]?.target_id ?? ''
      writeSession(SESSION_KEYS.liveFocusId, next)
      return next
    })
    const focusId = new URLSearchParams(window.location.search).get('finding')
    if (focusId && authToken) setSelectedFinding(await api.finding(focusId, authToken))
    setRuns(runRows)
    setFindings(findingRows)
    setSelectedFinding((current) =>
      current ? { ...current, ...(findingRows.find((item) => item.finding_id === current.finding_id) ?? {}) } : null,
    )
    return targetRows
  }

  useEffect(() => {
    writeSession(SESSION_KEYS.token, token)
  }, [token])

  useEffect(() => {
    if (target?.target_id) writeSession(SESSION_KEYS.targetId, target.target_id)
  }, [target?.target_id])

  useEffect(() => {
    writeSession(SESSION_KEYS.demoFocusId, demoFocusId)
  }, [demoFocusId])

  useEffect(() => {
    writeSession(SESSION_KEYS.liveFocusId, liveFocusId)
  }, [liveFocusId])

  useEffect(() => {
    // Restore dashboard data after refresh, including a saved review token.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load(token).catch((reason: Error) => setError(reason.message))
  // Initial load only; later reloads are explicit after actions.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const findingDialogId = selectedFinding?.finding_id
  const runDialogId = selectedRun?.run_id
  useEffect(() => {
    if (!findingDialogId && !runDialogId && !showAdd) return
    const previous = document.activeElement as HTMLElement | null
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')
    const controls = () => Array.from(dialog?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],textarea,input') ?? [])
    controls()[0]?.focus()
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setSelectedFinding(null); setSelectedRun(null); setShowAdd(false) }
      if (event.key === 'Tab') {
        const items = controls()
        const first = items[0], last = items[items.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => { document.removeEventListener('keydown', handleKey); previous?.focus() }
  }, [findingDialogId, runDialogId, showAdd])

  const visibleFindings = findings.filter((finding) => finding.target_id === target?.target_id)
  const demoTargets = targets.filter((item) => isDemoTarget(item))
  const liveTargets = targets.filter((item) => !isDemoTarget(item))
  const selectedDemo = demoTargets.find((item) => item.target_id === demoFocusId) ?? demoTargets[0] ?? null
  const selectedLive = liveTargets.find((item) => item.target_id === liveFocusId) ?? liveTargets[0] ?? null
  const openFindings = useMemo(
    () => findings.filter((finding) => finding.status === 'OPEN' && finding.target_id === target?.target_id),
    [findings, target?.target_id],
  )

  const act = async (operation: () => Promise<unknown>, success: string) => {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await operation()
      await load()
      setMessage(success)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  const setVersion = (version: 'v1' | 'v2') => {
    if (!selectedDemo) return
    void act(() => api.setVersion(version, token, selectedDemo.target_id), `Demo portal switched to ${version.toUpperCase()}.`)
  }

  const runScan = async (targetId = selectedLive?.target_id) => {
    if (!targetId) return
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const started = await api.startRun(token, targetId)
      let run = started.run
      for (let attempt = 0; run.status === 'QUEUED' || run.status === 'RUNNING'; attempt += 1) {
        if (attempt >= 150) throw new Error('The scan is still running. Check the audit trail shortly.')
        await new Promise((resolve) => window.setTimeout(resolve, 2000))
        run = await api.run(run.run_id, token)
      }
      await load()
      if (run.status === 'FAILED') throw new Error(run.summary || 'The scan failed.')
      setMessage('Scan complete. Findings are ready for review when material changes were detected.')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  const triggerDemoChange = async () => {
    if (!selectedDemo) return
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await api.setVersion('v2', token, selectedDemo.target_id)
      const started = await api.startRun(token, selectedDemo.target_id)
      let run = started.run
      for (let attempt = 0; run.status === 'QUEUED' || run.status === 'RUNNING'; attempt += 1) {
        if (attempt >= 150) throw new Error('The demo scan is still running. Check Recent runs shortly.')
        await new Promise((resolve) => window.setTimeout(resolve, 2000))
        run = await api.run(run.run_id, token)
      }
      const rows = await load()
      const demo = rows.find((item) => item.target_id === selectedDemo.target_id) ?? null
      if (demo) setTarget(demo)
      if (run.status === 'FAILED') throw new Error(run.summary || 'The demo scan failed.')
      setMessage(`${selectedDemo.name}: V2 change triggered. Findings are ready for review.`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The demo change could not be triggered')
    } finally {
      setBusy(false)
    }
  }

  const decide = (action: 'APPROVE' | 'REJECT') => {
    if (!selectedFinding) return
    void act(
      () => api.decide(selectedFinding.finding_id, action, note, token),
      action === 'APPROVE'
        ? 'Approved draft created. No public content was changed.'
        : 'Finding rejected.',
    )
    setNote('')
  }

  const openFinding = async (finding: Finding) => {
    setSelectedFinding(finding)
    try {
      setSelectedFinding(await api.finding(finding.finding_id, token))
    } catch {
      // The list payload remains useful if a short-lived evidence URL cannot be issued.
    }
  }

  const addWebsite = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const added = await api.addWebsite({
        name: data.get('name'), public_url: data.get('public_url'),
        description: data.get('description'), monitoring_objective: data.get('objective'),
        scan_frequency_minutes: Number(data.get('frequency')), guidance_context: data.get('guidance'),
      }, token)
      setShowAdd(false)
      const rows = await load()
      setTarget(rows.find((row) => row.target_id === added.target_id) ?? added)
      setLiveFocusId(added.target_id)
      const status = added.setup_status
      setMessage(
        status === 'AWAITING_CONFIRMATION'
          ? 'Live website added and baseline captured. Confirm the sections to monitor.'
          : 'Live website added. Inspection is queued—refresh to check progress, then confirm what to monitor.',
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Website could not be added')
    } finally { setBusy(false) }
  }

  const selectTarget = (targetId: string) => {
    const next = targets.find((item) => item.target_id === targetId) ?? null
    setTarget(next)
    setSections('')
    if (next && isDemoTarget(next)) setDemoFocusId(next.target_id)
    if (next && !isDemoTarget(next)) setLiveFocusId(next.target_id)
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="identity" href="#dashboard" aria-label="Civic Canary dashboard">
          <span className="mark"><Activity aria-hidden="true" /></span>
          <span><strong>Civic Canary</strong><small>Quiet monitoring for public-service changes</small></span>
        </a>
        <div className="safety"><ShieldCheck aria-hidden="true" /> Read-only monitoring</div>
      </header>

      <main id="dashboard">
        <section className="hero">
          <div>
            <p className="kicker">Community website monitoring</p>
            <h1>Only surface changes that matter.</h1>
            <p className="lede">Civic Canary checks the portal in the background, traces the impact on community guidance, and leaves every consequential decision to a person.</p>
          </div>
          <div className="hero-status" aria-label="Current target status">
            <span className="pulse" aria-hidden="true" />
            <div><small>Monitoring</small><strong>{target ? `${targetLabel(target)} · ${isDemoTarget(target) ? 'Demo' : 'Live'}` : 'Loading target…'}</strong></div>
          </div>
        </section>

        <section className="feature-split" aria-label="Monitoring modes">
          <article className="feature-panel demo-scenario">
            <p className="kicker">Demo scenarios</p>
            <h2>Controlled V1 → V2 changes</h2>
            <p>Pick a synthetic community portal. V1 is the trusted baseline; Trigger Demo Change flips to V2 and runs the same scan → finding → approve/reject workflow.</p>
            <label htmlFor="demo-select">Demo scenario</label>
            <select
              id="demo-select"
              value={selectedDemo?.target_id ?? ''}
              onChange={(event) => selectTarget(event.target.value)}
            >
              {demoTargets.map((item) => (
                <option key={item.target_id} value={item.target_id}>{item.name}</option>
              ))}
            </select>
            <p className="feature-hint">{selectedDemo?.change_summary ?? 'Select a demo scenario to see its controlled change.'}</p>
            <div className="version-switch" aria-label="Demo portal version">
              {(['v1', 'v2'] as const).map((version) => (
                <button
                  key={version}
                  className={selectedDemo?.active_version === version ? 'active' : ''}
                  disabled={!token || busy || !selectedDemo}
                  onClick={() => setVersion(version)}
                >
                  {version.toUpperCase()}
                </button>
              ))}
            </div>
            <div className="feature-actions">
              <button
                className="secondary-button"
                disabled={!token || busy || !selectedDemo}
                onClick={() => void act(() => api.setVersion('v1', token, selectedDemo?.target_id), 'Demo reset to V1. Run Trigger Demo Change after reviewing the baseline.')}
              >
                Reset to V1
              </button>
              <button className="primary-button" disabled={!token || busy || !selectedDemo} onClick={() => void triggerDemoChange()}>
                {busy ? <RefreshCw className="spin" aria-hidden="true" /> : <Play aria-hidden="true" />}
                {busy ? 'Working…' : 'Trigger Demo Change'}
              </button>
            </div>
            <a className="portal-link" href={demoPortalHref(selectedDemo)} target="_blank" rel="noreferrer">
              Open demo portal <ArrowRight aria-hidden="true" />
            </a>
          </article>

          <article className="feature-panel live-panel">
            <p className="kicker">Live websites</p>
            <h2>Watch real public pages</h2>
            <p>Fairfax examples and any site you add use the live page as baseline—no scripted V2. Future scans detect real changes and notify only when review is needed.</p>
            <label htmlFor="website-select">Live website</label>
            <select
              id="website-select"
              value={selectedLive?.target_id ?? ''}
              onChange={(event) => selectTarget(event.target.value)}
            >
              {liveTargets.length === 0 ? (
                <option value="">No live websites yet</option>
              ) : liveTargets.map((item) => (
                <option key={item.target_id} value={item.target_id}>{item.name}</option>
              ))}
            </select>
            <div className="feature-actions">
              <button
                className="primary-button"
                type="button"
                disabled={busy}
                onClick={() => {
                  if (!token) {
                    setError('Enter your review token under Reviewer Access, then click + Add Live Website.')
                    setMessage('')
                    document.getElementById('review-token')?.focus()
                    return
                  }
                  setError('')
                  setShowAdd(true)
                }}
              >
                <Globe2 aria-hidden="true" /> + Add Live Website
              </button>
              <button
                className="secondary-button"
                disabled={!token || busy || !selectedLive || selectedLive.setup_status !== 'ACTIVE'}
                onClick={() => void runScan(selectedLive?.target_id)}
              >
                {busy ? <RefreshCw className="spin" aria-hidden="true" /> : <Play aria-hidden="true" />}
                {busy ? 'Working…' : 'Scan Live Website Now'}
              </button>
            </div>
            {!token && (
              <p className="feature-hint">Review token required before adding or scanning a live website.</p>
            )}
            {selectedLive && selectedLive.setup_status && selectedLive.setup_status !== 'ACTIVE' && (
              <div className="setup-status">
                <strong>Setup: {selectedLive.setup_status.replaceAll('_', ' ')}</strong>
                <p>Run: {selectedLive.setup_run_id}</p>
                {selectedLive.setup_status === 'AWAITING_CONFIRMATION' && <>
                  <p>The agent found these relevant headings. Accept them or edit one section per line.</p>
                  <textarea
                    aria-label="Sections to monitor"
                    value={sections || (selectedLive.recommended_sections ?? []).join('\n')}
                    onChange={(event) => setSections(event.target.value)}
                  />
                  <button
                    className="primary-button"
                    disabled={!token || busy}
                    onClick={() => void act(
                      () => api.confirm(selectedLive.target_id, (sections || (selectedLive.recommended_sections ?? []).join('\n')).split('\n').filter(Boolean), token),
                      'Monitoring confirmed. The background worker will scan on schedule.',
                    )}
                  >
                    Confirm monitoring
                  </button>
                </>}
                {selectedLive.setup_status === 'FAILED' && (
                  <button disabled={busy} onClick={() => void act(() => api.inspect(selectedLive.target_id, token), 'Inspection queued again.')}>
                    Retry inspection
                  </button>
                )}
              </div>
            )}
            {selectedLive?.setup_status === 'ACTIVE' && (
              <p className="feature-hint">Next scan: {formatTime(selectedLive.next_scan_at ?? null)}. First scan captures the real live baseline; later scans look for meaningful drift.</p>
            )}
          </article>
        </section>

        <section className="metrics" aria-label="Summary">
          <article><small>Open decisions</small><strong>{openFindings.length}</strong><span>Need a human</span></article>
          <article><small>Latest run</small><strong>{runs.find((row) => row.target_id === target?.target_id)?.status ?? '—'}</strong><span>{formatTime(runs.find((row) => row.target_id === target?.target_id)?.finished_at ?? null)}</span></article>
          <article>
            <small>{isDemoTarget(target) ? 'Portal version' : 'Scan frequency'}</small>
            <strong>{isDemoTarget(target) ? target?.active_version.toUpperCase() : `${target?.scan_frequency_minutes ?? '—'} min`}</strong>
            <span>{isDemoTarget(target) ? 'Synthetic demo' : 'Public website'}</span>
          </article>
        </section>

        <section className="work-grid">
          <div className="primary-column">
            <div className="section-heading">
              <div><p className="kicker">Decision queue</p><h2>Material findings</h2></div>
              <button className="secondary-button" onClick={() => void load()} aria-label="Refresh data"><RefreshCw aria-hidden="true" /> Refresh</button>
            </div>

            <div className="finding-list">
              {visibleFindings.length === 0 ? (
                <div className="empty-state">
                  <FileCheck2 aria-hidden="true" />
                  <h3>No findings yet</h3>
                  <p>
                    {isDemoTarget(target)
                      ? 'Use Trigger Demo Change to reveal the controlled V2 regressions.'
                      : 'No decisions for this website. Confirm setup, then let scheduled monitoring run.'}
                  </p>
                </div>
              ) : visibleFindings.map((finding) => (
                <button className="finding-card" key={finding.finding_id} onClick={() => void openFinding(finding)}>
                  <span className={`severity severity-${finding.severity.toLowerCase()}`}>{finding.severity}</span>
                  <span className="finding-copy"><strong>{finding.title}</strong><small>{finding.category.replace('_', ' ')} · {finding.affected_playbook_sections.join(', ')}</small></span>
                  <span className={`finding-state state-${finding.status.toLowerCase()}`}>{finding.status}</span>
                  <ArrowRight aria-hidden="true" />
                </button>
              ))}
            </div>

            <div className="section-heading runs-heading">
              <div><p className="kicker">Audit trail</p><h2>Recent runs</h2></div>
            </div>
            <div className="run-list">
              {runs.filter((run) => run.target_id === target?.target_id).map((run) => (
                <button key={run.run_id} className="run-row" onClick={() => setSelectedRun(run)}>
                  <span className="run-icon"><Clock3 aria-hidden="true" /></span>
                  <span><strong>{run.trigger_type.toLowerCase()} scan</strong><small>{formatTime(run.finished_at)}</small></span>
                  <span className={`run-status status-${run.status.toLowerCase()}`}>{run.status}</span>
                </button>
              ))}
            </div>
          </div>

          <aside className="demo-panel">
            <p className="kicker">Reviewer access</p>
            <h2>Connect securely</h2>
            <label htmlFor="review-token"><KeyRound aria-hidden="true" /> Review token</label>
            <input
              id="review-token"
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              autoComplete="off"
              placeholder="Required for actions"
            />
            <button className="secondary-button" disabled={!token || busy} onClick={() => void act(() => load(token), 'Connected to reviewer dashboard.')}>Connect / refresh</button>
            <button
              className="secondary-button"
              type="button"
              disabled={!token || busy}
              onClick={() => {
                setToken('')
                writeSession(SESSION_KEYS.token, '')
                setMessage('Review token cleared for this browser tab.')
                setError('')
              }}
            >
              Clear token
            </button>
            <p className="privacy-note">Saved for this browser tab only (survives refresh). Cleared when the tab closes, or use Clear token.</p>
            {target && !isDemoTarget(target) && (
              <a className="portal-link" href={target.start_url} target="_blank" rel="noreferrer">
                Open live website <ArrowRight aria-hidden="true" />
              </a>
            )}
          </aside>
        </section>

        <div className="announcements" aria-live="polite">
          {message && <p className="success-message"><Check aria-hidden="true" />{message}</p>}
          {error && <p className="error-message"><AlertTriangle aria-hidden="true" />{error}</p>}
        </div>
      </main>

      {showAdd && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setShowAdd(false)}>
          <section className="website-modal" role="dialog" aria-modal="true" aria-labelledby="add-live-title" onMouseDown={(event) => event.stopPropagation()}>
            <button className="close-button" type="button" onClick={() => setShowAdd(false)} aria-label="Close add live website"><X aria-hidden="true" /></button>
            <p className="kicker">Live website monitoring</p>
            <h2 id="add-live-title">Add Live Website</h2>
            <p>Civic Canary validates the public HTTPS URL, inspects with AgentCore Browser, analyzes with Strands, creates the first baseline, and schedules future scans.</p>
            <form className="website-form" onSubmit={(event) => void addWebsite(event)}>
              <label>Website name<input name="name" required maxLength={200} placeholder="City benefits portal" /></label>
              <label>Public URL<input name="public_url" type="url" required placeholder="https://example.org/services" /></label>
              <label className="wide-field">What to monitor<textarea name="objective" required minLength={3} maxLength={2000} placeholder="Changes that affect our clients or guidance" /></label>
              <label>Scan frequency<select name="frequency" defaultValue="1440"><option value="60">Hourly</option><option value="1440">Daily</option><option value="10080">Weekly</option><option value="43200">Monthly</option></select></label>
              <input name="description" type="hidden" value="Live public website" readOnly />
              <input name="guidance" type="hidden" value="" readOnly />
              <div className="modal-actions">
                <button type="button" className="secondary-button" onClick={() => setShowAdd(false)}>Cancel</button>
                <button className="primary-button" disabled={busy}>{busy ? 'Adding…' : 'Add and Inspect'}</button>
              </div>
            </form>
          </section>
        </div>
      )}

      {selectedFinding && (
        <div className="drawer-backdrop" role="presentation" onMouseDown={() => setSelectedFinding(null)}>
          <section className="drawer" role="dialog" aria-modal="true" aria-labelledby="finding-title" onMouseDown={(event) => event.stopPropagation()}>
            <button className="close-button" onClick={() => setSelectedFinding(null)} aria-label="Close finding"><X aria-hidden="true" /></button>
            <span className={`severity severity-${selectedFinding.severity.toLowerCase()}`}>{selectedFinding.severity}</span>
            <h2 id="finding-title">{selectedFinding.title}</h2>
            <p className="drawer-meta">{selectedFinding.category.replace('_', ' ')} · Severity {selectedFinding.severity} · {selectedFinding.status}</p>
            <h3>Previous state</h3>
            <p>{selectedFinding.before || 'Not stated on the previous page.'}</p>
            <h3>Current state</h3>
            <p>{selectedFinding.after || selectedFinding.title}</p>
            <h3>What changed</h3>
            <div className="before-after"><div><h4>Before</h4><p>{selectedFinding.before || 'See source evidence below.'}</p></div><div><h4>After</h4><p>{selectedFinding.after || selectedFinding.title}</p></div></div>
            <h3>Why it matters</h3><p>{selectedFinding.why_it_matters || 'Review the source evidence before changing guidance.'}</p>
            <h3>Who may be affected</h3><p>{selectedFinding.affected_people || 'People using this public service.'}</p>
            <p>Agent confidence: {selectedFinding.agent_confidence == null ? 'Not model-scored' : `${Math.round(selectedFinding.agent_confidence * 100)}% (self-assessed)`}</p>
            <p>Reasoning: {selectedFinding.reasoning_source ?? 'Fixture'} · Run: {selectedFinding.run_id}</p>
            <h3>Evidence</h3>
            {selectedFinding.evidence_urls?.map((url, index) => <p key={url}><a href={url} target="_blank" rel="noreferrer">Open evidence {index + 1}</a></p>)}
            <ul>{selectedFinding.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
            {selectedFinding.screenshot_urls.map((url) => (
              <img className="evidence-image" key={url} src={url} alt="Portal captured during this finding's scan" />
            ))}
            <h3>Affected community guidance</h3>
            <p>{selectedFinding.affected_playbook_sections.join(', ') || 'Needs playbook owner review'}</p>
            <h3>Current guidance</h3><p>{selectedFinding.current_guidance || 'No matching guidance supplied; proposed wording is a new draft.'}</p>
            <h3>Proposed guidance</h3>
            <pre>{selectedFinding.proposed_patch}</pre>
            {selectedFinding.status === 'APPROVED' && <>
              <p>Reviewed by {selectedFinding.reviewed_by ?? 'reviewer'} at {formatTime(selectedFinding.decided_at ?? null)}</p>
              <button className="primary-button" disabled={!token || busy} onClick={() => void act(() => api.download(selectedFinding.finding_id, token), 'Approved guidance downloaded.')}>Download approved artifact</button>
            </>}
            {selectedFinding.status === 'OPEN'  && (
              <>
                <label htmlFor="decision-note">Reviewer note</label>
                <textarea id="decision-note" value={note} onChange={(event) => setNote(event.target.value)} placeholder="Optional evidence or reason" />
                <div className="decision-actions">
                  <button className="secondary-button" disabled={!token || busy} onClick={() => decide('REJECT')}><X aria-hidden="true" /> Reject</button>
                  <button className="primary-button" disabled={!token || busy} onClick={() => decide('APPROVE')}><Check aria-hidden="true" /> Approve</button>
                </div>
              </>
            )}
          </section>
        </div>
      )}

      {selectedRun && (
        <div className="drawer-backdrop" role="presentation" onMouseDown={() => setSelectedRun(null)}>
          <section className="drawer" role="dialog" aria-modal="true" aria-labelledby="run-title" onMouseDown={(event) => event.stopPropagation()}>
            <button className="close-button" onClick={() => setSelectedRun(null)} aria-label="Close run"><X aria-hidden="true" /></button>
            <p className="kicker">Run detail</p>
            <h2 id="run-title">{selectedRun.run_id}</h2>
            <p>{selectedRun.summary}</p>
            <p>Reasoning: {selectedRun.reasoning_source ?? 'Fixture'} · Notification: {selectedRun.notification_status ?? 'Not required'}</p>
            {selectedRun.review_memo && <><p>Model: {selectedRun.review_memo.model_id}</p><p>{selectedRun.review_memo.packet?.summary}</p></>}
            <div className="timeline">
              {selectedRun.node_timings.map((node) => (
                <div key={node.node}><Check aria-hidden="true" /><span><strong>{node.node.replaceAll('-', ' ')}</strong><small>{node.duration_ms} ms</small></span></div>
              ))}
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
