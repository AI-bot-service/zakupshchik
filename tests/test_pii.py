from app.core.pii import mask, mask_text


def test_contacts_masked() -> None:
    text = "Контактное лицо: Иванова Мария Петровна, тел. +7 (495) 123-45-67, ivanova@school.ru"
    out = mask_text(text)
    assert "Иванова" not in out and "123-45-67" not in out and "@" not in out
    assert "[ФИО]" in out and "[телефон]" in out and "[почта]" in out


def test_initials_and_individual_inn_masked() -> None:
    out = mask_text("Ответственный Петров П.П., ИНН 500100732259, СНИЛС 112-233-445 95")
    assert "Петров" not in out and "500100732259" not in out and "112-233-445" not in out


def test_entrepreneur_name_masked() -> None:
    assert "Сидоров" not in mask_text("ИП Сидоров Олег Игоревич")


def test_organization_and_legal_inn_kept() -> None:
    text = 'МУНИЦИПАЛЬНОЕ КАЗЕННОЕ УЧРЕЖДЕНИЕ "ВОЛОВСКАЯ СЛУЖБА СЕРВИСА", ИНН 7124500810'
    assert mask_text(text) == text
    assert mask_text("Kaspersky Endpoint Security для бизнеса – Стандартный, 50 узлов") == (
        "Kaspersky Endpoint Security для бизнеса – Стандартный, 50 узлов"
    )


def test_mask_recurses() -> None:
    assert mask({"a": ["x@y.ru", 5], "b": None}) == {"a": ["[почта]", 5], "b": None}
