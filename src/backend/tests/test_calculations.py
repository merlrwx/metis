from types import SimpleNamespace

import pytest
from backend.services.calculations import Calculation, calculate
from backend.services.knowledge import SearchHit


def evidence():
    return [
        SearchHit(
            SimpleNamespace(
                content=f"PAYSLIP\nPeriod: {period}\nNet payment AUD {amount}\nGross payment AUD 2500.00"
            ),
            SimpleNamespace(id=period),
            None,
            0.9,
        )
        for period, amount in [
            ("January 2026", "2000.10"),
            ("February 2026", "2100.25"),
        ]
    ]


def calculation():
    return Calculation.model_validate(
        {
            "operation": "difference",
            "operands": [
                {
                    "value": "2100.25",
                    "unit": "AUD",
                    "meaning": "net",
                    "period": "February 2026",
                    "citation": "C2",
                },
                {
                    "value": "2000.10",
                    "unit": "AUD",
                    "meaning": "net",
                    "period": "January 2026",
                    "citation": "C1",
                },
            ],
        }
    )


def test_difference_is_decimal_and_shows_cited_operands():
    result = calculate(calculation(), evidence())
    assert "= 100.15 AUD" in result
    assert "2100.25 AUD net, February 2026 [C2]" in result
    assert "2000.10 AUD net, January 2026 [C1]" in result


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", "2000.11"),
        ("value", "NaN"),
        ("unit", "USD"),
        ("period", "March 2026"),
        ("citation", "C9"),
        ("meaning", "gross"),
    ],
)
def test_unverified_operands_are_rejected(field, value):
    request = calculation()
    setattr(request.operands[1], field, value)
    with pytest.raises(ValueError):
        calculate(request, evidence())


def test_currency_from_another_line_cannot_be_assigned_to_net_payment():
    hits = evidence()
    hits[
        0
    ].chunk.content = (
        "Period: January 2026\nNet payment USD 2000.10\nGross payment AUD 2500.00"
    )
    with pytest.raises(ValueError):
        calculate(calculation(), hits)
