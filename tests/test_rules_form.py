import pytest

from app.scoring.rules import DEFAULT_SEARCH_QUERIES, RulesFieldError, load_rules, make_rules, parse_profile
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


def test_recipients_parsed_normalized_and_outside_version() -> None:
    rules = load_rules()
    form = _form() | {"notify__recipients": "Manager@Softline.ru, b@x.ru\nb@x.ru;  c@y.org\n"}
    again = make_rules(parse_profile(parse_form(form)))
    assert again.profile.notify.recipients == ["manager@softline.ru", "b@x.ru", "c@y.org"]
    assert again.version == rules.version


def test_bad_recipient_points_to_field() -> None:
    with pytest.raises(RulesFieldError) as err:
        parse_profile(parse_form(_form() | {"notify__recipients": "ok@x.ru\nне-почта"}))
    assert err.value.field == "notify.recipients"
    assert "не-почта" in err.value.message


def test_diff_old_version_without_recipients() -> None:
    old = load_rules().profile.model_dump(mode="json")
    del old["notify"]["recipients"]
    new = parse_profile(parse_form(_form() | {"notify__recipients": "a@x.ru"})).model_dump(mode="json")
    changes = {name: (was, now) for name, was, now in diff(old, new)}
    assert changes["Адреса для писем о новых сделках"] == ("", "добавлено: a@x.ru")


def test_recipients_since_counts_continuous_stretch() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.scoring.store import recipients_since

    base = load_rules().profile.model_dump(mode="json")

    def row(day: int, addrs: list[str]) -> SimpleNamespace:
        profile = {**base, "notify": {**base["notify"], "recipients": addrs}}
        return SimpleNamespace(profile=profile, created_at=datetime(2026, 11, day, tzinfo=UTC))

    rows = [row(28, ["a@x.ru", "b@x.ru"]), row(27, ["b@x.ru"]), row(26, ["a@x.ru", "b@x.ru"])]
    session = SimpleNamespace(scalars=lambda _q: rows)
    since = recipients_since(session)  # type: ignore[arg-type]
    assert since["a@x.ru"].day == 28  # был удалён 27-го — считаем заново
    assert since["b@x.ru"].day == 26


def test_search_queries_saved_outside_version_and_shown_in_diff() -> None:
    rules = load_rules()
    old = rules.profile.model_dump(mode="json")
    form = _form() | {"filters__search_queries": "лицензи\n  СЗИ  \nлицензи\n"}
    again = make_rules(parse_profile(parse_form(form)))
    assert again.profile.filters.search_queries == ["лицензи", "СЗИ"]
    # Смена слов поиска не меняет отбор — сделки не пересчитываются
    assert again.version == rules.version
    changes = {name: now for name, _, now in diff(old, again.profile.model_dump(mode="json"))}
    assert changes["Слова для поиска"] == "добавлено: СЗИ"


def test_search_queries_default_for_old_versions_and_checked() -> None:
    old = load_rules().profile.model_dump(mode="json")
    del old["filters"]["search_queries"]
    assert parse_profile(old).filters.search_queries == list(DEFAULT_SEARCH_QUERIES)
    with pytest.raises(RulesFieldError) as err:
        parse_profile(parse_form(_form() | {"filters__search_queries": "по"}))
    assert err.value.field == "filters.search_queries"
    assert "от 3 до 100" in err.value.message
