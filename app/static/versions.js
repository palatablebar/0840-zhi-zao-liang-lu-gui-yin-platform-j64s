(function () {
  var state = {run: null, rules: [], pendingRevisionId: null, lastReport: null};
  function $(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch];
    });
  }
  async function request(url, options) {
    var resp = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await resp.json().catch(function () { return {}; });
    if (!resp.ok) throw new Error((data.error || ('HTTP ' + resp.status)) + (data.code ? '（' + data.code + '）' : ''));
    return data;
  }
  function notice(text) {
    var n = $('notice');
    if (n) n.textContent = text || '';
  }
  async function refreshRun(runId) {
    var run = await request('/api/runs/' + runId);
    state.run = run;
    document.dispatchEvent(new CustomEvent('yield:run', {detail: run}));
    renderVersions(run);
    return run;
  }

  function renderRulesOptions() {
    var sel = $('target-rule-version');
    if (!sel) return;
    sel.innerHTML = state.rules.map(function (r) {
      return '<option value="' + esc(r.rule_version) + '">' + esc(r.rule_version) +
        ' · 目标 ' + esc((r.params || {}).target || '') + '</option>';
    }).join('');
    if (state.run && state.run.rule_version) sel.value = state.run.rule_version === 'v1' ? 'v2' : state.run.rule_version;
  }

  function renderVersions(run) {
    state.run = run;
    var rv = run.rule_version || 'v1';
    $('rule-version-badge').textContent = '规则 ' + rv;
    var chip = document.querySelector('.rule-chip');
    if (chip) chip.textContent = (run.rule_info && run.rule_info.rule_short) ? run.rule_info.rule_short : ('工艺规则 ' + rv);
    $('rule-scope').textContent = run.rule_scope || (run.scope && run.scope.name) || '全部工序 · 首检良率 / 滚动良率归因';
    $('publishable-version').textContent = run.publishable_version == null ? '暂无可发布版本' : ('v' + run.publishable_version);
    $('issued-version').textContent = '已签发：' + (run.issued_version == null ? '无' : ('v' + run.issued_version));
    $('recalc').disabled = false;
    $('create-daily').disabled = run.issued_version == null;

    var reasons = run.incomplete_reasons || [];
    var box = $('incomplete-box');
    if (reasons.length) {
      box.hidden = false;
      $('incomplete-list').innerHTML = reasons.map(function (code) {
        return '<li><code>' + esc(code) + '</code></li>';
      }).join('');
      var pending = (run.pending_revisions || [])[0];
      state.pendingRevisionId = pending ? pending.id : null;
      $('resume-recalc').disabled = !state.pendingRevisionId;
    } else {
      box.hidden = true;
      state.pendingRevisionId = null;
    }

    var versions = run.versions || [];
    $('version-list').innerHTML = versions.length ? versions.map(function (v) {
      var cls = v.state === 'ISSUED' ? 'ver-issued' : (v.state === 'READY' ? 'ver-ready' : 'ver-pending');
      return '<li class="' + cls + '"><strong>版本 v' + esc(v.version) + '</strong>' +
        '<span>' + esc(v.rule_version) + '</span><em>' + esc(v.state) + '</em></li>';
    }).join('') : '<li class="empty">尚未生成版本；签发后保留第一版。</li>';

    var ready = versions.filter(function (v) { return v.state === 'READY'; }).pop();
    $('publish-candidate').disabled = !ready;
    $('publish-candidate').textContent = ready ? ('发布可发布版本 v' + ready.version) : '发布可发布版本';
  }

  async function recalc() {
    if (!state.run) return;
    try {
      notice('');
      $('recalc').disabled = true;
      var ruleVersion = $('target-rule-version').value || 'v2';
      var payload = {rule_version: ruleVersion};
      if ($('simulate-interrupt') && $('simulate-interrupt').checked) payload.complete = false;
      var result = await request('/api/runs/' + state.run.run_id + '/revisions', {
        method: 'POST', body: JSON.stringify(payload)
      });
      if ($('simulate-interrupt')) $('simulate-interrupt').checked = false;
      if (result.incomplete) {
        notice('重算中断：' + (result.incomplete_reasons || []).join(', ') + '；补齐数据后可续跑。');
        state.pendingRevisionId = result.id;
      } else {
        notice('已按 ' + ruleVersion + ' 重算，得到可发布版本 v' + result.version + '（重复提交同一请求不会新增版本）。');
      }
      await refreshRun(state.run.run_id);
    } catch (e) { notice(e.message); }
    finally { $('recalc').disabled = false; }
  }

  async function publishCandidate() {
    if (!state.run) return;
    var version = state.run.publishable_version;
    if (version == null) return;
    try {
      notice('');
      var run = await request('/api/runs/' + state.run.run_id + '/review', {
        method: 'POST',
        body: JSON.stringify({
          decision: 'APPROVED', comment: $('comment').value || '按新规则重算后签发',
          expected_revision: state.run.revision, version: version
        })
      });
      renderVersions(run);
      document.dispatchEvent(new CustomEvent('yield:run', {detail: run}));
      notice('版本 v' + version + ' 已签发；历史版本与生产日报引用保持不变。');
    } catch (e) { notice(e.message); }
  }

  async function resume() {
    if (!state.pendingRevisionId) return;
    try {
      notice('');
      await request('/api/revisions/' + state.pendingRevisionId + '/resume', {
        method: 'POST', body: JSON.stringify({complete: true})
      });
      notice('已补齐数据并续跑完成，生成可发布版本。');
      await refreshRun(state.run.run_id);
    } catch (e) { notice(e.message); }
  }

  async function createDaily() {
    if (!state.run) return;
    var date = $('daily-date').value || new Date().toISOString().slice(0, 10);
    try {
      notice('');
      var report = await request('/api/runs/' + state.run.run_id + '/daily-reports', {
        method: 'POST', body: JSON.stringify({date: date})
      });
      state.lastReport = report;
      $('daily-view').innerHTML =
        '<p><strong>' + esc(report.report_date) + '</strong> · 引用已签发版本 v' + esc(report.version) +
        '（' + esc(report.rule_version) + '）</p>' +
        '<p class="hint">证据摘要 ' + esc((report.artifact_sha256 || '').slice(0, 16)) + '… · 日报摘要 ' +
        esc((report.report_sha256 || '').slice(0, 16)) + '…</p>' +
        '<ul class="version-list">' + (report.rows || []).map(function (r) {
          return '<li><strong>' + esc(r.label) + '</strong><span>' + esc(r.verdict) +
            '</span><em>' + esc(r.reason) + '</em></li>';
        }).join('') + '</ul>' +
        '<a href="/api/daily-reports/' + encodeURIComponent(report.id) + '" target="_blank">查看日报 JSON</a>';
      notice('生产日报已固定引用 v' + report.version + '，后续重算不会改变它。');
    } catch (e) { notice(e.message); }
  }

  function reset() {
    state.run = null; state.pendingRevisionId = null;
    $('rule-scope').textContent = '--';
    $('publishable-version').textContent = '--';
    $('issued-version').textContent = '已签发：--';
    $('incomplete-box').hidden = true;
    $('version-list').innerHTML = '<li class="empty">尚未生成版本；签发后保留第一版。</li>';
    $('publish-candidate').disabled = true;
    $('recalc').disabled = true;
    $('create-daily').disabled = true;
  }

  document.addEventListener('DOMContentLoaded', async function () {
    var today = $('daily-date');
    if (today) today.value = new Date().toISOString().slice(0, 10);
    $('recalc').addEventListener('click', recalc);
    $('publish-candidate').addEventListener('click', publishCandidate);
    $('resume-recalc').addEventListener('click', resume);
    $('create-daily').addEventListener('click', createDaily);
    document.addEventListener('yield:run', function (e) { if (e.detail) renderVersions(e.detail); });
    document.addEventListener('yield:reset', reset);
    try {
      var data = await request('/api/rules');
      state.rules = data.rule_versions || data.rules || [];
      renderRulesOptions();
    } catch (e) { /* 目录不可用时保持默认选项 */ }
  });
})();
