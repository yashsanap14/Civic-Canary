import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Check,
  Clock3,
  FileCheck2,
  KeyRound,
  Play,
  RefreshCw,
  ShieldCheck,
  X,
} from 'lucide-react'

import { api, type Finding, type Run, type Target } from './api'
import './styles.css'

function formatTime(value: string | null) {
  if (!value) return 'Not completed'
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

export default function App() {
  const [target, setTarget] = useState<Target | null>(null)
  const [targets, setTargets] = useState<Target[]>([])
  const [showAdd, setShowAdd] = useState(false)
  const [sections, setSections] = useState('')
  const [runs, setRuns] = useState<Run[]>([])
  const [findings, setFindings] = useState<Finding[]>([])
  const [selectedFinding, setSelectedFinding] = useState<Finding | null>(null)
  const [selectedRun, setSelectedRun] = useState<Run | null>(null)
  const [token, setToken] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = async () => {
    const [targetRows, runRows, findingRows] = await Promise.all([
      api.targets(token),
      api.runs(token),
      api.findings(token),
    ])
    setTargets(targetRows)
    setTarget((current) => targetRows.find((row) => row.target_id === current?.target_id) ?? targetRows[0] ?? null)
    const focusId = new URLSearchParams(window.location.search).get('finding')
    if (focusId && token) setSelectedFinding(await api.finding(focusId, token))
    setRuns(runRows)
    setFindings(findingRows)
    setSelectedFinding((current) =>
      current ? { ...current, ...(findingRows.find((item) => item.finding_id === current.finding_id) ?? {}) } : null,
    )
  }

  useEffect(() => {
    // The asynchronous load synchronizes the dashboard with the external API.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load().catch((reason: Error) => setError(reason.message))
  // Initial public/local load only. Protected reload is explicit after entering a token.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const findingDialogId = selectedFinding?.finding_id
  const runDialogId = selectedRun?.run_id
  useEffect(() => {
    if (!findingDialogId && !runDialogId) return
    const previous = document.activeElement as HTMLElement | null
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')
    const controls = () => Array.from(dialog?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],textarea,input') ?? [])
    controls()[0]?.focus()
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setSelectedFinding(null); setSelectedRun(null) }
      if (event.key === 'Tab') {
        const items = controls()
        const first = items[0], last = items[items.length - 1]
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => { document.removeEventListener('keydown', handleKey); previous?.focus() }
  }, [findingDialogId, runDialogId])

  const visibleFindings = findings.filter((finding) => finding.target_id === target?.target_id)
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

  const setVersion = (version: 'v1' | 'v2') =>
    act(() => api.setVersion(version, token, target?.target_id), `Demo portal switched to ${version.toUpperCase()}.`)

  const runScan = async () => {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      const started = await api.startRun(token, target?.target_id)
      let run = started.run
      for (let attempt = 0; run.status === 'QUEUED' || run.status === 'RUNNING'; attempt += 1) {
        if (attempt >= 150) throw new Error('The scan is still running. Check the audit trail shortly.')
        await new Promise((resolve) => window.setTimeout(resolve, 2000))
        run = await api.run(run.run_id, token)
      }
      await load()
      if (run.status === 'FAILED') throw new Error(run.summary || 'The scan failed.')
      setMessage('Scan complete. Findings are ready for review.')
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

  const addWebsite = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    try {
      const added = await api.addWebsite({
        name: data.get('name'), public_url: data.get('public_url'),
        description: data.get('description'), monitoring_objective: data.get('objective'),
        scan_frequency_minutes: Number(data.get('frequency')), guidance_context: data.get('guidance'),
      }, token)
      setTarget(added)
      setShowAdd(false)
      await load()
      setMessage('Inspection queued. The background worker will capture the baseline and recommend sections. Refresh to check progress.')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Website could not be added')
    } finally { setBusy(false) }
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
            <div><small>Monitoring</small><strong>{target?.name ?? 'Loading target…'}</strong></div>
          </div>
        </section>

        <section className="monitor-setup" aria-label="Website monitoring setup">
          <label htmlFor="website-select">Monitored website</label>
          <select id="website-select" value={target?.target_id ?? ''} onChange={(event) => {
            const next = targets.find((item) => item.target_id === event.target.value) ?? null
            setTarget(next)
            setSections('')
          }}>
            {targets.map((item) => <option key={item.target_id} value={item.target_id}>{item.name}</option>)}
          </select>
          <button className="primary-button" disabled={!token || busy} onClick={() => setShowAdd(!showAdd)}>Add Website</button>
          {showAdd && <form className="website-form" onSubmit={(event) => void addWebsite(event)}>
            <label>Website name<input name="name" required maxLength={200} /></label>
            <label>Public HTTPS URL<input name="public_url" type="url" required placeholder="https://example.org/services" /></label>
            <label>Description or category<input name="description" maxLength={1000} /></label>
            <label>What should we monitor?<textarea name="objective" required minLength={3} maxLength={2000} placeholder="Changes that affect our clients or guidance" /></label>
            <label>Scan frequency<select name="frequency" defaultValue="1440"><option value="5">Every 5 minutes (demo)</option><option value="60">Hourly</option><option value="1440">Daily</option><option value="10080">Weekly</option></select></label>
            <label>Optional nonprofit guidance<textarea name="guidance" maxLength={20000} /></label>
            <button className="primary-button" disabled={busy}>Inspect website</button>
          </form>}
          {target && target.setup_status && target.setup_status !== 'ACTIVE' && <div className="setup-status">
            <strong>Setup: {target.setup_status.replaceAll('_', ' ')}</strong>
            <p>Run: {target.setup_run_id}</p>
            {target.setup_status === 'AWAITING_CONFIRMATION' && <>
              <p>The agent found these relevant headings. Accept them or edit one section per line.</p>
              <textarea aria-label="Sections to monitor" value={sections || (target.recommended_sections ?? []).join('\n')} onChange={(event) => setSections(event.target.value)} />
              <button className="primary-button" disabled={!token || busy} onClick={() => void act(
                () => api.confirm(target.target_id, (sections || (target.recommended_sections ?? []).join('\n')).split('\n').filter(Boolean), token),
                'Monitoring confirmed. The background worker will scan on schedule.',
              )}>Confirm monitoring</button>
            </>}
            {target.setup_status === 'FAILED' && <button disabled={busy} onClick={() => void act(() => api.inspect(target.target_id, token), 'Inspection queued again.')}>Retry inspection</button>}
          </div>}
          {target?.setup_status === 'ACTIVE' && <p>Next scan: {formatTime(target.next_scan_at ?? null)}. Unchanged scans stay quiet.</p>}
        </section>

        <section className="metrics" aria-label="Summary">
          <article><small>Open decisions</small><strong>{openFindings.length}</strong><span>Need a human</span></article>
          <article><small>Latest run</small><strong>{runs.find((row) => row.target_id === target?.target_id)?.status ?? '—'}</strong><span>{formatTime(runs.find((row) => row.target_id === target?.target_id)?.finished_at ?? null)}</span></article>
          <article><small>Portal version</small><strong>{target?.active_version.toUpperCase() ?? '—'}</strong><span>{target?.target_id === 'benefits-demo' ? 'Synthetic demo' : 'Public website'}</span></article>
        </section>

        <section className="work-grid">
          <div className="primary-column">
            <div className="section-heading">
              <div><p className="kicker">Decision queue</p><h2>Material findings</h2></div>
              <button className="secondary-button" onClick={() => void load()} aria-label="Refresh data"><RefreshCw aria-hidden="true" /> Refresh</button>
            </div>

            <div className="finding-list">
              {visibleFindings.length === 0 ? (
                <div className="empty-state"><FileCheck2 aria-hidden="true" /><h3>No findings yet</h3><p>{target?.target_id === 'benefits-demo' ? 'Switch to V2 and run a scan to reveal the controlled regressions.' : 'No decisions for this website. Confirm setup, then let scheduled monitoring run.'}</p></div>
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
            <p className="kicker">Demo controls</p>
            <h2>Simulate a portal change</h2>
            <p>V1 is the trusted baseline. V2 adds one document requirement, breaks Spanish guidance, and removes a form label.</p>

            <label htmlFor="review-token"><KeyRound aria-hidden="true" /> Review token</label>
            <input id="review-token" type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" placeholder="Required for actions" />
            <button className="secondary-button" disabled={!token || busy} onClick={() => void act(load, 'Connected to reviewer dashboard.')}>Connect / refresh</button>
            <p className="privacy-note">Held in memory only—never saved in this browser.</p>

            {target?.target_id === 'benefits-demo' && <div className="version-switch" aria-label="Portal version">
              {(['v1', 'v2'] as const).map((version) => (
                <button key={version} className={target?.active_version === version ? 'active' : ''} disabled={!token || busy} onClick={() => void setVersion(version)}>
                  {version.toUpperCase()}
                </button>
              ))}
            </div>}
            <button className="primary-button" disabled={!token || busy || (target?.setup_status !== undefined && target.setup_status !== 'ACTIVE')} onClick={() => void runScan()}>
              {busy ? <RefreshCw className="spin" aria-hidden="true" /> : <Play aria-hidden="true" />}
              {busy ? 'Working…' : 'Run Canary scan'}
            </button>
            <a className="portal-link" href={target?.target_id === 'benefits-demo' ? `/portal/${target?.active_version ?? 'v1'}/index.html` : target?.start_url} target="_blank" rel="noreferrer">
              Open monitored website <ArrowRight aria-hidden="true" />
            </a>
          </aside>
        </section>

        <div className="announcements" aria-live="polite">
          {message && <p className="success-message"><Check aria-hidden="true" />{message}</p>}
          {error && <p className="error-message"><AlertTriangle aria-hidden="true" />{error}</p>}
        </div>
      </main>

      {selectedFinding && (
        <div className="drawer-backdrop" role="presentation" onMouseDown={() => setSelectedFinding(null)}>
          <section className="drawer" role="dialog" aria-modal="true" aria-labelledby="finding-title" onMouseDown={(event) => event.stopPropagation()}>
            <button className="close-button" onClick={() => setSelectedFinding(null)} aria-label="Close finding"><X aria-hidden="true" /></button>
            <span className={`severity severity-${selectedFinding.severity.toLowerCase()}`}>{selectedFinding.severity}</span>
            <h2 id="finding-title">{selectedFinding.title}</h2>
            <p className="drawer-meta">{selectedFinding.category.replace('_', ' ')} · {selectedFinding.status}</p>
            <h3>What changed</h3>
            <div className="before-after"><div><h4>Before</h4><p>{selectedFinding.before || 'See source evidence below.'}</p></div><div><h4>After</h4><p>{selectedFinding.after || selectedFinding.title}</p></div></div>
            <h3>Why it matters</h3><p>{selectedFinding.why_it_matters || 'Review the source evidence before changing guidance.'}</p>
            <h3>Who may be affected</h3><p>{selectedFinding.affected_people || 'People using this public service.'}</p>
            <p>Agent confidence: {selectedFinding.agent_confidence == null ? 'Not model-scored' : `${Math.round(selectedFinding.agent_confidence * 100)}% (self-assessed)`}</p>
            <p>Reasoning: {selectedFinding.reasoning_source ?? 'Fixture'} · Run: {selectedFinding.run_id}</p>
            <h3>Source evidence</h3>
            {selectedFinding.evidence_urls?.map((url, index) => <p key={url}><a href={url} target="_blank" rel="noreferrer">Open evidence {index + 1}</a></p>)}
            <ul>{selectedFinding.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
            {selectedFinding.screenshot_urls.map((url) => (
              <img className="evidence-image" key={url} src={url} alt="Portal captured during this finding's scan" />
            ))}
            <h3>Affected guidance</h3>
            <p>{selectedFinding.affected_playbook_sections.join(', ')}</p>
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
                  <button className="primary-button" disabled={!token || busy} onClick={() => decide('APPROVE')}><Check aria-hidden="true" /> Approve draft</button>
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
