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
 */

(function () {
  'use strict';

  const ENDPOINT_BASE = '/api/plugins/skillopt';

  async function call(path, opts = {}) {
    const r = await fetch(ENDPOINT_BASE + path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: opts.body ? JSON.stringify(opts.body) : '{}',
    });
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
     *     reject decision (audit only; does not delete the staged file).
     *   - rollback(skill)               -> POST /rollback: restore the
     *     most recent whole-file _default snapshot for <skill>.
     *   - renderStagedProposals(result)  -> pure fn: shape a /staged
     *     response into rows the WebUI config page can render.
     */
    staged() { return call('/staged'); },
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
     * v1.7.0 (Phase C3): shape a /staged response into render-ready rows.
     * Pure: no fetch, no DOM. Each row carries the gate evidence the
     * config.html "Staged proposals" cards render (skill, proposal_id,
     * lift_pp, n_held_out, gate_reason, last_outcome, size) plus the
     * Approve/Reject/Rollback actions the UI binds to.
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
      }));
      return {
        'data-staged': rows,
        'data-staged-count': rows.length,
        'data-fetched-at': fetchedAt,
      };
    },
  };

  // Convenience: log availability on load (helps debugging in DevTools)
  console.log('[skillopt] dashboard loaded; API at ' + ENDPOINT_BASE);
})();
