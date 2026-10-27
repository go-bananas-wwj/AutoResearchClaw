from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from researchclaw.config import RCConfig
from researchclaw.server.app import create_app
from researchclaw.server.websocket.heartbeat import is_heartbeat


def test_chat_heartbeats_do_not_call_model_but_user_ping_is_preserved(tmp_path, monkeypatch):
    reply = AsyncMock(return_value="user response")
    monkeypatch.setattr("researchclaw.server.dialog.router.route_message", reply)
    config = RCConfig.from_dict({"project":{"name":"test"}, "research":{"topic":"test"},
        "runtime":{"timezone":"UTC"}, "notifications":{"channel":"console"},
        "knowledge_base":{"root":str(tmp_path)},
        "llm":{"base_url":"https://example.org", "api_key_env":"TEST"}}, check_paths=False)
    client = TestClient(create_app(config))
    for endpoint in ("/ws/chat", "/ws/events"):
        with client.websocket_connect(endpoint) as socket:
            assert socket.receive_json()["type"] == "connected"
            for heartbeat in ("ping", '{"type":"ping"}', '{"type":"heartbeat"}'):
                socket.send_text(heartbeat)
                result = socket.receive_json()
                assert result["type"] == "heartbeat"
                assert result["data"]["status"] == "pong"
            reply.assert_not_called()
            if endpoint == "/ws/chat":
                socket.send_json({"text":"ping", "run_id":"rc-fixture"})
                assert socket.receive_json()["type"] == "chat_response"
                reply.assert_awaited_once()
        reply.reset_mock()


def test_chat_payloads_and_malformed_data_are_not_transport_frames():
    assert not is_heartbeat('{"text":"ping"}')
    assert not is_heartbeat('{"type":"ping","text":"research question"}')
    assert not is_heartbeat('actual scientific question')
    assert not is_heartbeat('["ping"]')
