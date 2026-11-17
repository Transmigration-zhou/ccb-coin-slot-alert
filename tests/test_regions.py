import re

import pytest

from coin_alert.regions import (PROVINCES, REGIONS, UnknownRegionError, canonical_city, cities,
                                cities_by_province, districts_of, resolve_district)


def test_districts_of_hangzhou_keeps_order():
    assert districts_of("杭州")[0] == ("滨江区", "330108")
    assert len(districts_of("杭州")) == 13


def test_resolve_by_name_and_code():
    assert resolve_district("杭州", "滨江区") == ("滨江区", "330108")
    assert resolve_district("杭州", "330108") == ("滨江区", "330108")


def test_city_name_forms():
    assert canonical_city("杭州") == "杭州市"
    assert canonical_city("杭州市") == "杭州市"
    assert canonical_city("大理") == "大理白族自治州"
    assert canonical_city("北京") == "北京市"


def test_unknown_city_points_to_list_command():
    with pytest.raises(UnknownRegionError, match="main.py list"):
        districts_of("火星")


def test_unknown_district_lists_options():
    with pytest.raises(UnknownRegionError, match="滨江区"):
        resolve_district("杭州", "不存在区")


def test_codes_are_six_digits():
    for city in cities():
        for _, code in districts_of(city):
            assert re.fullmatch(r"\d{6}", code)


def test_city_names_unique_across_provinces():
    assert len(REGIONS) == sum(len(cs) for cs in PROVINCES.values())


def test_codes_belong_to_their_province():
    for prov, cs in cities_by_province().items():
        prefixes = {code[:2] for c in cs for code in REGIONS[c].values()}
        assert len(prefixes) == 1, prov


def test_codes_unique():
    codes = [code for ds in REGIONS.values() for code in ds.values()]
    assert len(codes) == len(set(codes))


def test_dataset_size_matches_ccb_snapshot():
    # 2026-11-18 PRODUCT_ID=201948 页面数据：10 省 107 市 673 区县，代码之和用于防止抄录错误
    codes = [int(code) for ds in REGIONS.values() for code in ds.values()]
    assert (len(PROVINCES), len(REGIONS), len(codes), sum(codes)) == (10, 107, 673, 231281196)
