/**
 * SettingsView — 浏览器内配置文件编辑器 + 环境状态。
 */
const SettingsView = {
  _files: [],
  _current: '',

  render(container) {
    container.innerHTML = `
      <div class="card">
        <h2>Settings</h2>
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
          <select id="set-file" class="wiz-input" style="width:auto;margin:0"></select>
          <button id="set-reload" class="wiz-btn">Reload</button>
          <button id="set-save" class="wiz-btn primary">Save</button>
          <span id="set-target" style="font-size:12px;color:var(--text-muted)"></span>
        </div>
        <div id="set-env" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px"></div>
        <div id="set-msg" style="display:none;padding:8px 12px;border-radius:6px;margin-bottom:10px;font-size:13px"></div>
        <textarea id="set-editor" spellcheck="false" style="width:100%;min-height:420px;font-family:var(--font-mono,monospace);font-size:13px;background:var(--bg-primary);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:12px" placeholder="Select a config file"></textarea>
        <p style="font-size:12px;color:var(--text-muted);margin-top:8px">Saving validates YAML and writes an automatic .bak backup next to the file. Changes to a running pipeline require a restart of the run to take effect.</p>
      </div>
      <style>
        .wiz-btn { padding:8px 20px; border-radius:6px; border:1px solid var(--border);
                   background:var(--bg-tertiary); color:var(--text-primary); cursor:pointer; font-size:14px; }
        .wiz-btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
        .wiz-btn:hover { opacity:0.9; }
        .wiz-input { padding:10px; background:var(--bg-tertiary); border:1px solid var(--border);
                     border-radius:6px; color:var(--text-primary); font-size:14px; }
      </style>
    `;

    document.getElementById('set-reload').addEventListener('click', () => this._loadFile());
    document.getElementById('set-save').addEventListener('click', () => this._saveFile());
    document.getElementById('set-file').addEventListener('change', (e) => {
      this._current = e.target.value; this._loadFile();
    });

    this._init();
  },

  async _init() {
    try {
      const [files, env] = await Promise.all([API.settingsFiles(), API.settingsEnv()]);
      this._files = files.files;
      const sel = document.getElementById('set-file');
      sel.innerHTML = this._files.map(f =>
        `<option value="${f.name}"${f.name === files.default ? ' selected' : ''}>${f.name}${f.is_symlink ? ' ->' : ''}</option>`).join('');
      this._current = sel.value;

      document.getElementById('set-env').innerHTML = Object.entries(env.keys).map(([k, ok]) =>
        `<span class="status-badge ${ok ? 'completed' : 'idle'}" style="font-family:var(--font-mono,monospace)">${k}: ${ok ? 'set' : 'missing'}</span>`).join('');

      await this._loadFile();
    } catch (e) { this._msg('error', String(e.message || e)); }
  },

  async _loadFile() {
    if (!this._current) return;
    try {
      const d = await API.settingsFile(this._current);
      document.getElementById('set-editor').value = d.content;
      document.getElementById('set-target').textContent =
        d.resolved !== d.name ? `(symlink → ${d.resolved})` : '';
      this._msg('ok', 'Loaded ' + d.name);
    } catch (e) { this._msg('error', String(e.message || e)); }
  },

  async _saveFile() {
    const content = document.getElementById('set-editor').value;
    try {
      const r = await API.settingsSave(this._current, content);
      this._msg('ok', `Saved ${r.wrote} (backup: ${r.backup}) — restart run to apply`);
    } catch (e) { this._msg('error', String(e.message || e)); }
  },

  _msg(kind, text) {
    const el = document.getElementById('set-msg');
    if (!el) return;
    el.style.display = 'block';
    el.style.background = kind === 'ok' ? 'rgba(63,185,80,0.15)' : 'rgba(248,81,73,0.15)';
    el.style.color = kind === 'ok' ? 'var(--success)' : 'var(--danger, #f85149)';
    el.textContent = text;
    setTimeout(() => { el.style.display = 'none'; }, 5000);
  },

  onEvent() {}
};
