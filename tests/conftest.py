

# --- #578/#579: swarph-shared ships NO default gateway host -----------------
#
# `canonical_names` now degrades through its existing fail-soft path when no
# gateway is configured. Most of this suite tests something else and merely needs
# A gateway to be configured, so it gets a dummy one here rather than 18
# near-identical edits.
#
# THIS IS NOT MASKING THE PROPERTY. The unconfigured behaviour has dedicated
# tests that DELETE the variable and pin DEFAULT_GATEWAY_URL to "" —
# test_579_no_machine_specific_host_defaults.py::
#   test_unset_gateway_raises_the_SAME_error_as_an_unreachable_one
#   test_unset_gateway_still_serves_a_warm_cache
# Verified by can-fail: reintroducing a host literal turns those red.
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
