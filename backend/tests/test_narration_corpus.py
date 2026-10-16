import pytest

from app.indian import parse_narration
from narration_corpus import CORPUS


@pytest.mark.parametrize("nar,channel,utr,payer,extra", CORPUS, ids=[c[0][:40] for c in CORPUS])
def test_corpus(nar, channel, utr, payer, extra):
    r = parse_narration(nar)
    assert r.channel == channel, r
    if utr:
        assert r.utr == utr, r
    if payer:
        assert r.payer_name and payer in r.payer_name.upper(), r
    if "ifsc" in extra:
        assert r.payer_ifsc == extra["ifsc"], r
    if "vpa" in extra:
        assert r.payer_vpa == extra["vpa"], r
    if "kind" in extra:
        assert r.kind == extra["kind"], r
    if "ccy" in extra:
        assert r.extra.get("fx_ccy") == extra["ccy"] and r.extra.get("fx_minor") == extra["fx_minor"], r
