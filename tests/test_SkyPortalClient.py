import pytest

from ampel.secret.DictSecretProvider import NamedSecret
from ampel.ztf.t3.skyportal.SkyPortalClient import SkyPortalClient


def test_validate_url():
    """URL path may not be set"""
    with pytest.raises(ValueError, match="base_url may not have a path set"):
        SkyPortalClient.validate(
            dict(
                base_url="http://foo.bar/",
                token=NamedSecret[str](label="foo", value="seekrit"),
            )
        )
    SkyPortalClient.validate(
        dict(
            base_url="http://foo.bar",
            token=NamedSecret[str](label="foo", value="seekrit"),
        )
    )
