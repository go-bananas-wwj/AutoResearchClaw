/**
 * REST API client for ResearchClaw.
 */
const API = {
  base: '/api',

  async get(path) {
    const res = await fetch(`${this.base}${path}`);
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return res.json();
  },

  async post(path, body = {}) {
    const res = await fetch(`${this.base}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return res.json();
  },

  async put(path, body = {}) {
    const res = await fetch(`${this.base}${path}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      // 尽量提取 FastAPI 的 detail 字段
      let msg = await res.text();
      try { msg = JSON.parse(msg).detail || msg; } catch (e) {}
      throw new Error(`API ${res.status}: ${msg}`);
    }
    return res.json();
  },

  // Convenience methods
  health() { return this.get('/health'); },
  config() { return this.get('/config'); },
  pipelineStatus() { return this.get('/pipeline/status'); },
  pipelineStages() { return this.get('/pipeline/stages'); },
  startPipeline(opts) { return this.post('/pipeline/start', opts); },
  stopPipeline() { return this.post('/pipeline/stop'); },
  listRuns() { return this.get('/runs'); },
  getRun(id) { return this.get(`/runs/${id}`); },
  runFiles(id) { return this.get(`/runs/${id}/files`); },
  getMetrics(id) { return this.get(`/runs/${id}/metrics`); },
  listProjects() { return this.get('/projects'); },
  settingsFiles() { return this.get('/settings/files'); },
  settingsFile(name) { return this.get(`/settings/file?name=${encodeURIComponent(name)}`); },
  settingsSave(name, content) { return this.put('/settings/file', { name, content }); },
  settingsEnv() { return this.get('/settings/env'); },
  ideationStart(direction) { return this.post('/ideation/start', { direction }); },
  ideaCards(direction, n) { return this.post('/ideation/cards', { direction: direction, n: n || 4 }); },
  ideaPrompt() { return this.get('/ideation/prompt'); },
  ideationSessions() { return this.get('/ideation/sessions'); },
  ideationSession(id) { return this.get(`/ideation/sessions/${id}`); },
  createIdeation(direction) { return this.post('/ideation/sessions', { direction }); },
  ideationAction(id, action, body = {}) { return this.post(`/ideation/sessions/${id}/${action}`, body); },
  async uploadIdeationPaper(id, revision, file) {
    const res = await fetch(`${this.base}/ideation/sessions/${id}/papers/upload?revision=${revision}`, {
      method: 'POST', headers: { 'Content-Type': 'application/pdf', 'X-Filename': encodeURIComponent(file.name) }, body: file,
    });
    if (!res.ok) { const data = await res.json(); throw new Error(data.detail || '上传失败'); }
    return res.json();
  },
  ideaConfirm(payload) { return this.post('/ideation/confirm', payload); },
  ideationStatus() { return this.get('/ideation/status'); },
  ideationReport(runId) { return this.get(`/ideation/report${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}`); },
  reproduceStart(opts) { return this.post('/reproduce/start', opts); },
  reproduceStatus() { return this.get('/reproduce/status'); },
  reproduceReport(runId, slug) {
    const q = [runId && `run_id=${encodeURIComponent(runId)}`, slug && `paper_slug=${encodeURIComponent(slug)}`].filter(Boolean).join('&');
    return this.get(`/reproduce/report${q ? `?${q}` : ''}`);
  },
  reproduceStop() { return this.post('/reproduce/stop'); },
  hitlWaiting() { return this.get('/hitl/waiting'); },
  hitlRunWaiting(id) { return this.get(`/runs/${id}/hitl/waiting`); },
  hitlFile(id, path) { return this.get(`/runs/${id}/hitl/file?path=${encodeURIComponent(path)}`); },
  hitlInterventions(id) { return this.get(`/runs/${id}/hitl/interventions`); },
  hitlRespond(id, body) { return this.post(`/runs/${id}/hitl/respond`, body); },
  paperPull(id, language) {
    return this.post(`/runs/${id}/overleaf/pull?language=${encodeURIComponent(language || 'zh')}`);
  },
  paperPush(id, language) {
    return this.post(`/runs/${id}/overleaf/push?language=${encodeURIComponent(language || 'zh')}`);
  },
  paperAnnotations(id, language) {
    return this.get(`/runs/${id}/paper/annotations?language=${encodeURIComponent(language || 'zh')}`);
  },
  paperRevise(id, language, instruction) {
    return this.post(`/runs/${id}/paper/revise`, { language: language || 'zh', instruction: instruction || '' });
  },
  paperTranslate(id) {
    return this.post(`/runs/${id}/paper/translate`, {});
  },
  paperVersions(id, language) {
    return this.get(`/runs/${id}/paper/versions?language=${encodeURIComponent(language || 'zh')}`);
  },
};
