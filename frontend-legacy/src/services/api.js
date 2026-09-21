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
  getMetrics(id) { return this.get(`/runs/${id}/metrics`); },
  listProjects() { return this.get('/projects'); },
  settingsFiles() { return this.get('/settings/files'); },
  settingsFile(name) { return this.get(`/settings/file?name=${encodeURIComponent(name)}`); },
  settingsSave(name, content) { return this.put('/settings/file', { name, content }); },
  settingsEnv() { return this.get('/settings/env'); },
};
