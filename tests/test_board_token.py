"""The board's write token survives a restart (factory/board.py)."""
from factory import board


def test_the_token_is_kept_across_restarts_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "TOKEN_FILE", tmp_path / "cache" / "board-token")
    first = board._page_token()
    assert board._page_token() == first and len(first) >= 32
    assert (board.TOKEN_FILE.stat().st_mode & 0o777) == 0o600
