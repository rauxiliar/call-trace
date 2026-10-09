from datetime import timedelta

import pytest

from app.cdr import SearchInputError, correlate
from app.db import InvalidDomain
from tests.support import (
    CALL,
    DOMAIN,
    LEG_B,
    LEG_C,
    NOW,
    SIP_A,
    SIP_B,
    SIP_C,
    T0,
    T0B,
    T1,
    T1B,
    FakeVoip,
    MemoryRepo,
    cloud_us,
    sample_details,
    sample_legs,
    settings_for,
)


def _uuids(outcome) -> set[str]:
    return {item.leg.uuid for call in outcome.calls for item in call.legs}


def test_call_uuid_and_sip_call_id_return_the_same_legs_from_details():
    repo = MemoryRepo(sample_details(), sample_legs())
    voip = FakeVoip()
    settings = settings_for(cloud_us())
    by_uuid = correlate(
        cloud=cloud_us(),
        settings=settings,
        domain="form.example",
        search_id=CALL,
        repo=repo,
        voip=voip,
        now=NOW,
    )
    by_sip = correlate(
        cloud=cloud_us(),
        settings=settings,
        domain="other.example",
        search_id=SIP_B,
        repo=repo,
        voip=voip,
        now=NOW,
    )
    assert _uuids(by_uuid) == {CALL, LEG_B, LEG_C}
    assert _uuids(by_sip) == _uuids(by_uuid)
    assert by_uuid.cdr_not_found is False
    domains = {call[1] for call in repo.calls if call[0] == "call_uuid"}
    assert domains == {DOMAIN}
    assert not any(call[0] == "any" for call in repo.calls)


def test_without_details_domain_still_expands_every_leg():
    repo = MemoryRepo([], sample_legs())
    voip = FakeVoip()
    settings = settings_for(cloud_us())
    by_uuid = correlate(
        cloud=cloud_us(),
        settings=settings,
        domain=DOMAIN,
        search_id=CALL,
        repo=repo,
        voip=voip,
        now=NOW,
    )
    by_sip = correlate(
        cloud=cloud_us(),
        settings=settings,
        domain=DOMAIN,
        search_id=SIP_B,
        repo=repo,
        voip=voip,
        now=NOW,
    )
    assert _uuids(by_uuid) == {CALL, LEG_B, LEG_C}
    assert _uuids(by_sip) == _uuids(by_uuid)
    assert ("any", DOMAIN, SIP_B) in repo.calls
    assert ("call_uuid", DOMAIN, CALL) in repo.calls


def test_each_sip_call_id_opens_its_own_ladder_in_its_window():
    repo = MemoryRepo(sample_details(), sample_legs())
    voip = FakeVoip(empty={SIP_B})
    outcome = correlate(
        cloud=cloud_us(),
        settings=settings_for(cloud_us()),
        domain="form.example",
        search_id=SIP_C,
        repo=repo,
        voip=voip,
        now=NOW,
    )
    windows = {call_id: (start, end) for call_id, start, end in voip.calls}
    assert windows[SIP_A] == (T0 - timedelta(minutes=5), T1 + timedelta(minutes=5))
    assert windows[SIP_B] == (T0B - timedelta(minutes=5), T1B + timedelta(minutes=5))
    assert set(voip.shares) == {SIP_A, SIP_C}
    assert SIP_B not in voip.shares
    by_uuid = {item.leg.uuid: item for call in outcome.calls for item in call.legs}
    assert by_uuid[LEG_B].voip.missing is True
    assert by_uuid[LEG_B].leg.uuid == LEG_B
    assert by_uuid[CALL].voip.share_url.endswith(SIP_A)
    assert by_uuid[LEG_C].voip.share_url.endswith(SIP_C)
    assert by_uuid[CALL].voip.caller == "100"
    assert by_uuid[CALL].voip.mos == "4.3"
    assert by_uuid[CALL].voip.sip_response == "200"
    assert outcome.cdr_not_found is False


def test_search_without_a_domain_does_not_query():
    repo = MemoryRepo([], sample_legs())
    voip = FakeVoip()
    with pytest.raises(SearchInputError) as exc:
        correlate(
            cloud=cloud_us(),
            settings=settings_for(cloud_us()),
            domain=None,
            search_id=SIP_A,
            repo=repo,
            voip=voip,
            now=NOW,
        )
    assert exc.value.args[0] == "domain"
    assert voip.calls == []
    assert repo.calls == []


def test_invalid_domain_does_not_query():
    class Boom:
        def fetch_details(self, search_id):
            raise AssertionError("details")

        def fetch_legs_by_call_uuid(self, domain, call_uuid):
            raise AssertionError("legs")

        def fetch_legs_by_any_id(self, domain, search_id):
            raise AssertionError("any")

    for bad in ('foo"; DROP TABLE freeswitch_details;--', "has space"):
        with pytest.raises(InvalidDomain):
            correlate(
                cloud=cloud_us(),
                settings=settings_for(cloud_us()),
                domain=bad,
                search_id=CALL,
                repo=Boom(),
                voip=FakeVoip(),
                now=NOW,
            )
