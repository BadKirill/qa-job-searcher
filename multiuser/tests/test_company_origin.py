import pytest

from qa_job_bot.company_origin import is_russian_company


@pytest.mark.parametrize(
    "description,company,expected",
    [
        ("Мы — российская IT-компания. Строим сервис для разработчиков.", "", True),
        ("О компании: российская продуктовая компания", "", True),
        ("Наша компания — компания родом из России.", "", True),
        ("We’re a Russian-founded software startup now based in Spain.", "", True),
        ("About us: a global team. We are a Russia-based technology company.", "", True),
        ("Example Group was founded in Russia and moved to Serbia.", "Example Group", True),
        ("Ромашка — российская компания.", "Ромашка", True),
        ("Headquarters: St. Petersburg, Russia.", "", True),
        ("Наша штаб-квартира находится в Москве.", "", True),
        ("Компания основана в России. Работаем по всему миру.", "", True),
        ("We support a Russian company and its customers.", "", False),
        ("Russian company Example Group is our client.", "Recruiting Agency", False),
        ("Founded in Russia, our partner now works worldwide.", "", False),
        ("Example Group was founded in Russia.", "Another Company", False),
        ("We are not a Russian company. The role is based in Moscow.", "", False),
        ("Remote role in Moscow. We need someone familiar with the Russian market.", "", False),
        ("Наш офис продаж находится в Москве. Требуется русский язык.", "", False),
        ("", "ООО Ромашка", False),
        ("x" * 20000 + "\nМы российская компания", "", False),
    ],
)
def test_company_origin_requires_employer_evidence(description, company, expected):
    assert is_russian_company(description, company) is expected
