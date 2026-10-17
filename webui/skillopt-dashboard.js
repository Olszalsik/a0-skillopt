/**
 * SkillOpt dashboard JS — runs in the browser context.
 *
 * Loaded via /a0/usr/plugins/skillopt/extensions/webui/page-head/skillopt-head.html
 * which is included in the WebUI <head> by the framework's extension loader.
 *
 * Exposes a small `window.SkillOptDashboard` namespace that the
 * WebUI config page and the sidebar card can hook into.
 *
 * v1.4.0-Dev (Day-5 item 7): adds per-cycle dashboard endpoints.
 *   - cycles(limit, skill, sinceTs, outcome)      -> POST /cycles
 *   - cycle(cycleId)                              -> POST /cycle/<id>
 *   - auditLog(limit, skill, passed)              -> POST /audit_log
 *   - renderCycleHistory(cyclesResult)            -> JSON payload the
 *     WebUI server (templates/config.html) can mount as a table.
 * Backwards-compat: every existing method (status/sleep/adopt/config/
 * setConfig/waitForCycle) is preserved exactly. The vanilla-JS + fetch
 * pattern is unchanged. No external deps, no new globals.
 *
 * v1.8.34: informed approval + dual mode on the DASHBOARD surface too.
 * Previously this file's `renderStagedProposals` mapped a /staged response
 * down to skill/proposal_id/size/lift/gate_reason and DROPPED everything
 * else - so while the settings page grew an intent badge, a rationale and a
 * diff, this surface stayed a name-and-byte-count card. Two approval
 * surfaces, one of them still blind, is the exact problem the feature set
 * out to remove. Added, all pure and defensive:
 *   - proposal(id, full)                    -> POST /proposal
 *   - mode({mode|pause|adoptedToday})       -> POST /mode
 *   - approveGovernance(skill, ok, id)      -> POST /governance_approve
 *                                              (binds the decision to a
 *                                              proposal, not just a skill)
 *   - renderProposalSummary(summary)        -> flattened summary primitives
 *   - renderPendingApproval(map)            -> per-skill pending proposal
 *   - renderEvolutionMode(result, loop)     -> Evolution mode card values
 */

