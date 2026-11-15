"""Pure synthetic PDF fixtures shared by tests and the optional layout smoke.

No Django setup, filesystem writes, browser imports, network or subprocess calls.
"""

def fixture(count, long):
    """Lengths stay inside the current form/model limits; all data is invented."""
    paragraph = (
        "Синтетический пример спокойной поездки: пожелания требуют проверки менеджером. "
        "Прогулки, расположение и условия размещения не являются фактами о реальном отеле. "
    )
    extra = paragraph * (8 if long else 1)
    return {
        "schema_version": 1, "template_version": "prototype-b-3", "synthetic": True,
        "title": "Тестовая подборка — " + ("ПродолжительноеНазваниеБезПробелов" * 4 if long else "спокойная поездка"),
        "traveller_summary": "Состав: двое вымышленных взрослых. " + extra + "Конец состава.",
        "requirements": "Начало пожеланий. " + extra + "Конец пожеланий.",
        "request_revision": 1,
        "hotels": [{
            "id": str(i), "name": f"Вымышленный отель {i} — " + ("ДлинноеНазваниеБезПробелов" * 5 if long else "демонстрация"),
            "summary": f"Начало описания {i}. " + extra + f"Конец описания {i}.",
            "pros": [f"Начало особенности {i}. " + extra + f"Конец особенности {i}."],
            "limitations": [f"Начало ограничения {i}. " + extra + f"Конец ограничения {i}."],
            "source_reference": f"Синтетический источник {i}: " + ("ИсточникБезПробелов" * 10 if long else "вымышленные данные"),
        } for i in range(1, count + 1)],
    }


def expected_values(snapshot):
    yield from (snapshot["title"], snapshot["traveller_summary"], snapshot["requirements"])
    for hotel in snapshot["hotels"]:
        yield from (hotel["name"], hotel["summary"], hotel["source_reference"])
        yield from hotel["pros"]
        yield from hotel["limitations"]


def compact(value):
    # PDF extraction may move decorative list markers to the page end.
    return "".join(value.replace("\u00ad", "").replace("•", "").split())
