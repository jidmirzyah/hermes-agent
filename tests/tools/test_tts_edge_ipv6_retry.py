"""Edge TTS retries once over IPv6 when the IPv4 route to speech.platform.bing.com resets.

Regression test: this behavior (originally d2d91820ae, 2026-08-21) was silently dropped by the
2026-09-06 refactor that moved ``_generate_edge_tts`` from tools/tts_tool.py into
tools/tts_tool_providers.py -- the merge carried the rest of the function but not this fix, and
nothing caught it because no test covered the retry path. Restored 2026-09-29.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_edge_tts_retries_over_ipv6_on_connection_reset(tmp_path):
    attempts = []

    def make_communicate(text, **kwargs):
        attempts.append(kwargs)
        comm = MagicMock()
        if "connector" not in kwargs:
            comm.save = AsyncMock(side_effect=ConnectionResetError("simulated IPv4 reset"))
        else:
            comm.save = AsyncMock(return_value=None)
        return comm

    mock_edge = MagicMock()
    mock_edge.Communicate = MagicMock(side_effect=make_communicate)

    with patch("tools.tts_tool._import_edge_tts", return_value=mock_edge):
        from tools.tts_tool import _generate_edge_tts
        result = asyncio.run(_generate_edge_tts("Hello", str(tmp_path / "out.mp3"), {}))

    assert result == str(tmp_path / "out.mp3")
    assert len(attempts) == 2, "expected one failed IPv4 attempt and one IPv6 retry"
    assert "connector" not in attempts[0]
    assert "connector" in attempts[1]


def test_edge_tts_does_not_retry_on_unrelated_error(tmp_path):
    """Only ConnectionResetError/OSError trigger the IPv6 retry -- anything else propagates."""
    mock_comm = MagicMock()
    mock_comm.save = AsyncMock(side_effect=ValueError("unrelated failure"))
    mock_edge = MagicMock()
    mock_edge.Communicate = MagicMock(return_value=mock_comm)

    with patch("tools.tts_tool._import_edge_tts", return_value=mock_edge):
        from tools.tts_tool import _generate_edge_tts
        with pytest.raises(ValueError):
            asyncio.run(_generate_edge_tts("Hello", str(tmp_path / "out.mp3"), {}))

    assert mock_edge.Communicate.call_count == 1
