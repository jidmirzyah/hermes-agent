"""The suite must refuse to run from the live checkout.

`_live_system_guard` blocks individual dangerous git calls but is
function-scoped, so collection time and session-scoped fixtures sit outside
it. Refusing before collection closes both windows. These tests exercise the
pure helper, so they never need a run to actually originate from the live
checkout.
"""

from tests.conftest import (
    LIVE_CHECKOUT_OVERRIDE_ENV,
    LIVE_CHECKOUT_PATH_ENV,
    _live_checkout_refusal,
)


def _live(home):
    p = home / ".hermes" / "hermes-agent"
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_allows_a_clone(tmp_path):
    clone = tmp_path / "reconcile-work" / "some-clone"
    clone.mkdir(parents=True)
    _live(tmp_path)
    assert _live_checkout_refusal(clone, tmp_path, {}) is None


def test_refuses_the_live_checkout(tmp_path):
    live = _live(tmp_path)
    msg = _live_checkout_refusal(live, tmp_path, {})
    assert msg is not None
    assert "Refusing to run the test suite" in msg


def test_override_set_to_one_allows(tmp_path):
    live = _live(tmp_path)
    env = {LIVE_CHECKOUT_OVERRIDE_ENV: "1"}
    assert _live_checkout_refusal(live, tmp_path, env) is None


def test_override_only_honours_exactly_one(tmp_path):
    """A truthy-looking value must not disable the guard."""
    live = _live(tmp_path)
    for value in ("0", "", "true", "yes", "TRUE", "2"):
        env = {LIVE_CHECKOUT_OVERRIDE_ENV: value}
        assert _live_checkout_refusal(live, tmp_path, env) is not None, value


def test_symlink_to_live_checkout_is_still_the_live_checkout(tmp_path):
    live = _live(tmp_path)
    link = tmp_path / "innocent-looking-path"
    link.symlink_to(live)
    assert _live_checkout_refusal(link, tmp_path, {}) is not None


def test_custom_live_checkout_path_is_honoured(tmp_path):
    """A non-standard install must still be protected."""
    elsewhere = tmp_path / "opt" / "hermes-agent"
    elsewhere.mkdir(parents=True)
    _live(tmp_path)
    env = {LIVE_CHECKOUT_PATH_ENV: str(elsewhere)}
    assert _live_checkout_refusal(elsewhere, tmp_path, env) is not None
    # and the default location is no longer what is protected
    assert _live_checkout_refusal(_live(tmp_path), tmp_path, env) is None


def test_message_tells_the_reader_what_to_do(tmp_path):
    live = _live(tmp_path)
    msg = _live_checkout_refusal(live, tmp_path, {})
    assert "git clone" in msg
    assert "reconcile-work" in msg
    assert LIVE_CHECKOUT_OVERRIDE_ENV in msg
    assert str(live) in msg
