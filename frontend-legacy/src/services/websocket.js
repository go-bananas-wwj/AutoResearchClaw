/**
 * WebSocket client for ResearchClaw events and chat.
 */
class RCWebSocket {
  constructor(path, options = {}) {
    this.path = path;
    this.reconnectInterval = options.reconnectInterval || 3000;
    this.maxReconnects = options.maxReconnects ?? Infinity;
    this.maxReconnectInterval = options.maxReconnectInterval || 30000;
    this.heartbeatInterval = options.heartbeatInterval ?? 25000;
    this.listeners = {};
    this.ws = null;
    this._reconnects = 0;
    this._closed = false;
    this._reconnectTimer = null;
    this._heartbeatTimer = null;
    this._resume = () => { if (!this._closed && document.visibilityState !== 'hidden') this.connect(); };
    this._watching = false;
  }

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    this._closed = false;
    clearTimeout(this._reconnectTimer);this._reconnectTimer = null;
    if (!this._watching) {
      window.addEventListener('online', this._resume);
      document.addEventListener('visibilitychange', this._resume);
      this._watching = true;
    }
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}${this.path}`;
    const socket = new WebSocket(url);this.ws = socket;

    this.ws.onopen = () => {
      if (this.ws !== socket || this._closed) return;
      this._reconnects = 0;
      clearInterval(this._heartbeatTimer);
      if (this.heartbeatInterval > 0) this._heartbeatTimer = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send('ping');
      }, this.heartbeatInterval);
      this._emit('open', {});
    };

    this.ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        this._emit(data.type, data.data, data.timestamp);
        this._emit('message', data);
      } catch (e) {
        console.warn('Failed to parse WS message:', e);
      }
    };

    this.ws.onclose = () => {
      if (this.ws !== socket) return;
      clearInterval(this._heartbeatTimer);this._heartbeatTimer = null;
      this._emit('close', {});
      if (!this._closed && this._reconnects < this.maxReconnects) {
        this._reconnects++;
        const delay = Math.min(this.maxReconnectInterval, this.reconnectInterval * 2 ** Math.min(this._reconnects - 1, 5));
        this._emit('reconnecting', { attempt: this._reconnects, delay });
        clearTimeout(this._reconnectTimer);
        this._reconnectTimer = setTimeout(() => { if (!this._closed) this.connect(); }, delay);
      }
    };

    this.ws.onerror = (err) => {
      this._emit('error', { error: err });
    };
  }

  send(data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(typeof data === 'string' ? data : JSON.stringify(data));
    }
  }

  on(event, callback) {
    if (!this.listeners[event]) this.listeners[event] = [];
    this.listeners[event].push(callback);
    return this;
  }

  off(event, callback) {
    if (this.listeners[event]) {
      this.listeners[event] = this.listeners[event].filter(cb => cb !== callback);
    }
  }

  close() {
    this._closed = true;
    clearTimeout(this._reconnectTimer);this._reconnectTimer = null;
    clearInterval(this._heartbeatTimer);this._heartbeatTimer = null;
    if (this._watching) {
      window.removeEventListener('online', this._resume);
      document.removeEventListener('visibilitychange', this._resume);
      this._watching = false;
    }
    if (this.ws) this.ws.close();
  }

  _emit(event, data, timestamp) {
    (this.listeners[event] || []).forEach(cb => cb(data, timestamp));
  }
}

// Singleton instances
const eventsWS = new RCWebSocket('/ws/events');
const chatWS = new RCWebSocket('/ws/chat');
