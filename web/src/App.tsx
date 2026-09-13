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
  Trash2,
  X,
} from 'lucide-react'

import { api, type Finding, type MonitoringBrief, type Run, type Target } from './api'
import './styles.css'

const SESSION_KEYS = {
  token: 'civic-canary.review-token',
  targetId: 'civic-canary.target-id',
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

function setupLabel(status: Target['setup_status']) {
  if (!status) return 'Unknown'
  return status.replaceAll('_', ' ')
}

function briefFromRuns(runs: Run[], targetId: string | undefined): MonitoringBrief | null {
  if (!targetId) return null
  const match = runs.find(
    (run) => run.target_id === targetId && run.status === 'SUCCEEDED' && run.monitoring_brief,
  )
  return match?.monitoring_brief ?? null
}

function formatBriefTime(value: string | null | undefined) {
  if (!value) return 'Not completed'
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

export default function App() {
  const [target, setTarget] = useState<Target | null>(null)
  const [targets, setTargets] = useState<Target[]>([])
  const [liveFocusId, setLiveFocusId] = useState(() => readSession(SESSION_KEYS.liveFocusId))
  const [showAdd, setShowAdd] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<Target | null>(null)
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
    const liveRows = targetRows.filter((item) => !isDemoTarget(item))
    setTargets(liveRows)
    const preferredId = readSession(SESSION_KEYS.targetId)
    setTarget((current) => {
      const preferred = preferredId
        ? liveRows.find((row) => row.target_id === preferredId)
        : undefined
      return preferred
        ?? liveRows.find((row) => row.target_id === current?.target_id)
        ?? liveRows[0]
        ?? null
    })
    setLiveFocusId((current) => {
      const next = liveRows.some((item) => item.target_id === current)
        ? current
        : liveRows[0]?.target_id ?? ''
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
    return liveRows
  }

  useEffect(() => {
    writeSession(SESSION_KEYS.token, token)
  }, [token])

  useEffect(() => {
    if (target?.target_id) writeSession(SESSION_KEYS.targetId, target.target_id)
    else writeSession(SESSION_KEYS.targetId, '')
  }, [target?.target_id])

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
  const modalOpen = showAdd || Boolean(pendingDelete)
  useEffect(() => {
    if (!findingDialogId && !runDialogId && !modalOpen) return
    const previous = document.activeElement as HTMLElement | null
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')
    const controls = () => Array.from(dialog?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],textarea,input') ?? [])
    controls()[0]?.focus()
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSelectedFinding(null)
        setSelectedRun(null)
        setShowAdd(false)
        setPendingDelete(null)
      }
      if (event.key === 'Tab') {
        const items = controls()
        const first = items[0], last = items[items.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => { document.removeEventListener('keydown', handleKey); previous?.focus() }
  }, [findingDialogId, runDialogId, modalOpen])

  const liveTargets = targets
  const selectedLive = liveTargets.find((item) => item.target_id === liveFocusId)
    ?? liveTargets.find((item) => item.target_id === target?.target_id)
    ?? liveTargets[0]
    ?? null
  const visibleFindings = findings.filter((finding) => finding.target_id === target?.target_id)
  const targetRuns = runs.filter((run) => run.target_id === target?.target_id)
  const latestBrief = briefFromRuns(runs, target?.target_id)
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

  const waitForSetup = async (targetId: string) => {
    let latest = await api.target(targetId, token)
    for (let attempt = 0; attempt < 150; attempt += 1) {
      if (latest.setup_status === 'AWAITING_CONFIRMATION' || latest.setup_status === 'FAILED' || latest.setup_status === 'ACTIVE') {
        return latest
      }
      await new Promise((resolve) => window.setTimeout(resolve, 2000))
      latest = await api.target(targetId, token)
    }
    throw new Error('Inspection is still running. Check setup status shortly.')
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
      setLiveFocusId(added.target_id)
      setMessage('Live website saved. Waiting for baseline inspection…')
      let ready = added
      if (added.setup_status === 'PENDING') {
        ready = await waitForSetup(added.target_id)
      }
      const rows = await load()
      setTarget(rows.find((row) => row.target_id === ready.target_id) ?? ready)
      if (ready.setup_status === 'FAILED') {
        throw new Error('Inspection failed. Use Retry inspection after checking worker logs.')
      }
      setMessage(
        ready.setup_status === 'AWAITING_CONFIRMATION'
          ? 'Baseline captured. Confirm the sections to monitor.'
          : 'Live website is ready.',
      )
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Website could not be added')
    } finally { setBusy(false) }
  }

  const selectTarget = (targetId: string) => {
    const next = liveTargets.find((item) => item.target_id === targetId) ?? null
    setTarget(next)
    setSections('')
    if (next) setLiveFocusId(next.target_id)
  }

  const deleteWebsite = async () => {
    if (!pendingDelete) return
    const deletedId = pendingDelete.target_id
    const deletedName = pendingDelete.name
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await api.deleteWebsite(deletedId, token)
      setPendingDelete(null)
      if (target?.target_id === deletedId) {
        setTarget(null)
        writeSession(SESSION_KEYS.targetId, '')
      }
      if (liveFocusId === deletedId) {
        setLiveFocusId('')
        writeSession(SESSION_KEYS.liveFocusId, '')
      }
      setSelectedFinding((current) => (current?.target_id === deletedId ? null : current))
      setSelectedRun((current) => (current?.target_id === deletedId ? null : current))
      await load()
      setMessage(`Removed “${deletedName}” and cleared its monitoring schedule.`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Website could not be deleted')
    } finally {
      setBusy(false)
    }
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
            <p className="kicker">Live website monitoring</p>
            <h1>Only surface changes that matter.</h1>
            <p className="lede">Add public websites, capture a trusted baseline, and review material changes before any guidance is updated.</p>
          </div>
          <div className="hero-status" aria-label="Current target status">
            <span className="pulse" aria-hidden="true" />
            <div>
              <small>Monitoring</small>
              <strong>{target ? target.name : liveTargets.length === 0 ? 'No websites yet' : 'Select a website'}</strong>
            </div>
          </div>
        </section>

        <section className="live-monitor" aria-label="Live websites">
          <div className="section-heading">
            <div>
              <p className="kicker">Monitored websites</p>
              <h2>Watch real public pages</h2>
            </div>
            <div className="feature-actions live-heading-actions">
              <button
                className="primary-button"
                type="button"
                disabled={busy}
                onClick={() => {
                  if (!token) {
                    setError('Enter your review token under Reviewer Access, then click Add Live Website.')
                    setMessage('')
                    document.getElementById('review-token')?.focus()
                    return
                  }
                  setError('')
                  setShowAdd(true)
                }}
              >
                <Globe2 aria-hidden="true" /> Add Live Website
              </button>
              <button
                className="secondary-button"
                disabled={!token || busy || !selectedLive || selectedLive.setup_status !== 'ACTIVE'}
                onClick={() => void runScan(selectedLive?.target_id)}
              >
                {busy ? <RefreshCw className="spin" aria-hidden="true" /> : <Play aria-hidden="true" />}
                {busy ? 'Working…' : 'Scan Now'}
              </button>
            </div>
          </div>

          {!token && (
            <p className="feature-hint">Review token required before adding, scanning, or deleting a live website.</p>
          )}

          {liveTargets.length === 0 ? (
            <div className="empty-state live-empty">
              <Globe2 aria-hidden="true" />
              <h3>No websites yet</h3>
              <p>Add a public HTTPS page to capture a baseline and start scheduled monitoring.</p>
              <button
                className="primary-button"
                type="button"
                disabled={busy}
                onClick={() => {
                  if (!token) {
                    setError('Enter your review token under Reviewer Access, then click Add Live Website.')
                    document.getElementById('review-token')?.focus()
                    return
                  }
                  setShowAdd(true)
                }}
              >
                <Globe2 aria-hidden="true" /> Add Live Website
              </button>
            </div>
          ) : (
            <div className="website-grid" role="list">
              {liveTargets.map((item) => {
                const selected = item.target_id === (selectedLive?.target_id ?? target?.target_id)
                return (
                  <article
                    key={item.target_id}
                    className={`website-card${selected ? ' selected' : ''}`}
                    role="listitem"
                  >
                    <button
                      type="button"
                      className="website-card-main"
                      onClick={() => selectTarget(item.target_id)}
                      aria-pressed={selected}
                    >
                      <span className="website-card-title">
                        <strong>{item.name}</strong>
                        <span className={`setup-pill status-${(item.setup_status ?? 'PENDING').toLowerCase()}`}>
                          {setupLabel(item.setup_status)}
                        </span>
                      </span>
                      <small className="website-url">{item.start_url || 'Public website'}</small>
                      <span className="website-meta">
                        Every {item.scan_frequency_minutes ?? '—'} min
                        {item.setup_status === 'ACTIVE' ? ` · Next ${formatTime(item.next_scan_at ?? null)}` : ''}
                      </span>
                    </button>
                    <div className="website-card-actions">
                      <button
                        type="button"
                        className="danger-button"
                        disabled={!token || busy}
                        onClick={() => setPendingDelete(item)}
                        aria-label={`Delete ${item.name}`}
                      >
                        <Trash2 aria-hidden="true" /> Delete
                      </button>
                    </div>
                  </article>
                )
              })}
            </div>
          )}

          {selectedLive && selectedLive.setup_status && selectedLive.setup_status !== 'ACTIVE' && (
            <div className="setup-status">
              <strong>Setup: {setupLabel(selectedLive.setup_status)}</strong>
              <p>Run: {selectedLive.setup_run_id || 'Not started'}</p>
              <p className="feature-hint">You can delete this website at any time, including while inspection is running.</p>
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
                <button disabled={!token || busy} onClick={() => void act(() => api.inspect(selectedLive.target_id, token), 'Inspection queued again.')}>
                  Retry inspection
                </button>
              )}
            </div>
          )}
          {selectedLive?.setup_status === 'ACTIVE' && (
            <p className="feature-hint">
              Next scan: {formatTime(selectedLive.next_scan_at ?? null)}. First scan captures the live baseline; later scans look for meaningful drift. You can delete this website at any time.
            </p>
          )}
        </section>

        <section className="metrics" aria-label="Summary">
          <article><small>Open decisions</small><strong>{openFindings.length}</strong><span>Need a human</span></article>
          <article><small>Latest run</small><strong>{targetRuns[0]?.status ?? '—'}</strong><span>{formatTime(targetRuns[0]?.finished_at ?? null)}</span></article>
          <article>
            <small>Scan frequency</small>
            <strong>{target ? `${target.scan_frequency_minutes ?? '—'} min` : '—'}</strong>
            <span>Public website</span>
          </article>
        </section>

        {target && (
          <section className="monitoring-brief" aria-label="Latest monitoring brief">
            <div className="section-heading">
              <div>
                <p className="kicker">Monitoring summary</p>
                <h2>Latest Monitoring Brief</h2>
              </div>
            </div>
            {latestBrief ? (
              <article className={`brief-card status-${latestBrief.status.toLowerCase()}`}>
                <header className="brief-header">
                  <div>
                    <strong>{latestBrief.website_name}</strong>
                    <small>Scanned: {formatBriefTime(latestBrief.scanned_at)}</small>
                  </div>
                  <span className={`brief-status status-${latestBrief.status.toLowerCase()}`}>
                    {latestBrief.status_label}
                  </span>
                </header>
                <p className="brief-summary">{latestBrief.executive_summary}</p>
                {latestBrief.sections_reviewed.length > 0 && (
                  <div className="brief-sections">
                    <h3>Sections reviewed</h3>
                    <ul>
                      {latestBrief.sections_reviewed.map((section) => (
                        <li key={section}>{section}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {latestBrief.changes.length > 0 ? (
                  <div className="brief-changes">
                    <h3>Detected changes</h3>
                    {latestBrief.changes.map((change) => (
                      <article key={`${change.category}-${change.title}`} className="brief-change-item">
                        <div className="brief-change-title">
                          <strong>{change.title}</strong>
                          <span className={`severity severity-${change.severity.toLowerCase()}`}>{change.severity}</span>
                        </div>
                        <p><small>Category</small> {change.category}</p>
                        <div className="before-after">
                          <div><h4>Previous</h4><p>{change.previous}</p></div>
                          <div><h4>Current</h4><p>{change.current}</p></div>
                        </div>
                        <p><small>Impact</small> {change.impact}</p>
                        <p><small>Recommended action</small> {change.recommended_action}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <p className="brief-action"><strong>Recommended action:</strong> {latestBrief.recommended_action}</p>
                )}
                {latestBrief.source_url && (
                  <a className="portal-link" href={latestBrief.source_url} target="_blank" rel="noreferrer">
                    Open source page <ArrowRight aria-hidden="true" />
                  </a>
                )}
              </article>
            ) : (
              <div className="empty-state brief-empty">
                <FileCheck2 aria-hidden="true" />
                <h3>No brief yet</h3>
                <p>After the first successful scan, Civic Canary will show a concise monitoring summary here.</p>
              </div>
            )}
          </section>
        )}

        <section className="work-grid">
          <div className="primary-column">
            <div className="section-heading">
              <div><p className="kicker">Decision queue</p><h2>Material findings</h2></div>
              <button className="secondary-button" onClick={() => void load()} aria-label="Refresh data"><RefreshCw aria-hidden="true" /> Refresh</button>
            </div>

            <div className="finding-list">
              {!target ? (
                <div className="empty-state">
                  <Globe2 aria-hidden="true" />
                  <h3>Select a website</h3>
                  <p>Choose a monitored site to review findings and recent scans.</p>
                </div>
              ) : visibleFindings.length === 0 ? (
                <div className="empty-state">
                  <FileCheck2 aria-hidden="true" />
                  <h3>No findings yet</h3>
                  <p>No decisions for this website. Confirm setup, then let scheduled monitoring run.</p>
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
              <div><p className="kicker">Monitoring history</p><h2>Recent runs</h2></div>
            </div>
            <div className="run-list">
              {target && targetRuns.map((run) => (
                <button key={run.run_id} className="run-row" onClick={() => setSelectedRun(run)}>
                  <span className="run-icon"><Clock3 aria-hidden="true" /></span>
                  <span>
                    <strong>{run.trigger_type.toLowerCase()} scan</strong>
                    <small>
                      {formatTime(run.finished_at)}
                      {run.monitoring_brief ? ` · ${run.monitoring_brief.status_label}` : ''}
                    </small>
                  </span>
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
            {target?.start_url && (
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

      {pendingDelete && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => !busy && setPendingDelete(null)}>
          <section
            className="website-modal confirm-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-live-title"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <button className="close-button" type="button" disabled={busy} onClick={() => setPendingDelete(null)} aria-label="Cancel delete"><X aria-hidden="true" /></button>
            <p className="kicker">Remove website</p>
            <h2 id="delete-live-title">Delete “{pendingDelete.name}”?</h2>
            <p>
              This removes the website from monitoring at any time—including during inspection—
              cancels in-flight scans, and deletes related findings, runs, and evidence for this
              target only. This cannot be undone.
            </p>
            <p className="feature-hint">{pendingDelete.start_url}</p>
            <div className="modal-actions">
              <button type="button" className="secondary-button" disabled={busy} onClick={() => setPendingDelete(null)}>Cancel</button>
              <button type="button" className="danger-button" disabled={busy} onClick={() => void deleteWebsite()}>
                {busy ? 'Deleting…' : 'Delete website'}
              </button>
            </div>
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
            {selectedRun.monitoring_brief ? (
              <div className="run-brief">
                <p className={`brief-status status-${selectedRun.monitoring_brief.status.toLowerCase()}`}>
                  {selectedRun.monitoring_brief.status_label}
                </p>
                <p>{selectedRun.monitoring_brief.executive_summary}</p>
                <p><strong>Recommended action:</strong> {selectedRun.monitoring_brief.recommended_action}</p>
              </div>
            ) : (
              <p>{selectedRun.summary}</p>
            )}
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
