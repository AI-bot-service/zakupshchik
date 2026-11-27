import pytest

from app.scoring.rules import RulesFieldError, load_rules, make_rules, parse_profile
from app.web.rules_form import diff, form_values, parse_form
from tests.test_stand import make_client


def _form() -> dict[str, str]:
    values = form_values(load_rules().profile.model_dump(mode="json"))
    return {k: ("on" if v == "True" else "" if v == "False" else v) for k, v in values.items()}


def test_form_roundtrip_keeps_version() -> None:
    rules = load_rules()
    again = make_rules(parse_profile(parse_form(_form())))
    assert again.version == rules.version


def test_weights_must_sum_to_one_in_russian() -> None:
    form = _form() | {"weights__coverage": "0,9"}
    with pytest.raises(RulesFieldError) as err:
        parse_profile(parse_form(form))
    assert err.value.field == "weights"
    assert "сумма весов" in err.value.message


def test_number_error_points_to_field() -> None:
    with pytest.raises(RulesFieldError) as err:
        parse_profile(parse_form(_form() | {"filters__max_nmck_rub": "-5"}))
    assert err.value.field == "filters.max_nmck_rub"
    assert err.value.message == "должно быть больше 0"


def test_diff_names_changed_fields() -> None:
    old = load_rules().profile.model_dump(mode="json")
    form = _form() | {
        "filters__max_nmck_rub": "2000000",
        "filters__stop_words": _form()["filters__stop_words"] + "\nпринтеры",
    }
    new = parse_profile(parse_form(form)).model_dump(mode="json")
    changes = {name: (was, now) for name, was, now in diff(old, new)}
    assert changes["Максимальная цена закупки"] == ("1000000", "2000000")
    assert changes["Стоп-слова"] == ("", "добавлено: принтеры")


def test_admin_requires_login() -> None:
    r = make_client().get("/admin/rules", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
