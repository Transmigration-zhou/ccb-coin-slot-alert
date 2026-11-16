import re

import pytest

from coin_alert.regions import UnknownRegionError, cities, districts_of, resolve_district


def test_districts_of_hangzhou_keeps_order():
    assert districts_of("杭州")[0] == ("上城区", "330102")
    assert len(districts_of("杭州")) == 7


def test_resolve_by_name_and_code():
    assert resolve_district("杭州", "滨江区") == ("滨江区", "330108")
    assert resolve_district("杭州", "330108") == ("滨江区", "330108")


def test_unknown_city_lists_options():
    with pytest.raises(UnknownRegionError, match="杭州"):
        districts_of("火星")


def test_unknown_district_lists_options():
    with pytest.raises(UnknownRegionError, match="滨江区"):
        resolve_district("杭州", "不存在区")


def test_codes_are_six_digits():
    for city in cities():
        for _, code in districts_of(city):
            assert re.fullmatch(r"\d{6}", code)
