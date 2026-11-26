from types import SimpleNamespace

import pytest

from qa_job_bot import job_language
from qa_job_bot.job_language import working_languages

RUSSIAN = "Ищем инженера для тестирования продукта и развития внутренних инструментов команды."
ENGLISH = "We are hiring an engineer to test our product and improve our internal tools."
FRENCH = "Nous recherchons un ingénieur pour tester le produit et améliorer nos outils internes."


@pytest.mark.parametrize(
    "description,expected",
    [
        (RUSSIAN, {"ru"}),
        (ENGLISH, {"en"}),
        (FRENCH, set()),
        (f"{RUSSIAN}\nРабочий язык — английский.", {"en"}),
        (f"{ENGLISH}\nWorking language: Russian.", {"ru"}),
        (f"{ENGLISH}\nWorking language: Serbian.", set()),
        ("Мы работаем на русском и английском.", {"ru", "en"}),
        ("English and Russian are our primary working languages.", {"ru", "en"}),
        ("Team languages: Russian, German, and English", {"ru", "en"}),
        ("Team communication is in Russian / English", {"ru", "en"}),
        ("The company communicates in English.", {"en"}),
        ("В команде общаемся на русском.", {"ru"}),
        (f"{RUSSIAN}\nАнглоязычная команда.", {"en"}),
        (f"{ENGLISH}\nJoin our Russian-speaking team.", {"ru"}),
        (f"{RUSSIAN}\nJoin our English-speaking team. Working language: Russian.", {"ru"}),
        (f"{ENGLISH}\nWe are not a Russian-speaking team.", {"en"}),
        (f"{RUSSIAN}\nАнглийский B2 будет плюсом.", {"ru"}),
        (f"{ENGLISH}\nExperience testing Russian-language content is required.", {"en"}),
        ("", set()),
        ("Python · SQL · Java", set()),
        ("0123456789" * 5, set()),
    ],
)
def test_working_languages(description, expected):
    assert working_languages(description) == expected


def test_text_detection_checks_confidence_and_bounds_input(monkeypatch):
    guess = SimpleNamespace(lang="ru", prob=0.74)

    def detect(text):
        assert len(text) == 20000
        return [guess]

    monkeypatch.setattr(job_language, "detect_langs", detect)
    description = RUSSIAN * 300 + "Рабочий язык: английский"
    assert working_languages(description) == set()
    guess.prob = 0.75
    assert working_languages(description) == {"ru"}
