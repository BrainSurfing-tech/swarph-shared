

# --- #578/#579: swarph-shared ships NO default gateway host -----------------
#
# `canonical_names` now degrades through its existing fail-soft path when no
# gateway is configured. Most of this suite tests something else and merely needs
# A gateway to be configured, so it gets a dummy one here rather than 18
# near-identical edits.
#
# THIS IS NOT MASKING THE PROPERTY — but be precise about WHICH test carries it.
#
# CORRECTION (drop-on-meta-edge, seat-A review of PR #24): an earlier version of
# this comment claimed the two behavioural tests below were "verified by can-fail:
# reintroducing a host literal turns those red". He ran exactly that. THEY STAY
# GREEN — because both pin DEFAULT_GATEWAY_URL to "" themselves, so they observe a
# value they set. The claim was false about the tests it named.
#
#   test_unset_gateway_raises_the_SAME_error_as_an_unreachable_one   <- pins the constant
#   test_unset_gateway_still_serves_a_warm_cache                     <- pins the constant
#
# The guard that actually fires is test_the_shipped_default_is_EMPTY, which reads
# the constant in a SUBPROCESS with MESH_GATEWAY_URL removed — so it sees whatever
# the package really ships, including an `or "host"` append or a second assignment
# that a source regex misses. Verified red on all three variants.
#
# `gateway.invalid` is deliberate — reserved TLD, cannot resolve to anything real.
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _mesh_gateway_configured(monkeypatch):
    # setenv ALONE is not enough: DEFAULT_GATEWAY_URL is a module-level
    # os.getenv(), resolved once at IMPORT, long before any fixture runs. The
    # constant has to be patched as well — the same import-time trap that made
    # two swarph-cli tests compare against a value monkeypatch could never change.
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gateway.invalid:8788")
    from swarph_shared import peer_registry as _pr

    monkeypatch.setattr(_pr, "DEFAULT_GATEWAY_URL", "http://gateway.invalid:8788")
