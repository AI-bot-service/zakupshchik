"""Форма «Правила робота» на вкладке «Админка»: описание полей, разбор формы, разница между версиями."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.db.models import Procedure
from app.pipeline.prefilter import PROCEDURE_RU
from app.scoring.rules import RuleProfile


class Kind(StrEnum):
    NUMBER = "number"
    TEXT = "text"
    LINES = "lines"
    BOOL = "bool"
    TIME = "time"


@dataclass(frozen=True)
class FieldSpec:
    path: str  # filters.max_nmck_rub
    label: str
    hint: str
    kind: Kind
    step: str = "1"
    unit: str = ""

    @property
    def name(self) -> str:
        return self.path.replace(".", "__")


@dataclass(frozen=True)
class Group:
    title: str
    hint: str
    fields: tuple[FieldSpec, ...]
    procedures: bool = False


GROUPS: tuple[Group, ...] = (
    Group(
        "Отсев",
        "Отсеянная закупка не разбирается моделью и не стоит токенов. Причина — во вкладке «Отсеянные».",
        (
            FieldSpec(
                "filters.max_nmck_rub",
                "Максимальная цена закупки",
                "Дороже — отсекаем.",
                Kind.NUMBER,
                unit="₽",
            ),
            FieldSpec(
                "filters.min_hours_left",
                "Минимум до конца приёма КП",
                "Меньше — не успеем, отсекаем.",
                Kind.NUMBER,
                "0.5",
                "ч",
            ),
            FieldSpec(
                "filters.software_words",
                "Слова «это ПО»",
                "Начало слова, по одному в строке. Нет ни вендора из каталога, ни этих слов — не ПО.",
                Kind.LINES,
            ),
            FieldSpec(
                "filters.stop_words",
                "Стоп-слова",
                "Услуги, оборудование, чужие продукты без вендора из каталога. Пробел в конце — целое слово.",
                Kind.LINES,
            ),
            FieldSpec(
                "filters.smb_only_phrases",
                "Признаки «только для малого бизнеса»",
                "Такие закупки отсекаем: Софтлайн к ним не допускают.",
                Kind.LINES,
            ),
            FieldSpec(
                "filters.cut_quoted_foreign_product",
                "Отсекать чужой продукт в кавычках",
                "«ТехноКад» и т.п. без вендора из каталога.",
                Kind.BOOL,
            ),
        ),
        procedures=True,
    ),
    Group(
        "Пять условий",
        "Из них складывается «N из 5». Пороги двух условий:",
        (
            FieldSpec(
                "conditions.max_postpay_days",
                "Отсрочка оплаты не больше",
                "Дольше — условие «Оплата» не выполнено.",
                Kind.NUMBER,
                unit="дн.",
            ),
            FieldSpec(
                "conditions.min_hours_for_kp",
                "Запас времени на КП",
                "Меньше — условие «Успеваем подать» не выполнено.",
                Kind.NUMBER,
                "0.5",
                "ч",
            ),
        ),
    ),
    Group(
        "Приоритет внутри группы",
        "Веса слагаемых, в сумме ровно 1. Сделки с одинаковым «N из 5» сортируются по этой сумме.",
        (
            FieldSpec(
                "weights.coverage",
                "Покрытие каталогом",
                "Доля позиций, найденных в каталоге.",
                Kind.NUMBER,
                "0.05",
            ),
            FieldSpec(
                "weights.margin",
                "Маржа",
                "Сумма по каталогу укладывается в цену закупки.",
                Kind.NUMBER,
                "0.05",
            ),
            FieldSpec(
                "weights.amount", "Сумма", "Цена закупки относительно максимальной.", Kind.NUMBER, "0.05"
            ),
            FieldSpec("weights.payment", "Оплата", "Предоплата лучше отсрочки.", Kind.NUMBER, "0.05"),
            FieldSpec(
                "weights.simplicity",
                "Простота",
                "Без ТЗ во вложении и без договора заказчика.",
                Kind.NUMBER,
                "0.05",
            ),
        ),
    ),
    Group(
        "Коммерческое предложение",
        "Подставляется в КП по шаблону. Подпись и печать ставит человек.",
        (
            FieldSpec(
                "kp.valid_days",
                "Срок действия КП",
                "Если закупка требует больше — берём её срок.",
                Kind.NUMBER,
                unit="дн.",
            ),
            FieldSpec("kp.vat_rate", "НДС", "ПО из реестра Минцифры — без НДС.", Kind.NUMBER, unit="%"),
            FieldSpec("kp.default_payment", "Оплата, если в закупке не указана", "", Kind.TEXT),
            FieldSpec("kp.default_delivery", "Срок поставки по умолчанию", "", Kind.TEXT),
            FieldSpec("kp.signer_name", "Подписант: ФИО", "", Kind.TEXT),
            FieldSpec("kp.signer_title", "Подписант: должность", "", Kind.TEXT),
        ),
    ),
    Group(
        "Уведомления",
        "Письмо по сделке и уведомление в плагине. Адреса писем — в настройках сервера.",
        (
            FieldSpec(
                "notify.min_conditions",
                "Уведомлять от",
                "Сколько условий из пяти должно выполняться.",
                Kind.NUMBER,
                unit="из 5",
            ),
            FieldSpec(
                "notify.urgent_hours_left",
                "Срочно, если до конца приёма меньше",
                "Выделяется красным.",
                Kind.NUMBER,
                "0.5",
                "ч",
            ),
            FieldSpec("notify.daily_report_at", "Ежедневный Excel на почту", "Московское время.", Kind.TIME),
        ),
    ),
)
FIELDS = {f.path: f for g in GROUPS for f in g.fields}
PROC_CHOICES = (("take", "берём"), ("cut", "отсекаем"), ("none", "не учитываем, помечаем риск"))
PROCEDURES = tuple(p for p in Procedure if p is not Procedure.UNKNOWN)


def _get(data: dict[str, Any], path: str) -> Any:  # noqa: ANN401 — значения профиля разных типов
    node: Any = data
    for part in path.split("."):
        node = node[part]
    return node


def _show(value: Any, kind: Kind) -> str:  # noqa: ANN401
    if kind is Kind.LINES:
        return "\n".join(value)
    if kind is Kind.TIME:
        return str(value)[:5]
    if isinstance(value, float | Decimal):
        return f"{value:g}" if isinstance(value, float) else format(value.normalize(), "f")
    return str(value)


def form_values(profile_json: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {f.name: _show(_get(profile_json, f.path), f.kind) for f in FIELDS.values()}
    filters = profile_json["filters"]
    for p in PROCEDURES:
        values[f"proc__{p.value}"] = (
            "take"
            if p.value in filters["procedures_take"]
            else "cut"
            if p.value in filters["procedures_cut"]
            else "none"
        )
    return values


def parse_form(form: dict[str, str]) -> dict[str, Any]:
    """Поля формы → словарь для RuleProfile. Типы и пределы проверяет схема, здесь только раскладка."""
    data: dict[str, Any] = {}
    for spec in FIELDS.values():
        raw = form.get(spec.name, "")
        value: Any
        if spec.kind is Kind.BOOL:
            value = raw == "on"
        elif spec.kind is Kind.LINES:
            # пробел на конце значим: «сервер » не ловит «серверный»
            value = list(dict.fromkeys(line.rstrip("\r") for line in raw.splitlines() if line.strip()))
        else:
            value = raw.strip().replace(",", ".") if spec.kind is Kind.NUMBER else raw.strip()
        group, key = spec.path.split(".")
        data.setdefault(group, {})[key] = value
    take = [p.value for p in PROCEDURES if form.get(f"proc__{p.value}") == "take"]
    cut = [p.value for p in PROCEDURES if form.get(f"proc__{p.value}") == "cut"]
    data["filters"]["procedures_take"], data["filters"]["procedures_cut"] = take, cut
    return data


GROUP_LABELS = {"weights": "Веса приоритета", "filters": "Отсев", "conditions": "Пять условий", "kp": "КП"}


def label(path: str) -> str:
    if path.startswith("filters.procedures"):
        return "Способы закупки"
    if path in GROUP_LABELS:
        return GROUP_LABELS[path]
    spec = FIELDS.get(path)
    return spec.label if spec else path


def _flat(profile_json: dict[str, Any]) -> dict[str, str]:
    out = {
        path: _show(_get(profile_json, path), spec.kind).replace("\n", ", ") for path, spec in FIELDS.items()
    }
    f = profile_json["filters"]
    out["filters.procedures"] = "берём: " + ", ".join(
        PROCEDURE_RU[Procedure(x)] for x in f["procedures_take"]
    )
    out["filters.procedures"] += "; отсекаем: " + ", ".join(
        PROCEDURE_RU[Procedure(x)] for x in f["procedures_cut"]
    )
    return out


def diff(old: dict[str, Any] | None, new: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Что поменялось между версиями: (поле, было, стало). Длинные списки — числом строк."""
    if old is None:
        return []
    a, b = _flat(old), _flat(new)
    changes = []
    for path in b:
        if a.get(path) != b[path]:
            was, now = a.get(path, ""), b[path]
            if FIELDS.get(path) and FIELDS[path].kind is Kind.LINES:
                was_set, now_set = set(was.split(", ")), set(now.split(", "))
                added, removed = sorted(now_set - was_set), sorted(was_set - now_set)
                was = ("убрано: " + ", ".join(removed)) if removed else ""
                now = ("добавлено: " + ", ".join(added)) if added else ""
            changes.append((label(path), was, now))
    return changes


def as_profile_json(profile: RuleProfile) -> dict[str, Any]:
    return profile.model_dump(mode="json")