(function () {
  'use strict';

  const ENDPOINT_BASE = '/api/plugins/skillopt';

  let _csrfToken = null;

  async function getCsrfToken() {
    if (_csrfToken) return _csrfToken;
    const tr = await fetch('/api/csrf_token', {
      method: 'GET',
      credentials: 'same-origin',
      headers: { 'accept': 'application/json' },
    });
    if (!tr.ok) throw new Error('CSRF fetch HTTP ' + tr.status);
    const td = await tr.json();
    _csrfToken = td.token || td.csrf_token || null;
    return _csrfToken;
  }

  async function call(path, opts = {}) {
    const body = opts.body ? JSON.stringify(opts.body) : '{}';
    const headers = { 'content-type': 'application/json' };
    const token = await getCsrfToken().catch(() => null);
    if (token) headers['x-csrf-token'] = token;
    const r = await fetch(ENDPOINT_BASE + path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: headers,
      body: body,
    });
    if (r.status === 403) {
      // Token may have rotated (e.g. server restart) — refetch once and retry.
      _csrfToken = null;
      const token2 = await getCsrfToken().catch(() => null);
      if (token2) headers['x-csrf-token'] = token2;
      const r2 = await fetch(ENDPOINT_BASE + path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: headers,
        body: body,
      });
      if (!r2.ok) throw new Error('HTTP ' + r2.status);
      return r2.json();
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  /**
   * Build a query-string body the API endpoint expects, dropping
   * empty / null / undefined values so the server-side default kicks
   * in. Returns a plain object suitable for JSON.stringify.
   */
  function _qsBody(parts) {
    const out = {};
    for (const [k, v] of Object.entries(parts || {})) {
      if (v === undefined || v === null) continue;
      if (typeof v === 'string' && v === '') continue;
      out[k] = v;
    }
    return out;
  }

  const SkillOptDashboard = {
    status() { return call('/status'); },
    sleep(verb, skill) { return call('/sleep', { body: { verb, skill: skill || '' } }); },
    adopt() { return call('/adopt'); },
    config() { return call('/config'); },
    setConfig(overrides) { return call('/config', { body: overrides }); },

    /**
     * v1.7.0 (Solution C, Phase C3): human-in-the-loop adopt UI.
     *   - staged()                      -> POST /staged : list staged
     *     proposals with gate evidence (skill, proposal_id, lift_pp,
     *     n_held_out, gate_reason, last_outcome, diff_summary).
     *   - adoptProposal(id)             -> POST /adopt  : adopt a specific
     *     staged proposal by its stem (falls back to latest when id empty).
 *   - reject(id, reason)             -> POST /reject : record a human
 *     reject decision AND move the proposal out of the pending queue into
 *     staging/rejected/ - the document is preserved, not deleted. Audit-only
 *     was true only until v1.8.36; before that the drain could re-adopt the
 *     rejected document. Since v1.8.46 a partial failure reports ok:false,
 *     because the proposal may still be adoptable and the operator must be
 *     told rather than left to assume otherwise.
     *   - rollback(skill)               -> POST /rollback: restore the
     *     most recent whole-file _default snapshot for <skill>.
     *   - renderStagedProposals(result)  -> pure fn: shape a /staged
     *     response into rows the WebUI config page can render.
     */
    staged() { return call('/staged'); },

    /**
     * v1.8.34: a human decision that names the proposal it was made about.
     * `proposal_id` is what lets the surface tell a PENDING decision from a
     * DECIDED one and stop re-prompting forever; omitting it still works
     * (the ledger row then names only the skill) but the card can only show
     * a skill name.
     */
    approveGovernance(skill, approved, proposalId) {
      const body = {
        skill: String(skill || ''),
        // v1.8.48 (audit U7): pass the value through UNTOUCHED.
        // `api/governance_approve.py` exists precisely to accept an explicit
        // denial ("false"/"no"/"off"/"0"), because the F10 defect was
        // `bool("false") is True`. `!!approved` performed that same coercion
        // one layer earlier, so `governanceApprove('x', 'false')` sent
        // `approved: true` - an approval for a caller who asked to refuse.
        // One coercion dialect in the plugin: the server's.
        approved,
        decided_by: 'user',
      };
      if (proposalId) body.proposal_id = String(proposalId);
      return call('/governance_approve', { body });
    },

    /**
     * v1.8.34: one staged proposal's full summary + the uncapped diff.
     * `full: true` re-renders the diff at the hard ceiling; the list view
     * stays bounded so a queue of proposals remains cheap to fetch.
     * `proposal_id` is a proposal STEM, never a path - the route resolves it
     * by equality against the staged queue and rejects anything else.
     */
    proposal(proposalId, full) {
      const body = { proposal_id: String(proposalId || '') };
      if (full) body.full = true;
      return call('/proposal', { body });
    },

    /**
     * v1.8.34: the operating mode. Read with no args; write with
     * {mode: 'manual'|'autonomous'} or {pause: true|false} for the
     * `.autonomous_pause` kill switch. The response carries the RESOLVED
     * answer plus `source`, so a consumer can show where the mode came from
     * (canonical key vs legacy alias vs shipped default) instead of the mode
     * appearing to change on its own.
     */
    mode(opts) {
      const o = opts || {};
      const body = {};
      if (o.mode) body.mode = String(o.mode);
      // v1.8.50 (audit G2): the kill-switch value is passed through untouched.
    // `!!o.pause` meant a caller writing `pause: 'false'` PAUSED autonomy - the
    // F10 coercion class one layer below the route that now rejects it. One
    // dialect, owned by the server.
    if (o.pause !== undefined && o.pause !== null) body.pause = o.pause;
      if (o.adoptedToday !== undefined && o.adoptedToday !== null) {
        body.adopted_today = Number(o.adoptedToday) || 0;
      }
      return call('/mode', { body });
    },

    adoptProposal(proposalId) {
      const body = proposalId ? { proposal_id: String(proposalId) } : {};
      return call('/adopt', { body });
    },
    reject(proposalId, reason) {
      return call('/reject', { body: { proposal_id: String(proposalId || ''),
                                        reason: String(reason || '') } });
    },
    rollback(skill) {
      return call('/rollback', { body: { skill: String(skill || '') } });
    },

    /**
     * v1.7.0 (Phase C4): governance — auto opt-in with guardrails.
     *   - governanceStatus(skill?)   -> POST /governance_status : full
     *     block (no skill) or per-skill policy + markers + eligibility +
     *     last human decision (with skill).
     *   - governanceApprove(skill, approved, decidedBy?) -> POST
     *     /governance_approve : record a human decision (the row
     *     check_skill_eligible step 7 reads); approved=true also touches
     *     the .skillopt.optin marker so the ledger and marker agree.
     */
    governanceStatus(skill) {
      const body = skill ? { skill: String(skill || '') } : {};
      return call('/governance_status', { body });
    },
    governanceApprove(skill, approved, decidedBy) {
      return call('/governance_approve', {
        body: {
          skill: String(skill || ''),
          // v1.8.48 (audit U7): see `approveGovernance` - no client-side
          // `!!`, so an explicit denial stays a denial.
          approved,
          decided_by: String(decidedBy || 'user'),
        },
      });
    },

  /**
   * v1.8.8 (open question 5): one-click pause/resume.
   * - governancePause(skill, hours) -> POST /governance_pause
   *   writes the .skillopt.pause_until marker (default 24h, clamped
   *   0.25h..720h) so the governance gate skips the skill at once.
   * - governanceResume(skill) removes the marker immediately.
   */
  governancePause(skill, hours) {
    const body = { skill: String(skill || ''), action: 'pause' };
    const h = Number(hours);
    if (Number.isFinite(h) && h > 0) body.hours = h;
    return call('/governance_pause', { body: body });
  },
  governanceResume(skill) {
    return call('/governance_pause', {
      body: { skill: String(skill || ''), action: 'resume' },
    });
  },

    /**
     * Per-cycle dashboard — Day-5 item 7.
     * Reads the most recent N cycle_history.jsonl entries (newest-first)
     * from /api/plugins/skillopt/cycles. Filters: skill name, ISO since
     * timestamp, exact outcome match (adopted|rejected|skipped|errored|
     * unknown). Empty filter args are dropped so the server default wins.
     */
    cycles(limit = 50, skill = '', sinceTs = '', outcome = '') {
      return call('/cycles', {
        body: _qsBody({
          limit: Number.isFinite(limit) ? limit : 50,
          skill: String(skill || ''),
          since_ts: String(sinceTs || ''),
          outcome: String(outcome || ''),
        }),
      });
    },

    /**
     * Single-cycle fetch (Day-5 item 7). Backend route is
     * /api/plugins/skillopt/cycle/<id>; the id is passed in BOTH the
     * URL and the body for the rare case the URL is stripped (proxies,
     * trailing slashes, etc.).
     */
    cycle(cycleId) {
      const id = String(cycleId || '').trim();
      if (!id) return Promise.resolve({ ok: false, error: 'missing cycle_id' });
      return call('/cycle/' + encodeURIComponent(id), {
        body: { cycle_id: id, id: id },
      });
    },

    /**
     * Audit-log read (Day-5 item 7). Backed by logs/runs/adoptions.log
     * (the v1.3.0 audit trail written by auto_loop._auto_adopt()).
     * `passed` accepts true|false|null; null = "all".
     */
    auditLog(limit = 50, skill = '', passed = null) {
      const body = _qsBody({
        limit: Number.isFinite(limit) ? limit : 50,
        skill: String(skill || ''),
      });
      // 'passed' uses string coercion so the server-side _parse_passed()
      // can map true / false / 'true' / 'false' / 'passed' / 'failed' /
      // '' / 'all' to its True/False/None triple.
      if (passed === true) body.passed = true;
      else if (passed === false) body.passed = false;
      // passed === null -> field omitted -> server default (all)
      return call('/audit_log', { body });
    },

    /**
     * Poll status until the in-flight Sleep cycle finishes (or timeout).
     * Returns the final status snapshot.
     */
    async waitForCycle(timeoutMs = 120000, intervalMs = 2000) {
      const deadline = Date.now() + timeoutMs;
      let last = null;
      while (Date.now() < deadline) {
        last = await this.status();
        // The /status endpoint augments with last_log; if there's a
        // recent sleep-*.log that's still being written, the file
        // will grow. We approximate by checking last_log_tail for a
        // "cycle complete" marker — SkillOpt's own output has one.
        const tail = (last && last.last_log_tail) || '';
        if (/cycle complete|adopt|exit code 0|finished/i.test(tail)) break;
        await new Promise(r => setTimeout(r, intervalMs));
      }
      return last;
    },

    /**
     * Build the per-cycle dashboard mount payload from a /cycles
     * response. Returns a plain object the WebUI server (templates/
     * config.html) can iterate over when rendering HTML. Day-5 item 7.
     *
     * Schema:
     *   {
     *     "data-cycle-history": [
     *       {cycle_id, ts, skill, outcome, outcome_detail, runtime_seconds, ...},
     *       ...
     *     ],
     *     "data-summary": { total, adopted, rejected, errored, other },
     *     "data-filters": { skill, outcome, since_ts } (echoed for the UI),
     *     "data-fetched-at": ISO timestamp,
     *   }
     *
     * Pure function: no fetch, no DOM, no side-effects. Easy to unit
     * test (the smoke suite can `renderCycleHistory({ok:true,entries:[...]})`
     * and check the resulting shape).
     */
    renderCycleHistory(cyclesResult) {
      const fetchedAt = new Date().toISOString();
      const empty = {
        'data-cycle-history': [],
        'data-summary': { total: 0, adopted: 0, rejected: 0, errored: 0, other: 0 },
        'data-filters': { skill: '', outcome: '', since_ts: '' },
        'data-fetched-at': fetchedAt,
      };
      if (!cyclesResult || cyclesResult.ok !== true) return empty;
      const entries = Array.isArray(cyclesResult.entries) ? cyclesResult.entries : [];
      let adopted = 0, rejected = 0, errored = 0, other = 0;
      const rows = [];
      for (const e of entries) {
        const outcome = String(e && e.outcome || 'unknown');
        if (outcome === 'adopted') adopted++;
        else if (outcome === 'rejected') rejected++;
        else if (outcome === 'errored') errored++;
        else other++;
        rows.push({
          cycle_id: String(e.cycle_id || ''),
          ts: String(e.ts || ''),
          skill: String(e.skill || ''),
          outcome: outcome,
          outcome_detail: String(e.outcome_detail || ''),
          runtime_seconds: Number(e.runtime_seconds || 0),
          llm_calls: Number(e.llm_calls || 0),
          proposed_size: Number(e.proposed_size || 0),
          current_size: Number(e.current_size || 0),
          // The links block is nullable on entries that didn't fully
          // populate (e.g. skip-cycles); collapse to a flat reference
          // for the UI.
          audit_log_entry: (e.links && e.links.audit_log_entry) || '',
          staged_proposal: (e.links && e.links.staged_proposal) || '',
        });
      }
      return {
        'data-cycle-history': rows,
        'data-summary': {
          total: rows.length,
          adopted: adopted,
          rejected: rejected,
          errored: errored,
          other: other,
        },
        'data-filters': {
          skill: String(cyclesResult.skill || ''),
          outcome: String(cyclesResult.outcome || ''),
          since_ts: String(cyclesResult.since_ts || ''),
        },
        'data-fetched-at': fetchedAt,
      };
    },

    /**
     * v1.8.22: shape the /loop response's real_gate mirror into
     * render-ready values for the Loop status card ("real gate pending"
     * line). Pure function: no fetch, no DOM, no side-effects.
     *
     * loopState: the `/loop` JSON body (api/loop.py passes the whole
     * .auto_loop_state.json dict through). Returns null when no gate is
     * in flight, else { pending, skill, pid, started_ts, age_min }.
     */
    renderRealGate(loopState) {
      const rg = loopState && loopState.real_gate;
      if (!rg || typeof rg !== 'object') return null;
      return {
        pending: true,
        skill: String(rg.skill || ''),
        pid: Number(rg.pid || 0),
        started_ts: Number(rg.started_ts || 0),
        age_s: Number(rg.age_s || 0),
        age_min: Math.round(Number(rg.age_s || 0) / 60),
      };
    },

    /**
     * v1.7.0 (Phase C3): shape a /staged response into render-ready rows.
     * Pure: no fetch, no DOM. Each row carries the gate evidence the
     * config.html "Staged proposals" cards render (skill, proposal_id,
     * lift_pp, n_held_out, gate_reason, last_outcome, size) plus the
     * Approve/Reject/Rollback actions the UI binds to.
     *
     * v1.8.34: this row previously DROPPED the proposal summary, so the
     * sidebar card stayed a name-and-byte-count surface while the settings
     * page grew an informed one - two approval surfaces, one of them still
     * blind. `summary` and the `pending_approval` map are now carried
     * through, and `renderProposalSummary` below flattens the summary into
     * the primitives a `data-*` attribute template can bind.
     */
    renderStagedProposals(stagedResult) {
      const fetchedAt = new Date().toISOString();
      const empty = {
        'data-staged': [],
        'data-staged-count': 0,
        'data-fetched-at': fetchedAt,
      };
      if (!stagedResult || stagedResult.ok !== true) return empty;
      const items = Array.isArray(stagedResult.proposals) ? stagedResult.proposals : [];
      const rows = items.map((p) => ({
        skill: String((p && p.skill) || ''),
        proposal_id: String((p && p.proposal_id) || ''),
        size: Number((p && p.size) || 0),
        mtime: Number((p && p.mtime) || 0),
        lift_pp: (p && p.lift_pp != null) ? Number(p.lift_pp) : null,
        n_held_out: (p && p.n_held_out != null) ? Number(p.n_held_out) : null,
        gate_reason: String((p && p.gate_reason) || ''),
        last_outcome: String((p && p.last_outcome) || ''),
        diff_summary: String((p && p.diff_summary) || ''),
        // v1.8.34: the informed-approval payload. `summary` is null when the
        // proposal predates the sidecar AND summaries are disabled; callers
        // must handle that rather than assume an object.
        summary: (p && p.summary && typeof p.summary === 'object')
          ? this.renderProposalSummary(p.summary)
          : null,
      }));
      return {
        'data-staged': rows,
        'data-staged-count': rows.length,
        'data-staged-pending': this.renderPendingApproval(
          stagedResult.pending_approval),
        'data-fetched-at': fetchedAt,
      };
    },

    /**
     * v1.8.34: flatten a ProposalSummary into render-ready primitives.
     *
     * Pure and defensive by design. `intent` is ADVISORY - nothing in the
     * validation gate reads it - so an unknown or missing category must
     * render as `unknown`, never throw and never be treated as a verdict.
     * Everything is defaulted, because a reviewer must never see a blank
     * card where the evidence should be.
     */
    renderProposalSummary(summary) {
      const s = (summary && typeof summary === 'object') ? summary : {};
      const intent = (s.intent && typeof s.intent === 'object') ? s.intent : {};
      const diff = (s.diff && typeof s.diff === 'object') ? s.diff : {};
      const ev = (s.evidence && typeof s.evidence === 'object') ? s.evidence : {};
      const art = (s.artifacts && typeof s.artifacts === 'object')
        ? s.artifacts : {};
      const realGate = (ev.real_gate && typeof ev.real_gate === 'object')
        ? ev.real_gate : null;
      const category = typeof intent.category === 'string' && intent.category
        ? intent.category : 'unknown';
      let confidence = Number(intent.confidence);
      if (!isFinite(confidence) || confidence < 0 || confidence > 1) confidence = 0;
      return {
        available: s.available === true,
        schema: String(s.schema || ''),
        skill: String(s.skill || ''),
        proposal_id: String(s.proposal_id || ''),
        summary: String(s.summary || ''),
        rationale: Array.isArray(s.rationale)
          ? s.rationale.filter((b) => typeof b === 'string' && b.trim())
          : [],
        warnings: Array.isArray(s.warnings)
          ? s.warnings.filter((w) => typeof w === 'string' && w.trim())
          : [],
        intent_category: category,
        intent_confidence: confidence,
        intent_source: String(intent.source || 'absent'),
        intent_headline: String(intent.headline || ''),
        added_lines: Number(diff.added_lines) || 0,
        removed_lines: Number(diff.removed_lines) || 0,
        hunk_count: Number(diff.hunk_count) || 0,
        diff_available: diff.available === true,
        diff_truncated: diff.truncated === true,
        diff_text: String(diff.text || ''),
        gate_outcome: String(ev.gate_outcome || 'absent'),
        official_gated: ev.official_gated === true,
        lift_pp: (ev.lift_pp != null) ? Number(ev.lift_pp) : null,
        n_held_out: (ev.n_held_out != null) ? Number(ev.n_held_out) : null,
        real_gate_status: realGate ? String(realGate.status || '') : '',
        real_gate_accepted: realGate ? realGate.accepted === true : null,
        proposal_path: String(art.proposal_rel || art.proposal_path || ''),
        // A one-line headline a compact card can show without the full block.
        headline: String(s.summary || intent.headline || ''),
      };
    },

    /**
     * v1.8.34: shape the /staged `pending_approval` map (or a per-skill
     * `pending_proposal`) into one row per skill. This is what lets a
     * governance surface name the document its Approve button refers to -
     * the gap that made "Approve" indistinguishable from approving an unseen
     * document.
     */
    renderPendingApproval(pendingApproval) {
      const out = {};
      if (!pendingApproval || typeof pendingApproval !== 'object') return out;
      for (const skill of Object.keys(pendingApproval)) {
        const entry = pendingApproval[skill];
        if (!entry || typeof entry !== 'object') continue;
        out[skill] = {
          skill: String(skill),
          proposal_id: String(entry.proposal_id || ''),
          // 'pending' until a decision naming this proposal exists.
          decision: String(entry.decision || 'pending'),
          decided_by: String(entry.decided_by || ''),
          decided_at: String(entry.decided_at || ''),
          staged_at: Number(entry.staged_at) || 0,
          summary: (entry.summary && typeof entry.summary === 'object')
            ? this.renderProposalSummary(entry.summary)
            : null,
        };
      }
      return out;
    },

    /**
     * v1.8.34: shape a /mode response into render-ready values for the
     * Evolution mode card. `source` is preserved verbatim because "manual"
     * chosen by the operator and "manual" arrived at because a legacy alias
     * was invalid are different facts, and an operator debugging a mode that
     * will not change needs to see which one they are looking at.
     */
    renderEvolutionMode(modeResult, loopState) {
      const m = (modeResult && typeof modeResult === 'object') ? modeResult : {};
      const rails = (m.rails && typeof m.rails === 'object') ? m.rails : {};
      const adoptedToday = (m.adopted_today != null)
        ? Number(m.adopted_today)
        : Number((loopState && loopState.autonomous_adoptions_today) || 0);
      const cap = Number(rails.max_adoptions_per_day != null
        ? rails.max_adoptions_per_day : m.max_per_day);
      return {
        ok: m.ok === true,
        mode: String(m.mode || 'manual'),
        autonomous: m.autonomous === true,
        // The kill switch outranks every config source, so a consumer must
        // be able to render "paused" without inspecting the mode string.
        killed: m.killed === true,
        source: String(m.source || 'unavailable'),
        error: String(m.error || ''),
        valid_modes: Array.isArray(m.valid_modes) && m.valid_modes.length
          ? m.valid_modes.map(String) : ['manual', 'autonomous'],
        adopted_today: isFinite(adoptedToday) ? adoptedToday : 0,
        max_per_day: isFinite(cap) ? cap : null,
        max_per_skill_per_day: Number(rails.max_adoptions_per_skill_per_day
          != null ? rails.max_adoptions_per_skill_per_day
          : m.max_per_skill_per_day) || null,
        require_post_adopt_check: rails.require_post_adopt_check
          !== false && m.require_post_adopt_check !== false,
        min_evidence_rollouts: Number(rails.min_evidence_rollouts
          != null ? rails.min_evidence_rollouts
          : m.min_evidence_rollouts) || 0,
        audit: rails.audit !== false && m.audit !== false,
        // True only when the mode came from something other than the shipped
        // default, i.e. the operator (or their legacy config) chose it.
        explicit: ['evolution_mode', 'auto_adopt_legacy']
          .indexOf(String(m.source || '')) !== -1,
      };
    },
  };

  // v1.8.34: actually EXPOSE the namespace.
  //
  // This assignment was missing. `const SkillOptDashboard` above is scoped to
  // this IIFE, so the object was unreachable from any other script: the
  // header comment promised "a small `window.SkillOptDashboard` namespace
  // that the WebUI config page and the sidebar card can hook into", but
  // `window.SkillOptDashboard` was `undefined`. Nothing broke visibly only
  // because no consumer referenced it yet - which is exactly how the whole
  // documented extension point stayed dead. Any page that had tried to call
  // `SkillOptDashboard.staged()` or `renderStagedProposals()` got a
  // TypeError.
  //
  // Assigned to `window` rather than `globalThis` so it works on the older
  // browsers this WebUI still supports, and guarded so a re-injected script
  // (the extension loader can run it twice) does not throw on a const-like
  // global.
  try {
    window.SkillOptDashboard = SkillOptDashboard;
  } catch (e) {
    /* no window (non-browser context) - nothing to expose to */
  }

  // Convenience: log availability on load (helps debugging in DevTools)
  console.log('[skillopt] dashboard loaded; API at ' + ENDPOINT_BASE);
})();
