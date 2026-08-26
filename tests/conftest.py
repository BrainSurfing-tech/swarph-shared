

# --- #578/#579: swarph-shared ships NO default gateway host -----------------
#
# `canonical_names` degrades through its existing fail-soft path when no
# gateway is configured. Most of this suite tests something else and merely
# needs A gateway to be configured, so it gets a dummy one here rather than
# 18 near-identical edits.
#
# setenv ALONE is enough. Since #632, `canonical_names` resolves
# MESH_GATEWAY_URL at CALL time via _default_gateway_url(); the module-level
# DEFAULT_GATEWAY_URL symbol is a non-authoritative compatibility snapshot
# that NOTHING in the package reads, so patching it changes nothing under
# test. (Before #632 the symbol WAS the configuration — captured once at
# import — and this fixture patched it because setenv could not reach it.
# That rule died with the fix. Do not re-add the patch to new tests.)
#
# WHICH TEST CARRIES THE PROPERTY, precisely — both guards are SUBPROCESS
# probes, because an in-process test of this family observes state it set:
#
#   test_the_shipped_default_is_EMPTY            <- reads the symbol the
#                                                   package actually ships
#   test_import_time_capture_does_not_outlive_an_env_unset
#                                                <- NOTHING patched: env set
#                                                   at import, unset, cold
#                                                   call must fail soft
#
# The two behavioural tests in that file (unset -> same error, warm cache)
# observe the fail-soft shape via delenv alone. An earlier version of this
# comment claimed they were can-fail verified against a reintroduced literal;
# drop-on-meta-edge ran exactly that and THEY STAY GREEN — they set the state
# they observe. The claim was false about the tests it named; the guards that
# actually fire are the two probes above.
#
# `gateway.invalid` is deliberate — reserved TLD, cannot resolve to anything real.
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _mesh_gateway_configured(monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gateway.invalid:8788")
