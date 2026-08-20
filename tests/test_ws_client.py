from collector.server_ws_client import build_frame, heartbeat_frame, login_frame


def test_login_and_heartbeat_frame_are_binary_protocol_frames():
    token = "header.payload.signature"
    login = login_frame(token)
    heartbeat = heartbeat_frame()
    assert login[:8] == bytes.fromhex("00000000000005cb")
    assert login[8:12] == (2).to_bytes(4, "big")
    assert token.encode() in login
    assert heartbeat == build_frame(3)
    assert heartbeat[8:12] == (3).to_bytes(4, "big")
